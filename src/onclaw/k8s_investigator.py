from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from kubernetes import client, config as k8s_config
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException

logger = logging.getLogger(__name__)


@dataclass
class PodInfo:
    name: str
    namespace: str
    status: str
    restart_count: int
    ready: bool
    age: str
    container_statuses: list[dict[str, str]]
    # Diagnostics for unhealthy pods — why is it down?
    conditions: list[dict[str, str]] = field(default_factory=list)
    last_terminated: list[dict[str, str]] = field(default_factory=list)


@dataclass
class PodLogSnippet:
    pod_name: str
    namespace: str
    container_name: str
    log_lines: str
    is_previous: bool = False


@dataclass
class K8sEvent:
    namespace: str
    involved_object: str
    reason: str
    message: str
    count: int
    last_timestamp: str


@dataclass
class InvestigationData:
    timestamp: str
    context_used: str
    namespaces_checked: list[str]
    pods: list[PodInfo] = field(default_factory=list)
    unhealthy_pods: list[PodInfo] = field(default_factory=list)
    pod_logs: list[PodLogSnippet] = field(default_factory=list)
    events: list[K8sEvent] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    total_pod_count: int = 0


    def merge(self, other: InvestigationData) -> None:
        """Merge another round's data, deduplicating pods/logs/events."""
        existing_pods = {(p.namespace, p.name) for p in self.pods}
        for p in other.pods:
            if (p.namespace, p.name) not in existing_pods:
                self.pods.append(p)

        existing_unhealthy = {(p.namespace, p.name) for p in self.unhealthy_pods}
        for p in other.unhealthy_pods:
            if (p.namespace, p.name) not in existing_unhealthy:
                self.unhealthy_pods.append(p)

        existing_logs = {
            (l.namespace, l.pod_name, l.container_name, l.is_previous)
            for l in self.pod_logs
        }
        for log in other.pod_logs:
            key = (log.namespace, log.pod_name, log.container_name, log.is_previous)
            if key not in existing_logs:
                self.pod_logs.append(log)

        existing_events = {
            (e.namespace, e.involved_object, e.reason, e.message) for e in self.events
        }
        for e in other.events:
            if (e.namespace, e.involved_object, e.reason, e.message) not in existing_events:
                self.events.append(e)

        self.errors.extend(other.errors)


@dataclass
class ClusterInfo:
    """Discovered cluster topology — contexts and their namespaces."""
    contexts: dict[str, list[str]]  # context_name -> [namespace, ...]


def _compute_age(creation_timestamp: datetime | None) -> str:
    if creation_timestamp is None:
        return "unknown"
    delta = datetime.now(timezone.utc) - creation_timestamp.replace(tzinfo=timezone.utc)
    total_seconds = int(delta.total_seconds())
    if total_seconds < 60:
        return f"{total_seconds}s"
    if total_seconds < 3600:
        return f"{total_seconds // 60}m"
    if total_seconds < 86400:
        return f"{total_seconds // 3600}h"
    return f"{total_seconds // 86400}d"


def _is_unhealthy(pod: client.V1Pod) -> bool:
    """Determine if a pod is unhealthy based on its status."""
    status = pod.status
    if status is None:
        return True

    phase = status.phase
    if phase in ("Failed", "Unknown"):
        return True

    container_statuses = status.container_statuses or []
    for cs in container_statuses:
        if cs.restart_count and cs.restart_count > 2:
            return True
        if not cs.ready:
            return True
        if cs.state:
            if cs.state.waiting and cs.state.waiting.reason in (
                "CrashLoopBackOff", "ErrImagePull", "ImagePullBackOff",
                "CreateContainerConfigError", "OOMKilled",
            ):
                return True
            if cs.state.terminated and cs.state.terminated.reason in (
                "OOMKilled", "Error",
            ):
                return True

    return False


def _extract_pod_info(pod: client.V1Pod, unhealthy: bool = False) -> PodInfo:
    status = pod.status
    container_statuses_info: list[dict[str, str]] = []
    total_restarts = 0
    all_ready = True

    for cs in (status.container_statuses or []) if status else []:
        total_restarts += cs.restart_count or 0
        if not cs.ready:
            all_ready = False

        info: dict[str, str] = {"name": cs.name}
        if cs.state:
            if cs.state.running:
                info["state"] = "running"
            elif cs.state.waiting:
                info["state"] = f"waiting: {cs.state.waiting.reason or 'unknown'}"
            elif cs.state.terminated:
                info["state"] = f"terminated: {cs.state.terminated.reason or 'unknown'}"
        container_statuses_info.append(info)

    # For unhealthy pods: capture diagnostics (why is it down?)
    conditions: list[dict[str, str]] = []
    last_terminated: list[dict[str, str]] = []
    if unhealthy and status:
        # Pod conditions that are not met (e.g. Ready=False)
        for cond in status.conditions or []:
            if cond.status != "True":
                conditions.append({
                    "type": cond.type or "",
                    "reason": cond.reason or "",
                    "message": cond.message or "",
                })

        # Last terminated state per container (exit code, crash reason)
        for cs in status.container_statuses or []:
            if cs.last_state and cs.last_state.terminated:
                t = cs.last_state.terminated
                last_terminated.append({
                    "container": cs.name,
                    "reason": t.reason or "",
                    "exit_code": str(t.exit_code) if t.exit_code is not None else "",
                    "finished_at": t.finished_at.isoformat() if t.finished_at else "",
                })

    return PodInfo(
        name=pod.metadata.name,
        namespace=pod.metadata.namespace,
        status=status.phase if status else "Unknown",
        restart_count=total_restarts,
        ready=all_ready,
        age=_compute_age(pod.metadata.creation_timestamp),
        container_statuses=container_statuses_info,
        conditions=conditions,
        last_terminated=last_terminated,
    )


class K8sInvestigator:
    def __init__(self, kubeconfig_path: str | None = None) -> None:
        self._kubeconfig_path = kubeconfig_path
        self._has_in_cluster = self._detect_in_cluster()
        self._cluster_info: ClusterInfo | None = None

    @staticmethod
    def _detect_in_cluster() -> bool:
        """Check if we're running inside a K8s pod."""
        return Path("/var/run/secrets/kubernetes.io/serviceaccount/token").exists()

    def discover_cluster_info(self) -> ClusterInfo:
        """Discover all available contexts and their namespaces.

        Supports three modes:
        - In-cluster only (no kubeconfig): discovers the local cluster
        - Kubeconfig only (outside cluster): discovers all contexts from kubeconfig
        - Both (in-cluster + kubeconfig mounted): discovers local cluster + all remote contexts
        """
        if self._cluster_info is not None:
            return self._cluster_info

        contexts_map: dict[str, list[str]] = {}

        # 1. In-cluster context (if running inside K8s)
        if self._has_in_cluster:
            self._discover_context("in-cluster", contexts_map)

        # 2. All contexts from kubeconfig (if provided)
        if self._kubeconfig_path:
            try:
                contexts, _ = k8s_config.list_kube_config_contexts(
                    config_file=self._kubeconfig_path
                )
                for ctx in contexts:
                    ctx_name = ctx["name"]
                    if ctx_name not in contexts_map:
                        self._discover_context(ctx_name, contexts_map)
            except Exception as e:
                logger.warning("Failed to read kubeconfig contexts: %s", e)

        # 3. Fallback: if nothing was discovered and we have a kubeconfig, try default
        if not contexts_map and self._kubeconfig_path:
            self._discover_context("default", contexts_map)

        if not contexts_map:
            logger.warning("No Kubernetes clusters discovered")

        self._cluster_info = ClusterInfo(contexts=contexts_map)
        return self._cluster_info

    def _discover_context(
        self, ctx_name: str, contexts_map: dict[str, list[str]]
    ) -> None:
        try:
            api_client = self._get_api_client(ctx_name)
            core_api = client.CoreV1Api(api_client=api_client)
            ns_list = core_api.list_namespace()
            namespaces = [ns.metadata.name for ns in ns_list.items]
            contexts_map[ctx_name] = sorted(namespaces)
            logger.info(
                "Discovered context '%s': %d namespaces", ctx_name, len(namespaces)
            )
        except Exception as e:
            logger.warning(
                "Failed to discover namespaces for context '%s': %s", ctx_name, e
            )
            contexts_map[ctx_name] = []

    def _get_api_client(self, context_name: str) -> client.ApiClient:
        """Get an API client for a specific context."""
        if context_name == "in-cluster":
            k8s_config.load_incluster_config()
            return client.ApiClient()
        else:
            return k8s_config.new_client_from_config(
                config_file=self._kubeconfig_path,
                context=context_name,
            )

    def list_pod_names(
        self, context: str, namespaces: list[str],
    ) -> list[str]:
        """List all pod names (namespace/name) — lightweight, no logs or details."""
        names: list[str] = []
        try:
            api_client = self._get_api_client(context)
        except Exception as e:
            logger.warning("Failed to connect to context '%s': %s", context, e)
            return names

        core_api = client.CoreV1Api(api_client=api_client)
        for ns in namespaces:
            try:
                pods = core_api.list_namespaced_pod(namespace=ns)
                for pod in pods.items:
                    names.append(f"{ns}/{pod.metadata.name}")
            except Exception as e:
                logger.warning("Failed to list pods in %s: %s", ns, e)
        return names

    def investigate(
        self,
        context: str,
        namespaces: list[str],
        max_log_lines: int = 200,
        target_pod_names: list[str] | None = None,
        target_service_names: list[str] | None = None,
    ) -> InvestigationData:
        data = InvestigationData(
            timestamp=datetime.now(timezone.utc).isoformat(),
            context_used=context,
            namespaces_checked=list(namespaces),
        )

        try:
            api_client = self._get_api_client(context)
        except (ConfigException, Exception) as e:
            msg = f"Failed to connect to context '{context}': {e}"
            logger.error(msg)
            data.errors.append(msg)
            return data

        core_api = client.CoreV1Api(api_client=api_client)

        for ns in namespaces:
            self._collect_pods(
                core_api, ns, data,
                target_pod_names or [], target_service_names or [],
            )
            self._collect_events(core_api, ns, data)

        # Collect logs: running pods get current logs only,
        # unhealthy pods also get previous logs + diagnostics are already in PodInfo
        for pod in data.pods:
            is_down = pod in data.unhealthy_pods
            self._collect_pod_logs(core_api, pod, max_log_lines, data, is_down)

        return data

    @staticmethod
    def _matches_targets(
        pod: PodInfo,
        pod_names: list[str],
        service_names: list[str],
    ) -> bool:
        """Check if a pod matches any AI-extracted target names.

        Supports both namespace-qualified (``ns/pod-name``) and bare names.
        """
        qualified = f"{pod.namespace}/{pod.name}".lower()
        name_lower = pod.name.lower()
        for target in pod_names:
            t = target.lower()
            # Exact namespace/name match takes priority
            if "/" in t:
                if t == qualified:
                    return True
            elif t in name_lower:
                return True
        for target in service_names:
            if target.lower() in name_lower:
                return True
        return False

    def _collect_pods(
        self,
        core_api: client.CoreV1Api,
        namespace: str,
        data: InvestigationData,
        target_pod_names: list[str],
        target_service_names: list[str],
    ) -> None:
        try:
            pods = core_api.list_namespaced_pod(namespace=namespace)
            for pod in pods.items:
                data.total_pod_count += 1
                unhealthy = _is_unhealthy(pod)
                pod_info = _extract_pod_info(pod, unhealthy=unhealthy)
                targeted = self._matches_targets(
                    pod_info, target_pod_names, target_service_names,
                )

                # Only include targeted or unhealthy pods in detail
                if targeted or unhealthy:
                    data.pods.append(pod_info)
                if unhealthy:
                    data.unhealthy_pods.append(pod_info)
        except ApiException as e:
            msg = f"Failed to list pods in {namespace}: {e.reason} (HTTP {e.status})"
            logger.warning(msg)
            data.errors.append(msg)
        except Exception as e:
            msg = f"Failed to list pods in {namespace}: {e}"
            logger.warning(msg)
            data.errors.append(msg)

    def _collect_events(
        self, core_api: client.CoreV1Api, namespace: str, data: InvestigationData
    ) -> None:
        try:
            events = core_api.list_namespaced_event(
                namespace=namespace,
                field_selector="type=Warning",
            )
            for event in events.items:
                last_ts = event.last_timestamp or event.event_time
                ts_str = last_ts.isoformat() if last_ts else "unknown"
                data.events.append(K8sEvent(
                    namespace=namespace,
                    involved_object=(
                        f"{event.involved_object.kind}/{event.involved_object.name}"
                        if event.involved_object else "unknown"
                    ),
                    reason=event.reason or "unknown",
                    message=event.message or "",
                    count=event.count or 1,
                    last_timestamp=ts_str,
                ))
        except ApiException as e:
            msg = f"Failed to list events in {namespace}: {e.reason} (HTTP {e.status})"
            logger.warning(msg)
            data.errors.append(msg)
        except Exception as e:
            msg = f"Failed to list events in {namespace}: {e}"
            logger.warning(msg)
            data.errors.append(msg)

    def _collect_pod_logs(
        self,
        core_api: client.CoreV1Api,
        pod: PodInfo,
        max_log_lines: int,
        data: InvestigationData,
        is_unhealthy: bool = False,
    ) -> None:
        for cs in pod.container_statuses:
            container_name = cs["name"]

            # Current container logs (always collected)
            try:
                logs = core_api.read_namespaced_pod_log(
                    name=pod.name,
                    namespace=pod.namespace,
                    container=container_name,
                    tail_lines=max_log_lines,
                )
                data.pod_logs.append(PodLogSnippet(
                    pod_name=pod.name,
                    namespace=pod.namespace,
                    container_name=container_name,
                    log_lines=logs or "(no logs)",
                ))
            except ApiException as e:
                msg = (
                    f"Failed to get logs for {pod.namespace}/{pod.name}/{container_name}: "
                    f"{e.reason}"
                )
                logger.warning(msg)
                data.errors.append(msg)

            # Previous container logs — only for unhealthy pods (crash diagnostics).
            # Running pods don't need previous logs; their current logs have the evidence.
            if is_unhealthy:
                try:
                    prev_logs = core_api.read_namespaced_pod_log(
                        name=pod.name,
                        namespace=pod.namespace,
                        container=container_name,
                        tail_lines=max_log_lines,
                        previous=True,
                    )
                    if prev_logs:
                        data.pod_logs.append(PodLogSnippet(
                            pod_name=pod.name,
                            namespace=pod.namespace,
                            container_name=container_name,
                            log_lines=prev_logs,
                            is_previous=True,
                        ))
                except ApiException:
                    # Previous logs not available is normal
                    pass
