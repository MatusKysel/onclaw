from __future__ import annotations

import logging
import re

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from onclaw.ai_summarizer import AISummarizer
from onclaw.config import OnclawConfig
from onclaw.investigation import InvestigationOrchestrator
from onclaw.k8s_investigator import ClusterInfo
from onclaw.notifier import AlertEvent, SlackNotifier, is_obvious_non_alert

logger = logging.getLogger(__name__)

_DETAIL_KEYWORDS = {"details", "detail", "more", "expand", "elaborate", "info", "full", "logs", "explain"}
_DETAIL_PATTERN = re.compile(r"\b(?:details?|more|expand|elaborate|info|full|logs|explain)\b")


def _is_detail_request(text: str) -> bool:
    return _DETAIL_PATTERN.search(text.lower()) is not None


class SlackListener:
    def __init__(
        self,
        config: OnclawConfig,
        orchestrator: InvestigationOrchestrator,
        summarizer: AISummarizer,
        cluster_info: ClusterInfo,
        notifier: SlackNotifier,
    ) -> None:
        self._config = config
        self._orchestrator = orchestrator
        self._summarizer = summarizer
        self._cluster_info = cluster_info
        self._notifier = notifier
        self._channel_names: dict[str, str] = {}

        self._app = App(token=config.slack_bot_token)
        self._app.event("message")(self._handle_message)

    def _resolve_channel_name(self, channel_id: str) -> str:
        """Lazily resolve and cache channel names from the Slack API."""
        if channel_id in self._channel_names:
            return self._channel_names[channel_id]
        try:
            resp = self._app.client.conversations_info(channel=channel_id)
            name = resp["channel"].get("name") or channel_id
            self._channel_names[channel_id] = name
            return name
        except Exception:
            # Don't cache failures — next message will retry
            return channel_id

    def _handle_message(self, event: dict, say: object, logger: logging.Logger) -> None:
        # Ignore bot messages, edits, deletions, etc.
        if event.get("subtype") is not None:
            return

        channel_id = event.get("channel")
        if not channel_id:
            return

        text = event.get("text", "")
        if not text or is_obvious_non_alert(text):
            return

        channel_name = self._resolve_channel_name(channel_id)

        # Check if this is a thread reply asking for details
        thread_ts = event.get("thread_ts")
        if thread_ts and thread_ts != event.get("ts"):
            if _is_detail_request(text):
                logger.info("Detail request in #%s thread %s", channel_name, thread_ts)
                self._orchestrator.expand(channel_id, thread_ts, self._notifier)
                return

        alert_event = AlertEvent(
            channel_id=channel_id,
            message_id=event["ts"],
            channel_name=channel_name,
            text=text,
        )

        # AI classifies the message
        try:
            classification = self._summarizer.classify_message(
                message_text=text,
                channel_name=channel_name,
                cluster_info=self._cluster_info,
            )
        except Exception:
            logger.exception("Failed to classify message in #%s", channel_name)
            self._notifier.post_reply(
                alert_event,
                ":warning: Failed to classify message. Check bot logs for details.",
            )
            return

        if not classification.is_alert:
            logger.debug("Message in #%s classified as non-alert, skipping", channel_name)
            return

        logger.info(
            "Alert detected in #%s: severity=%s, context=%s, namespaces=%s, pods=%s",
            channel_name,
            classification.severity,
            classification.context,
            classification.namespaces,
            classification.pod_names,
        )

        self._orchestrator.submit(classification, alert_event, self._notifier)

    def start(self) -> None:
        """Start listening for Slack events via Socket Mode. Blocks forever."""
        handler = SocketModeHandler(self._app, self._config.slack_app_token)
        logger.info("Onclaw Slack listener started")
        handler.start()

    @property
    def app(self) -> App:
        return self._app
