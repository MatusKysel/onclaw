from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from onclaw.ai_summarizer import AISummarizer
from onclaw.config import OnclawConfig
from onclaw.investigation import InvestigationOrchestrator
from onclaw.k8s_investigator import ClusterInfo
from onclaw.notifier import AlertEvent, Notifier, is_obvious_non_alert

logger = logging.getLogger(__name__)

_DETAIL_PATTERN = re.compile(r"\b(?:details?|more|expand|elaborate|info|full|logs|explain)\b")


class TelegramClient:
    """Minimal Telegram Bot API client using stdlib only."""

    def __init__(self, token: str) -> None:
        self._base = f"https://api.telegram.org/bot{token}"

    def _call(self, method: str, **params: Any) -> dict:
        payload = {k: v for k, v in params.items() if v is not None}
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self._base}/{method}",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        http_timeout = int(params.get("timeout", 0)) + 10 if "timeout" in params else 30
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            raise RuntimeError(f"Telegram API {e.code}: {body}") from e
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API error: {result}")
        return result

    def get_me(self) -> dict:
        return self._call("getMe")["result"]

    def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict]:
        return self._call("getUpdates", offset=offset, timeout=timeout)["result"]

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = None,
    ) -> dict:
        return self._call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            reply_to_message_id=reply_to_message_id,
            parse_mode=parse_mode,
        )

    def edit_message_text(self, chat_id: int, message_id: int, text: str) -> dict:
        return self._call(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=text,
        )

    def delete_message(self, chat_id: int, message_id: int) -> dict:
        return self._call("deleteMessage", chat_id=chat_id, message_id=message_id)


class TelegramNotifier(Notifier):
    """Telegram implementation using reply messages for status feedback."""

    def __init__(self, client: TelegramClient) -> None:
        self._client = client
        self._status_messages: dict[str, int] = {}
        self._lock = threading.Lock()

    def indicate_investigating(self, event: AlertEvent) -> None:
        try:
            result = self._client.send_message(
                chat_id=int(event.channel_id),
                text="Investigating...",
                reply_to_message_id=int(event.message_id),
            )
            msg_id = result.get("result", {}).get("message_id")
            if msg_id:
                key = f"{event.channel_id}:{event.message_id}"
                with self._lock:
                    self._status_messages[key] = msg_id
        except Exception:
            logger.warning("Failed to send investigating status on Telegram")

    def indicate_complete(self, event: AlertEvent) -> None:
        key = f"{event.channel_id}:{event.message_id}"
        with self._lock:
            msg_id = self._status_messages.pop(key, None)
        if msg_id:
            try:
                self._client.delete_message(int(event.channel_id), msg_id)
            except Exception:
                pass

    def indicate_failed(self, event: AlertEvent) -> None:
        key = f"{event.channel_id}:{event.message_id}"
        with self._lock:
            msg_id = self._status_messages.pop(key, None)
        if msg_id:
            try:
                self._client.edit_message_text(
                    int(event.channel_id),
                    msg_id,
                    "Investigation failed. Check bot logs for details.",
                )
            except Exception:
                logger.warning("Failed to update Telegram status message")

    def post_reply(self, event: AlertEvent, text: str) -> str | None:
        # Try with Markdown first, fall back to plain text if it fails
        try:
            result = self._client.send_message(
                chat_id=int(event.channel_id),
                text=text,
                reply_to_message_id=int(event.message_id),
                parse_mode="Markdown",
            )
        except Exception:
            # Markdown parsing can fail on unmatched special chars — send plain
            result = self._client.send_message(
                chat_id=int(event.channel_id),
                text=text,
                reply_to_message_id=int(event.message_id),
            )
        msg_id = result.get("result", {}).get("message_id")
        return str(msg_id) if msg_id else None


class TelegramListener:
    """Listens for messages in Telegram groups via long polling."""

    def __init__(
        self,
        config: OnclawConfig,
        orchestrator: InvestigationOrchestrator,
        summarizer: AISummarizer,
        cluster_info: ClusterInfo,
    ) -> None:
        self._config = config
        self._orchestrator = orchestrator
        self._summarizer = summarizer
        self._cluster_info = cluster_info
        self._client = TelegramClient(config.telegram_bot_token)
        self._notifier = TelegramNotifier(self._client)
        self._bot_id: int | None = None

    def start(self) -> None:
        """Start polling for Telegram updates. Blocks forever."""
        me = self._client.get_me()
        self._bot_id = me["id"]
        logger.info(
            "Telegram bot @%s (id=%d) started polling",
            me.get("username"),
            self._bot_id,
        )

        offset: int | None = None
        while True:
            try:
                updates = self._client.get_updates(offset=offset, timeout=30)
                for update in updates:
                    offset = update["update_id"] + 1
                    self._handle_update(update)
            except Exception:
                logger.exception("Error polling Telegram updates")
                time.sleep(5)

    def _is_detail_request(self, text: str) -> bool:
        return _DETAIL_PATTERN.search(text.lower()) is not None

    def _handle_update(self, update: dict) -> None:
        message = update.get("message")
        if not message:
            return

        # Skip bot's own messages
        from_user = message.get("from", {})
        if from_user.get("id") == self._bot_id:
            return

        text = message.get("text", "")
        if not text:
            return

        chat = message.get("chat", {})
        chat_id = str(chat.get("id", ""))
        chat_name = chat.get("title") or chat.get("username") or chat_id
        message_id = str(message.get("message_id", ""))

        # Check if this is a reply to a bot message asking for details
        reply_to = message.get("reply_to_message")
        if reply_to and self._is_detail_request(text):
            reply_from = reply_to.get("from", {})
            if reply_from.get("id") == self._bot_id:
                reply_msg_id = str(reply_to.get("message_id", ""))
                logger.info("Detail request in TG/%s for message %s", chat_name, reply_msg_id)
                self._orchestrator.expand(chat_id, reply_msg_id, self._notifier)
                return

        # Skip obvious non-alerts (acks, emoji, short chatter) before calling AI
        if is_obvious_non_alert(text):
            return

        # Send immediate acknowledgment
        status_msg_id: int | None = None
        try:
            result = self._client.send_message(
                chat_id=int(chat_id),
                text="Processing...",
                reply_to_message_id=int(message_id),
            )
            status_msg_id = result.get("result", {}).get("message_id")
        except Exception:
            pass

        # AI classifies the message
        try:
            classification = self._summarizer.classify_message(
                message_text=text,
                channel_name=chat_name,
                cluster_info=self._cluster_info,
            )
        except Exception:
            logger.exception("Failed to classify message in TG/%s", chat_name)
            if status_msg_id:
                try:
                    self._client.edit_message_text(
                        int(chat_id), status_msg_id,
                        "Failed to classify message. Check bot logs for details.",
                    )
                except Exception:
                    pass
            return

        if not classification.is_alert:
            logger.debug("Message in TG/%s classified as non-alert", chat_name)
            # Remove the "Processing..." message for non-alerts
            if status_msg_id:
                try:
                    self._client.delete_message(int(chat_id), status_msg_id)
                except Exception:
                    pass
            return

        logger.info(
            "Alert detected in TG/%s: severity=%s, context=%s, namespaces=%s",
            chat_name,
            classification.severity,
            classification.context,
            classification.namespaces,
        )

        # Clean up "Processing..." — the orchestrator posts its own "Investigating..." status
        if status_msg_id:
            try:
                self._client.delete_message(int(chat_id), status_msg_id)
            except Exception:
                pass

        event = AlertEvent(
            channel_id=chat_id,
            message_id=message_id,
            channel_name=chat_name,
            text=text,
        )

        self._orchestrator.submit(classification, event, self._notifier)
