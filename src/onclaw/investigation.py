from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

from onclaw.ai_summarizer import AISummarizer, AlertClassification
from onclaw.config import OnclawConfig
from onclaw.k8s_investigator import InvestigationData, K8sInvestigator
from onclaw.memory import InvestigationRecord, Memory, format_past_investigations
from onclaw.notifier import AlertEvent, Notifier

logger = logging.getLogger(__name__)

MAX_CACHE_SIZE = 100


@dataclass
class CachedInvestigation:
    """Stored investigation context for detail expansion."""

    alert_text: str
    data: InvestigationData
    past_investigations: str
    event: AlertEvent


class InvestigationOrchestrator:
    def __init__(
        self,
        config: OnclawConfig,
        k8s: K8sInvestigator,
        summarizer: AISummarizer,
        memory: Memory,
    ) -> None:
        self._config = config
        self._k8s = k8s
        self._summarizer = summarizer
        self._memory = memory
        self._executor = ThreadPoolExecutor(
            max_workers=config.max_concurrent_investigations
        )
        self._active: set[str] = set()
        self._lock = threading.Lock()
        # Cache for detail expansion: {reply_msg_key: CachedInvestigation}
        self._cache: OrderedDict[str, CachedInvestigation] = OrderedDict()
        self._cache_lock = threading.Lock()

    def submit(
        self,
        classification: AlertClassification,
        event: AlertEvent,
        notifier: Notifier,
    ) -> None:
        """Submit an alert for investigation. Deduplicates by channel+message."""
        key = f"{event.channel_id}:{event.message_id}"
        with self._lock:
            if key in self._active:
                logger.debug("Skipping duplicate investigation for %s", key)
                return
            self._active.add(key)

        self._executor.submit(self._run, classification, event, notifier, key)

    def expand(self, channel_id: str, reply_msg_id: str, notifier: Notifier) -> None:
        """Generate and post the detailed summary for a cached investigation."""
        cache_key = f"{channel_id}:{reply_msg_id}"
        with self._cache_lock:
            cached = self._cache.get(cache_key)

        if not cached:
            logger.debug("No cached investigation for %s", cache_key)
            return

        self._executor.submit(self._run_expand, cached, notifier)

    def _run_expand(self, cached: CachedInvestigation, notifier: Notifier) -> None:
        try:
            detailed = self._summarizer.summarize(
                alert_text=cached.alert_text,
                investigation_data=cached.data,
                past_investigations=cached.past_investigations,
                detailed=True,
            )
            notifier.post_reply(cached.event, detailed)
        except Exception as e:
            logger.exception("Failed to generate detailed summary")
            try:
                notifier.post_reply(
                    cached.event, f"Failed to generate detailed summary: {e}"
                )
            except Exception:
                pass

    def _resolve_targets(
        self,
        classification: AlertClassification,
        event: AlertEvent,
        past_records: list[InvestigationRecord],
    ) -> list[str]:
        """Determine which pods to investigate, using cached knowledge when possible.

        Priority:
        1. Classification already extracted specific pod/service names → use directly
        2. Memory has a past investigation for same channel+context → reuse its pods
        3. Fallback: list all pods and ask AI to select (most expensive)
        """
        # 1. Classification already identified specific targets
        if classification.pod_names or classification.service_names:
            logger.info(
                "Using targets from classification (skipped AI selection): "
                "pods=%s services=%s",
                classification.pod_names,
                classification.service_names,
            )
            return classification.pod_names

        # 2. Memory has relevant past investigation for this channel+context
        allowed_namespaces = set(classification.namespaces)
        for record in past_records:
            if (
                record.channel_name == event.channel_name
                and record.context == classification.context
                and record.pod_names
            ):
                # Only reuse targets when the stored investigation actually
                # overlaps the namespaces selected for the current alert.
                if allowed_namespaces and not allowed_namespaces.intersection(record.namespaces):
                    continue

                compatible_targets = [
                    pod_name
                    for pod_name in record.pod_names
                    if "/" not in pod_name or pod_name.split("/", 1)[0] in allowed_namespaces
                ]
                if not compatible_targets:
                    continue

                logger.info(
                    "Using pod targets from memory (skipped AI selection): %s",
                    compatible_targets,
                )
                return compatible_targets

        # 3. Fallback: list pods and let AI select
        all_pod_names = self._k8s.list_pod_names(
            context=classification.context,
            namespaces=classification.namespaces,
        )

        if not all_pod_names:
            return classification.pod_names or []

        # Pre-shrink: filter pods by classification keywords to reduce AI prompt
        keywords = classification.keywords + classification.service_names
        if keywords:
            filtered = [
                p for p in all_pod_names
                if any(kw.lower() in p.lower() for kw in keywords)
            ]
            # Only use filtered list if it actually narrowed things down
            if filtered:
                logger.info(
                    "Pre-filtered %d → %d pods using keywords %s",
                    len(all_pod_names), len(filtered), keywords,
                )
                all_pod_names = filtered

        logger.info(
            "No cached targets — AI selecting from %d pods in %s/%s",
            len(all_pod_names),
            classification.context,
            classification.namespaces,
        )

        ai_selected = self._summarizer.select_pods(
            alert_text=event.text,
            channel_name=event.channel_name,
            pod_names=all_pod_names,
        )
        return list(dict.fromkeys(ai_selected + classification.pod_names))

    def _run(
        self,
        classification: AlertClassification,
        event: AlertEvent,
        notifier: Notifier,
        key: str,
    ) -> None:
        try:
            # 1. Signal investigation started
            notifier.indicate_investigating(event)

            logger.info(
                "Starting investigation: channel=%s severity=%s context=%s "
                "namespaces=%s target_pods=%s target_services=%s",
                event.channel_name,
                classification.severity,
                classification.context,
                classification.namespaces,
                classification.pod_names,
                classification.service_names,
            )

            # 2. Search memory for past similar investigations
            past_records = self._memory.search(event.text, limit=3)
            past_context = format_past_investigations(past_records)
            if past_records:
                logger.info(
                    "Found %d past investigations for similar alerts", len(past_records)
                )

            # 3. Resolve target pods — skip AI selection when memory/classification
            #    already provides specific targets
            target_pods = self._resolve_targets(
                classification, event, past_records,
            )
            remembered_pod_targets = list(target_pods)
            logger.info("Target pods for investigation: %s", target_pods)

            # 4. Collect K8s data only for targeted + unhealthy pods
            data = self._k8s.investigate(
                context=classification.context,
                namespaces=classification.namespaces,
                max_log_lines=self._config.max_log_lines,
                target_pod_names=target_pods,
                target_service_names=classification.service_names,
            )

            logger.info(
                "K8s data collected: %d/%d pods investigated, %d unhealthy, "
                "%d events, %d errors",
                len(data.pods),
                data.total_pod_count,
                len(data.unhealthy_pods),
                len(data.events),
                len(data.errors),
            )

            # 4b. Multi-hop follow-up — chase leads across pods
            if self._config.max_follow_up_depth > 0 and data.pod_logs:
                all_pod_names = self._k8s.list_pod_names(
                    context=classification.context,
                    namespaces=classification.namespaces,
                )
                investigated = {
                    f"{p.namespace}/{p.name}" for p in data.pods
                }
                latest_data = data

                for depth in range(self._config.max_follow_up_depth):
                    remaining = [
                        p for p in all_pod_names if p not in investigated
                    ]
                    if not remaining:
                        break

                    follow_ups = self._summarizer.suggest_follow_up_pods(
                        data=latest_data,
                        available_pods=remaining,
                        investigated=list(investigated),
                    )
                    if not follow_ups:
                        break

                    logger.info(
                        "Follow-up round %d: investigating %s",
                        depth + 1, follow_ups,
                    )

                    latest_data = self._k8s.investigate(
                        context=classification.context,
                        namespaces=classification.namespaces,
                        max_log_lines=self._config.max_log_lines,
                        target_pod_names=follow_ups,
                    )
                    data.merge(latest_data)
                    found_follow_ups = {
                        f"{p.namespace}/{p.name}" for p in latest_data.pods
                    }
                    remembered_pod_targets.extend(
                        pod_name for pod_name in follow_ups if pod_name in found_follow_ups
                    )
                    investigated.update(
                        f"{p.namespace}/{p.name}" for p in latest_data.pods
                    )

                    logger.info(
                        "After follow-up %d: %d total pods investigated",
                        depth + 1, len(data.pods),
                    )

            # 5. Generate SHORT AI summary
            summary = self._summarizer.summarize(
                alert_text=event.text,
                investigation_data=data,
                past_investigations=past_context,
                detailed=False,
            )

            # 6. Post short reply and cache data for expansion
            reply_id = notifier.post_reply(event, summary)

            if reply_id:
                cached = CachedInvestigation(
                    alert_text=event.text,
                    data=data,
                    past_investigations=past_context,
                    event=event,
                )
                reply_key = f"{event.channel_id}:{reply_id}"
                parent_key = f"{event.channel_id}:{event.message_id}"
                with self._cache_lock:
                    self._cache[reply_key] = cached
                    self._cache[parent_key] = cached
                    # Evict oldest entries if cache is too large
                    while len(self._cache) > MAX_CACHE_SIZE:
                        self._cache.popitem(last=False)

            # 7. Signal completion
            notifier.indicate_complete(event)

            # 8. Store this investigation in memory (with resolved targets,
            #    so future alerts can reuse them without AI selection)
            self._memory.store(InvestigationRecord(
                timestamp=datetime.now(timezone.utc).isoformat(),
                channel_name=event.channel_name,
                alert_text=event.text,
                severity=classification.severity,
                context=classification.context,
                namespaces=classification.namespaces,
                pod_names=list(dict.fromkeys(remembered_pod_targets)),
                service_names=classification.service_names,
                unhealthy_pods=[p.name for p in data.unhealthy_pods],
                summary=summary,
            ))

            logger.info("Investigation complete for %s", key)

        except Exception:
            logger.exception("Investigation failed for %s", key)
            try:
                notifier.indicate_failed(event)
            except Exception:
                logger.exception("Failed to send failure notification for %s", key)
        finally:
            with self._lock:
                self._active.discard(key)

    def shutdown(self) -> None:
        """Gracefully shut down the thread pool."""
        self._executor.shutdown(wait=True)
