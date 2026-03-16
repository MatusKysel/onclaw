from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Deterministic prefilter: skip obvious non-alert chatter before calling AI.
# Conservative — only matches messages that are clearly NOT alerts.
_ACK_PATTERN = re.compile(
    r"^(?:ok|okay|k|ack|noted|thanks|thx|ty|got it|on it|will do|done|"
    r"sure|yes|no|yep|nope|np|lgtm|sgtm|\+1|👍|👌|🫡|✅|🙏|💪|👀)[\s!.?]*$",
    re.IGNORECASE,
)


def is_obvious_non_alert(text: str) -> bool:
    """Return True for messages that are clearly not alerts (acks, emoji, etc.).

    Conservative: only skips messages that cannot plausibly be alerts.
    """
    stripped = text.strip()
    if len(stripped) < 3:
        return True
    if _ACK_PATTERN.match(stripped):
        return True
    return False


@dataclass
class AlertEvent:
    """Platform-agnostic representation of an alert message."""

    channel_id: str  # Slack channel ID or Telegram chat ID
    message_id: str  # Slack ts or Telegram message_id
    channel_name: str  # resolved channel/group name
    text: str


class Notifier(ABC):
    """Abstract interface for chat platform notifications."""

    @abstractmethod
    def indicate_investigating(self, event: AlertEvent) -> None:
        """Signal that an investigation has started."""

    @abstractmethod
    def indicate_complete(self, event: AlertEvent) -> None:
        """Signal that an investigation completed successfully."""

    @abstractmethod
    def indicate_failed(self, event: AlertEvent) -> None:
        """Signal that an investigation failed."""

    @abstractmethod
    def post_reply(self, event: AlertEvent, text: str) -> str | None:
        """Post the investigation summary as a reply. Returns the reply message ID."""


class SlackNotifier(Notifier):
    """Slack implementation using emoji reactions and threaded replies."""

    def __init__(self, client: object) -> None:
        from slack_sdk import WebClient

        self._client: WebClient = client  # type: ignore[assignment]

    def indicate_investigating(self, event: AlertEvent) -> None:
        self._add_reaction(event, "eyes")

    def indicate_complete(self, event: AlertEvent) -> None:
        self._remove_reaction(event, "eyes")
        self._add_reaction(event, "white_check_mark")

    def indicate_failed(self, event: AlertEvent) -> None:
        self._remove_reaction(event, "eyes")
        self._add_reaction(event, "x")
        self.post_reply(event, ":warning: Investigation failed. Check bot logs for details.")

    def post_reply(self, event: AlertEvent, text: str) -> str | None:
        response = self._client.chat_postMessage(
            channel=event.channel_id,
            thread_ts=event.message_id,
            text=text,
            unfurl_links=False,
        )
        return response.get("ts")

    def _add_reaction(self, event: AlertEvent, name: str) -> None:
        try:
            self._client.reactions_add(
                channel=event.channel_id, timestamp=event.message_id, name=name
            )
        except Exception:
            logger.warning("Failed to add :%s: reaction", name)

    def _remove_reaction(self, event: AlertEvent, name: str) -> None:
        try:
            self._client.reactions_remove(
                channel=event.channel_id, timestamp=event.message_id, name=name
            )
        except Exception:
            logger.warning("Failed to remove :%s: reaction", name)
