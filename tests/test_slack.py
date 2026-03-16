from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from onclaw.ai_summarizer import AlertClassification
from onclaw.slack_listener import SlackListener


def _make_listener() -> SlackListener:
    """Create a SlackListener with mocked dependencies."""
    with patch.object(SlackListener, "__init__", lambda self, *a, **kw: None):
        listener = SlackListener.__new__(SlackListener)
        listener._summarizer = MagicMock()
        listener._orchestrator = MagicMock()
        listener._cluster_info = MagicMock()
        listener._notifier = MagicMock()
        listener._channel_names = {"C001": "alerts"}
        listener._app = MagicMock()
        return listener


_logger = logging.getLogger("test")


class TestSlackListener:
    def test_skips_subtypes(self) -> None:
        listener = _make_listener()
        event = {"subtype": "bot_message", "channel": "C001", "text": "hi"}
        listener._handle_message(event, MagicMock(), _logger)
        listener._orchestrator.submit.assert_not_called()

    def test_skips_empty_text(self) -> None:
        listener = _make_listener()
        event = {"channel": "C001", "text": "", "ts": "1.1"}
        listener._handle_message(event, MagicMock(), _logger)
        listener._summarizer.classify_message.assert_not_called()

    def test_resolves_channel_name_lazily(self) -> None:
        listener = _make_listener()
        listener._channel_names = {}  # empty cache
        listener._app.client.conversations_info.return_value = {
            "channel": {"name": "resolved-alerts"}
        }
        listener._summarizer.classify_message.return_value = AlertClassification(
            is_alert=False, severity="info"
        )

        event = {"channel": "C999", "text": "hello", "ts": "1.1"}
        listener._handle_message(event, MagicMock(), _logger)

        listener._app.client.conversations_info.assert_called_once_with(channel="C999")
        assert listener._channel_names["C999"] == "resolved-alerts"

    def test_caches_resolved_channel_name(self) -> None:
        listener = _make_listener()
        listener._channel_names = {}
        listener._app.client.conversations_info.return_value = {
            "channel": {"name": "alerts"}
        }
        listener._summarizer.classify_message.return_value = AlertClassification(
            is_alert=False, severity="info"
        )

        # Two messages from the same channel
        event = {"channel": "C001", "text": "msg1", "ts": "1.1"}
        listener._handle_message(event, MagicMock(), _logger)
        listener._handle_message(event, MagicMock(), _logger)

        # Only one API call — second was served from cache
        listener._app.client.conversations_info.assert_called_once()

    def test_classifies_and_submits_alert(self) -> None:
        listener = _make_listener()
        listener._summarizer.classify_message.return_value = AlertClassification(
            is_alert=True,
            severity="critical",
            context="mainnet",
            namespaces=["prover"],
            pod_names=["prover-0"],
            service_names=[],
        )

        event = {"channel": "C001", "text": "prover-0 is down", "ts": "123.456"}
        listener._handle_message(event, MagicMock(), _logger)

        listener._orchestrator.submit.assert_called_once()
        call_args = listener._orchestrator.submit.call_args
        alert_event = call_args[0][1]
        assert alert_event.channel_id == "C001"
        assert alert_event.message_id == "123.456"
        assert alert_event.channel_name == "alerts"
        assert alert_event.text == "prover-0 is down"

    def test_skips_non_alert(self) -> None:
        listener = _make_listener()
        listener._summarizer.classify_message.return_value = AlertClassification(
            is_alert=False, severity="info"
        )

        event = {"channel": "C001", "text": "hey lunch?", "ts": "1.1"}
        listener._handle_message(event, MagicMock(), _logger)

        listener._orchestrator.submit.assert_not_called()

    def test_classification_error_reports_to_chat(self) -> None:
        listener = _make_listener()
        listener._summarizer.classify_message.side_effect = RuntimeError("API down")

        event = {"channel": "C001", "text": "prover alert", "ts": "1.1"}
        listener._handle_message(event, MagicMock(), _logger)

        listener._orchestrator.submit.assert_not_called()
        listener._notifier.post_reply.assert_called_once()
        reply_text = listener._notifier.post_reply.call_args[0][1]
        assert "Failed to classify" in reply_text
        assert "API down" not in reply_text  # raw exception not leaked

    def test_detail_request_expands(self) -> None:
        listener = _make_listener()
        event = {
            "channel": "C001",
            "text": "details",
            "ts": "2.2",
            "thread_ts": "1.1",
        }
        listener._handle_message(event, MagicMock(), _logger)

        listener._orchestrator.expand.assert_called_once_with(
            "C001", "1.1", listener._notifier
        )
        listener._summarizer.classify_message.assert_not_called()

    def test_detail_request_case_insensitive(self) -> None:
        listener = _make_listener()
        event = {
            "channel": "C001",
            "text": "DETAILS",
            "ts": "2.2",
            "thread_ts": "1.1",
        }
        listener._handle_message(event, MagicMock(), _logger)

        listener._orchestrator.expand.assert_called_once()

    def test_detail_request_with_punctuation_expands(self) -> None:
        listener = _make_listener()
        event = {
            "channel": "C001",
            "text": "details?",
            "ts": "2.2",
            "thread_ts": "1.1",
        }
        listener._handle_message(event, MagicMock(), _logger)

        listener._orchestrator.expand.assert_called_once_with(
            "C001", "1.1", listener._notifier
        )

    def test_failed_channel_lookup_not_cached(self) -> None:
        listener = _make_listener()
        listener._channel_names = {}
        listener._app.client.conversations_info.side_effect = RuntimeError("API error")
        listener._summarizer.classify_message.return_value = AlertClassification(
            is_alert=False, severity="info"
        )

        event = {"channel": "C999", "text": "hello", "ts": "1.1"}
        listener._handle_message(event, MagicMock(), _logger)

        # Failed lookup should NOT be cached
        assert "C999" not in listener._channel_names

    def test_non_detail_thread_reply_ignored(self) -> None:
        """Thread replies that are not detail keywords should not trigger expand."""
        listener = _make_listener()
        listener._summarizer.classify_message.return_value = AlertClassification(
            is_alert=False, severity="info"
        )

        event = {
            "channel": "C001",
            "text": "thanks for looking into this",
            "ts": "2.2",
            "thread_ts": "1.1",
        }
        listener._handle_message(event, MagicMock(), _logger)

        listener._orchestrator.expand.assert_not_called()
