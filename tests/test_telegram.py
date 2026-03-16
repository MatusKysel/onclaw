from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from onclaw.notifier import AlertEvent
from onclaw.telegram_listener import TelegramClient, TelegramListener, TelegramNotifier


class TestTelegramNotifier:
    def test_indicate_investigating_sends_reply(self) -> None:
        mock_client = MagicMock(spec=TelegramClient)
        mock_client.send_message.return_value = {"result": {"message_id": 42}}
        notifier = TelegramNotifier(mock_client)

        event = AlertEvent(
            channel_id="-100123",
            message_id="10",
            channel_name="alerts",
            text="test alert",
        )
        notifier.indicate_investigating(event)

        mock_client.send_message.assert_called_once_with(
            chat_id=-100123,
            text="Investigating...",
            reply_to_message_id=10,
        )

    def test_indicate_complete_deletes_status(self) -> None:
        mock_client = MagicMock(spec=TelegramClient)
        mock_client.send_message.return_value = {"result": {"message_id": 42}}
        notifier = TelegramNotifier(mock_client)

        event = AlertEvent(
            channel_id="-100123",
            message_id="10",
            channel_name="alerts",
            text="test",
        )
        notifier.indicate_investigating(event)
        notifier.indicate_complete(event)

        mock_client.delete_message.assert_called_once_with(-100123, 42)

    def test_indicate_failed_edits_status(self) -> None:
        mock_client = MagicMock(spec=TelegramClient)
        mock_client.send_message.return_value = {"result": {"message_id": 42}}
        notifier = TelegramNotifier(mock_client)

        event = AlertEvent(
            channel_id="-100123",
            message_id="10",
            channel_name="alerts",
            text="test",
        )
        notifier.indicate_investigating(event)
        notifier.indicate_failed(event)

        mock_client.edit_message_text.assert_called_once_with(
            -100123, 42, "Investigation failed. Check bot logs for details.",
        )

    def test_post_reply(self) -> None:
        mock_client = MagicMock(spec=TelegramClient)
        notifier = TelegramNotifier(mock_client)

        event = AlertEvent(
            channel_id="-100123",
            message_id="10",
            channel_name="alerts",
            text="test",
        )
        notifier.post_reply(event, "Summary text")

        mock_client.send_message.assert_called_once_with(
            chat_id=-100123,
            text="Summary text",
            reply_to_message_id=10,
            parse_mode="Markdown",
        )


class TestTelegramListener:
    def test_handle_update_classifies_and_submits(self) -> None:
        from onclaw.ai_summarizer import AlertClassification

        mock_summarizer = MagicMock()
        mock_summarizer.classify_message.return_value = AlertClassification(
            is_alert=True,
            severity="critical",
            context="mainnet",
            namespaces=["prover"],
            pod_names=["prover-0"],
            service_names=[],
        )

        mock_orchestrator = MagicMock()

        config = MagicMock()
        config.telegram_bot_token = "fake-token"

        cluster_info = MagicMock()

        with patch.object(TelegramListener, "__init__", lambda self, *a, **kw: None):
            listener = TelegramListener.__new__(TelegramListener)
            listener._summarizer = mock_summarizer
            listener._orchestrator = mock_orchestrator
            listener._cluster_info = cluster_info
            listener._client = MagicMock()
            listener._notifier = MagicMock()
            listener._bot_id = 999

        update = {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "from": {"id": 123, "first_name": "User"},
                "chat": {"id": -100123, "title": "Alerts L2", "type": "supergroup"},
                "text": "prover-0 not proving blocks",
            },
        }

        listener._handle_update(update)

        mock_summarizer.classify_message.assert_called_once()
        mock_orchestrator.submit.assert_called_once()

        # Verify the AlertEvent was constructed correctly
        call_args = mock_orchestrator.submit.call_args
        alert_event = call_args[0][1]
        assert alert_event.channel_id == "-100123"
        assert alert_event.message_id == "10"
        assert alert_event.channel_name == "Alerts L2"
        assert alert_event.text == "prover-0 not proving blocks"

    def test_handle_update_skips_bot_messages(self) -> None:
        mock_orchestrator = MagicMock()

        with patch.object(TelegramListener, "__init__", lambda self, *a, **kw: None):
            listener = TelegramListener.__new__(TelegramListener)
            listener._orchestrator = mock_orchestrator
            listener._bot_id = 999

        update = {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "from": {"id": 999},  # bot's own message
                "chat": {"id": -100123, "title": "Alerts"},
                "text": "some text",
            },
        }

        listener._handle_update(update)
        mock_orchestrator.submit.assert_not_called()

    def test_handle_update_skips_non_alert(self) -> None:
        from onclaw.ai_summarizer import AlertClassification

        mock_summarizer = MagicMock()
        mock_summarizer.classify_message.return_value = AlertClassification(
            is_alert=False, severity="info"
        )

        mock_orchestrator = MagicMock()

        with patch.object(TelegramListener, "__init__", lambda self, *a, **kw: None):
            listener = TelegramListener.__new__(TelegramListener)
            listener._summarizer = mock_summarizer
            listener._orchestrator = mock_orchestrator
            listener._cluster_info = MagicMock()
            listener._bot_id = 999

        update = {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "from": {"id": 123},
                "chat": {"id": -100123, "title": "Alerts"},
                "text": "hey team, lunch?",
            },
        }

        listener._handle_update(update)
        mock_orchestrator.submit.assert_not_called()

    def test_handle_update_detail_request_with_punctuation_expands(self) -> None:
        mock_orchestrator = MagicMock()

        with patch.object(TelegramListener, "__init__", lambda self, *a, **kw: None):
            listener = TelegramListener.__new__(TelegramListener)
            listener._orchestrator = mock_orchestrator
            listener._notifier = MagicMock()
            listener._bot_id = 999

        update = {
            "update_id": 1,
            "message": {
                "message_id": 11,
                "from": {"id": 123},
                "chat": {"id": -100123, "title": "Alerts"},
                "text": "details?",
                "reply_to_message": {
                    "message_id": 10,
                    "from": {"id": 999},
                },
            },
        }

        listener._handle_update(update)
        mock_orchestrator.expand.assert_called_once_with(
            "-100123", "10", listener._notifier
        )
