from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import anthropic

from onclaw.k8s_investigator import ClusterInfo, InvestigationData

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_SHORT = """\
You are an SRE assistant analyzing a Kubernetes incident. Provide a VERY brief summary \
(3-5 lines max). Use emojis for visual clarity.

Format:
🔴/🟡/🟢 *Status:* one-line summary of what's happening
🔍 *Root cause:* one sentence
⚡ *Action:* one sentence
🔁 If recurring, mention briefly

No logs, no pod listings, no detailed evidence. Keep it under 80 words.
Use single *asterisks* for bold (NOT double **). Do NOT use markdown headers.
End with: Reply *details* for full investigation."""

SYSTEM_PROMPT_DETAILED = """\
You are an SRE assistant analyzing a Kubernetes incident. You will be given an alert message \
and data collected from the cluster. Provide a thorough investigation summary with emojis.

Format your response with:
🔴 *Status* — severity and what's affected
🔍 *Root Cause* — what went wrong and why
📋 *Evidence* — key findings from logs and events (use `code` for pod names, include short log snippets in code blocks)
🏥 *Affected* — which pods/services are impacted
⚡ *Next Steps* — numbered action items for the on-call engineer
🔁 *History* — if past investigations exist, reference what worked before

Use single *asterisks* for bold (NOT double **). Use `backticks` for code and \
``` for code blocks. Do NOT use markdown headers (#). Keep under 500 words."""

CLASSIFY_AND_EXTRACT_PROMPT = """\
You are an SRE assistant monitoring chat channels for infrastructure alerts.

Given a message and the available Kubernetes cluster topology, determine:
1. Is this an alert/incident message that requires investigation?
2. If yes, which Kubernetes context and namespaces should we investigate?
3. Extract any pod/service hints from the message.

Available Kubernetes clusters and namespaces:
{cluster_inventory}

The message came from channel: "{channel_name}"

Return ONLY valid JSON, no markdown, no explanation:
{{
  "is_alert": true/false,
  "severity": "critical" or "warning" or "info",
  "context": "context-name-from-the-list-above",
  "namespaces": ["ns1", "ns2"],
  "pod_names": [],
  "service_names": [],
  "keywords": []
}}

Guidelines:
- is_alert: true if the message indicates an active incident, firing alert, or something broken. false for resolved alerts, informational messages, casual chat.
- severity: "critical" if service is down/stalled/not producing, "warning" for degraded performance, "info" for minor issues.
- context: pick the most relevant Kubernetes context from the list. Use the channel name and alert content as hints. If unsure, pick the one that seems most related.
- namespaces: pick the most relevant namespaces from that context. Use pod names, service names, and channel name as hints. Don't investigate irrelevant namespaces. If unsure, pick 1-3 most likely ones.
- pod_names: exact or partial pod/statefulset names mentioned (e.g. "prover-0", "l2-node-archived-1"). Parse from hostnames too (e.g. "l2-node-archived-1.mainnet:6060" -> "l2-node-archived-1").
- service_names: service, deployment, or component names (e.g. "prover", "sequencer", "bridge-relay").
- keywords: key terms to search for in logs (e.g. "L2 head", "proof generation", "not syncing")."""

SELECT_PODS_PROMPT = """\
You are an SRE assistant. Given an alert message from a monitoring channel and a list of \
all pods in the affected Kubernetes namespaces, select which pods are most relevant to \
investigate (usually 1-5 pods).

Alert: "{alert_text}"
Channel: "{channel_name}"

All pods:
{pod_list}

Return ONLY a valid JSON array of pod identifiers from the list above. \
Pick pods whose names relate to the alert keywords. Example: ["ns/pod-0", "ns/pod-1"]"""

MAX_PROMPT_CHARS = 150_000


@dataclass
class AlertClassification:
    is_alert: bool
    severity: str
    context: str = ""
    pod_names: list[str] = field(default_factory=list)
    service_names: list[str] = field(default_factory=list)
    namespaces: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)


def _format_cluster_inventory(cluster_info: ClusterInfo) -> str:
    """Format cluster topology for the AI prompt."""
    lines: list[str] = []
    for ctx_name, namespaces in cluster_info.contexts.items():
        ns_list = ", ".join(namespaces) if namespaces else "(no namespaces discovered)"
        lines.append(f"- context: {ctx_name}\n  namespaces: {ns_list}")
    return "\n".join(lines) if lines else "(no clusters available)"


def _format_investigation_data(data: InvestigationData) -> str:
    sections: list[str] = []

    # Pod overview
    sections.append(f"*Context:* {data.context_used}")
    sections.append(f"*Namespaces checked:* {', '.join(data.namespaces_checked)}")
    healthy_count = data.total_pod_count - len(data.unhealthy_pods)
    sections.append(
        f"*Total pods in namespace:* {data.total_pod_count} "
        f"({healthy_count} healthy, {len(data.unhealthy_pods)} unhealthy)"
    )
    sections.append(f"*Investigated pods:* {len(data.pods)}")

    # Only show investigated pods (targeted + unhealthy)
    if data.pods:
        lines = ["*Investigated Pod Statuses:*"]
        for p in data.pods:
            marker = " 🔴" if p in data.unhealthy_pods else ""
            lines.append(
                f"  `{p.namespace}/{p.name}` — {p.status}, "
                f"restarts: {p.restart_count}, ready: {p.ready}, age: {p.age}{marker}"
            )
        sections.append("\n".join(lines))

    # Unhealthy pod details
    if data.unhealthy_pods:
        lines = ["*Unhealthy Pod Details:*"]
        for p in data.unhealthy_pods:
            lines.append(f"  `{p.namespace}/{p.name}`:")
            for cs in p.container_statuses:
                lines.append(f"    container `{cs['name']}`: {cs.get('state', 'unknown')}")
        sections.append("\n".join(lines))

    # Pod logs
    if data.pod_logs:
        lines = ["*Pod Logs:*"]
        budget_per_log = max(500, (MAX_PROMPT_CHARS - 10_000) // max(len(data.pod_logs), 1))
        for log in data.pod_logs:
            label = f"{log.namespace}/{log.pod_name}/{log.container_name}"
            if log.is_previous:
                label += " (previous)"
            truncated = log.log_lines[:budget_per_log]
            if len(log.log_lines) > budget_per_log:
                truncated += "\n... (truncated)"
            lines.append(f"  `{label}`:\n```\n{truncated}\n```")
        sections.append("\n".join(lines))

    # Events
    if data.events:
        lines = ["*Warning Events:*"]
        for ev in data.events:
            lines.append(
                f"  [{ev.last_timestamp}] {ev.involved_object} — "
                f"{ev.reason}: {ev.message} (x{ev.count})"
            )
        sections.append("\n".join(lines))

    # Errors during collection
    if data.errors:
        lines = ["*Collection Errors:*"]
        for err in data.errors:
            lines.append(f"  - {err}")
        sections.append("\n".join(lines))

    return "\n\n".join(sections)


class AISummarizer:
    def __init__(
        self, api_key: str, model: str, max_tokens: int, fast_model: str = "",
    ) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model  # for summarization (quality matters)
        self._fast_model = fast_model or model  # for classify + select (structured JSON)
        self._max_tokens = max_tokens

    def classify_message(
        self,
        message_text: str,
        channel_name: str,
        cluster_info: ClusterInfo,
    ) -> AlertClassification:
        """Use AI to classify a message, pick the right K8s context/namespaces, and extract targets."""
        inventory = _format_cluster_inventory(cluster_info)
        system = CLASSIFY_AND_EXTRACT_PROMPT.format(
            cluster_inventory=inventory,
            channel_name=channel_name,
        )

        try:
            response = self._client.messages.create(
                model=self._fast_model,
                max_tokens=512,
                system=system,
                messages=[{"role": "user", "content": message_text}],
            )
            raw = response.content[0].text.strip()
            # Strip markdown code fences if the model wraps JSON in ```json ... ```
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                raw = raw.rsplit("```", 1)[0].strip()
            parsed = json.loads(raw)
            return AlertClassification(
                is_alert=bool(parsed.get("is_alert", False)),
                severity=parsed.get("severity", "info"),
                context=parsed.get("context", ""),
                pod_names=parsed.get("pod_names", []),
                service_names=parsed.get("service_names", []),
                namespaces=parsed.get("namespaces", []),
                keywords=parsed.get("keywords", []),
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Failed to parse classification response: %s", e)
            return AlertClassification(is_alert=False, severity="info")

    def select_pods(
        self,
        alert_text: str,
        channel_name: str,
        pod_names: list[str],
    ) -> list[str]:
        """Ask AI to select which pods are relevant to investigate."""
        pod_list = "\n".join(f"- {name}" for name in pod_names)
        prompt = SELECT_PODS_PROMPT.format(
            alert_text=alert_text,
            channel_name=channel_name,
            pod_list=pod_list,
        )

        try:
            response = self._client.messages.create(
                model=self._fast_model,
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                raw = raw.rsplit("```", 1)[0].strip()
            selected = json.loads(raw)
            if isinstance(selected, list):
                logger.info("AI selected pods: %s", selected)
                return [str(s) for s in selected]
        except Exception as e:
            logger.warning("Failed to select pods via AI: %s", e)
        return []

    def _build_user_message(
        self,
        alert_text: str,
        investigation_data: InvestigationData,
        past_investigations: str = "",
    ) -> str:
        formatted_data = _format_investigation_data(investigation_data)
        user_message = f"*Alert message:*\n> {alert_text}\n\n"
        if past_investigations:
            user_message += f"{past_investigations}\n\n"
        user_message += f"*Cluster investigation data:*\n{formatted_data}"
        return user_message

    def summarize(
        self,
        alert_text: str,
        investigation_data: InvestigationData,
        past_investigations: str = "",
        detailed: bool = False,
    ) -> str:
        user_message = self._build_user_message(
            alert_text, investigation_data, past_investigations,
        )
        system = SYSTEM_PROMPT_DETAILED if detailed else SYSTEM_PROMPT_SHORT
        max_tokens = self._max_tokens if detailed else 256

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_message}],
            )
            return response.content[0].text
        except anthropic.APIError as e:
            logger.exception("Claude API error during summarization")
            formatted_data = _format_investigation_data(investigation_data)
            fallback = (
                f":warning: *AI summary unavailable* ({e})\n\n"
                f"*Raw investigation data:*\n{formatted_data[:3000]}"
            )
            return fallback
