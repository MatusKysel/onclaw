from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from onclaw.ai_summarizer import AlertClassification
from onclaw.config import OnclawConfig
from onclaw.investigation import InvestigationOrchestrator
from onclaw.k8s_investigator import InvestigationData
from onclaw.memory import Memory
from onclaw.notifier import AlertEvent, Notifier


class MockNotifier(Notifier):
    """Test notifier that records calls."""

    def __init__(self) -> None:
        self.investigating_calls: list[AlertEvent] = []
        self.complete_calls: list[AlertEvent] = []
        self.failed_calls: list[AlertEvent] = []
        self.replies: list[tuple[AlertEvent, str]] = []

    def indicate_investigating(self, event: AlertEvent) -> None:
        self.investigating_calls.append(event)

    def indicate_complete(self, event: AlertEvent) -> None:
        self.complete_calls.append(event)

    def indicate_failed(self, event: AlertEvent) -> None:
        self.failed_calls.append(event)

    def post_reply(self, event: AlertEvent, text: str) -> str | None:
        self.replies.append((event, text))
        return f"reply-{len(self.replies)}"


def _make_orchestrator(
    tmp_path: Path | None = None,
) -> tuple[InvestigationOrchestrator, MagicMock, MagicMock, MockNotifier, Memory]:
    config = OnclawConfig(
        slack_app_token="xapp-test",
        slack_bot_token="xoxb-test",
        anthropic_api_key="sk-test",
        kubeconfig_path="/tmp/fake",
        max_concurrent_investigations=1,
    )

    mock_k8s = MagicMock()
    mock_k8s.list_pod_names.return_value = ["prover/prover-0", "prover/prover-1"]
    mock_k8s.investigate.return_value = InvestigationData(
        timestamp="2025-01-15T10:30:00Z",
        context_used="mainnet",
        namespaces_checked=["prover"],
        total_pod_count=2,
    )

    mock_summarizer = MagicMock()
    mock_summarizer.select_pods.return_value = ["prover/prover-0"]
    mock_summarizer.summarize.return_value = "Summary: prover-0 stalled due to OOM"

    notifier = MockNotifier()

    db_path = (tmp_path / "test.db") if tmp_path else Path(tempfile.mktemp(suffix=".db"))
    memory = Memory(db_path=db_path)

    orchestrator = InvestigationOrchestrator(
        config=config,
        k8s=mock_k8s,
        summarizer=mock_summarizer,
        memory=memory,
    )

    return orchestrator, mock_k8s, mock_summarizer, notifier, memory


def _make_event(
    channel_id: str = "C001",
    message_id: str = "1234567890.000001",
    text: str = "prover-0 not proving",
) -> AlertEvent:
    return AlertEvent(
        channel_id=channel_id,
        message_id=message_id,
        channel_name="alerts-l2",
        text=text,
    )


class TestInvestigationOrchestrator:
    def test_full_pipeline(self, tmp_path: Path) -> None:
        orchestrator, mock_k8s, mock_summarizer, notifier, memory = _make_orchestrator(tmp_path)

        classification = AlertClassification(
            is_alert=True,
            severity="critical",
            context="mainnet",
            namespaces=["prover"],
            pod_names=["prover-0"],
            service_names=["prover"],
        )
        event = _make_event()

        orchestrator._run(classification, event, notifier, "C001:1234567890.000001")

        # Verify K8s was called with AI-selected context and namespaces
        mock_k8s.investigate.assert_called_once()
        call_kwargs = mock_k8s.investigate.call_args.kwargs
        assert call_kwargs["context"] == "mainnet"
        assert call_kwargs["namespaces"] == ["prover"]
        assert "prover-0" in call_kwargs["target_pod_names"]

        # Classification had specific pod names — AI selection should be skipped
        mock_summarizer.select_pods.assert_not_called()
        mock_k8s.list_pod_names.assert_not_called()

        # Verify reply was posted
        assert len(notifier.replies) == 1
        assert notifier.replies[0][1] == "Summary: prover-0 stalled due to OOM"

        # Verify status flow: investigating -> complete
        assert len(notifier.investigating_calls) == 1
        assert len(notifier.complete_calls) == 1
        assert len(notifier.failed_calls) == 0

    def test_stores_investigation_in_memory(self, tmp_path: Path) -> None:
        orchestrator, _, _, notifier, memory = _make_orchestrator(tmp_path)

        classification = AlertClassification(
            is_alert=True,
            severity="critical",
            context="mainnet",
            namespaces=["prover"],
            pod_names=["prover-0"],
            service_names=["prover"],
        )
        event = _make_event(message_id="111.222", text="prover-0 not proving blocks")

        orchestrator._run(classification, event, notifier, "C001:111.222")

        # Verify the investigation was stored
        records = memory.search("prover")
        assert len(records) == 1
        assert records[0].alert_text == "prover-0 not proving blocks"
        assert records[0].channel_name == "alerts-l2"

    def test_stores_ai_selected_targets_in_memory(self, tmp_path: Path) -> None:
        orchestrator, _, _, notifier, memory = _make_orchestrator(tmp_path)

        classification = AlertClassification(
            is_alert=True,
            severity="critical",
            context="mainnet",
            namespaces=["prover"],
            pod_names=[],
            service_names=[],
        )
        event = _make_event(message_id="333.444", text="prover is not proving blocks")

        orchestrator._run(classification, event, notifier, "C001:333.444")

        records = memory.search("prover is not proving")
        assert len(records) == 1
        assert records[0].pod_names == ["prover/prover-0"]

    def test_passes_past_investigations_to_summarizer(self, tmp_path: Path) -> None:
        orchestrator, _, mock_summarizer, notifier, memory = _make_orchestrator(tmp_path)

        # Store a past investigation
        from onclaw.memory import InvestigationRecord
        memory.store(InvestigationRecord(
            timestamp="2025-01-14T10:00:00Z",
            channel_name="alerts-l2",
            alert_text="prover-0 not proving blocks",
            severity="critical",
            context="mainnet",
            namespaces=["prover"],
            pod_names=["prover-0"],
            service_names=["prover"],
            unhealthy_pods=["prover-0"],
            summary="Previous: prover-0 OOM killed, fixed by scaling memory.",
        ))

        classification = AlertClassification(
            is_alert=True,
            severity="critical",
            context="mainnet",
            namespaces=["prover"],
            pod_names=["prover-0"],
            service_names=["prover"],
        )
        event = _make_event(message_id="222.333", text="prover-0 not proving again")

        orchestrator._run(classification, event, notifier, "C001:222.333")

        # Verify summarizer received past investigation context
        call_kwargs = mock_summarizer.summarize.call_args.kwargs
        assert "Past similar investigations" in call_kwargs["past_investigations"]
        assert "prover-0 OOM killed" in call_kwargs["past_investigations"]

    def test_expand_posts_detailed_summary(self, tmp_path: Path) -> None:
        orchestrator, mock_k8s, mock_summarizer, notifier, memory = _make_orchestrator(tmp_path)

        # First run a short summary investigation
        mock_summarizer.summarize.return_value = "Short: prover stalled"

        classification = AlertClassification(
            is_alert=True,
            severity="critical",
            context="mainnet",
            namespaces=["prover"],
            pod_names=["prover-0"],
            service_names=["prover"],
        )
        event = _make_event()
        orchestrator._run(classification, event, notifier, "C001:1234567890.000001")

        # Cached under both reply ID and parent message ID
        assert len(orchestrator._cache) == 2

        # Now request details
        mock_summarizer.summarize.return_value = "Detailed: prover-0 OOM killed, logs show..."
        reply_id = list(orchestrator._cache.keys())[0].split(":")[1]

        notifier2 = MockNotifier()
        orchestrator._run_expand(
            orchestrator._cache[f"C001:{reply_id}"], notifier2
        )

        assert len(notifier2.replies) == 1
        assert "Detailed" in notifier2.replies[0][1]

        # Verify summarize was called with detailed=True
        last_call = mock_summarizer.summarize.call_args
        assert last_call.kwargs.get("detailed") is True

    def test_memory_skips_ai_pod_selection(self, tmp_path: Path) -> None:
        """When memory has a past investigation for the same channel+context,
        pod selection should be reused instead of calling AI."""
        orchestrator, mock_k8s, mock_summarizer, notifier, memory = _make_orchestrator(tmp_path)

        # Store a past investigation with known pod targets
        from onclaw.memory import InvestigationRecord
        memory.store(InvestigationRecord(
            timestamp="2025-01-14T10:00:00Z",
            channel_name="alerts-l2",
            alert_text="something weird with the network",
            severity="warning",
            context="mainnet",
            namespaces=["prover"],
            pod_names=["prover/l2-node-0"],
            service_names=[],
            unhealthy_pods=[],
            summary="Past investigation.",
        ))

        # New alert without specific pod names or service names
        classification = AlertClassification(
            is_alert=True,
            severity="warning",
            context="mainnet",
            namespaces=["prover"],
            pod_names=[],
            service_names=[],
        )
        event = _make_event(
            message_id="999.888",
            text="something weird with the network again",
        )

        orchestrator._run(classification, event, notifier, "C001:999.888")

        # Memory had a match — AI selection and pod listing should be skipped
        mock_summarizer.select_pods.assert_not_called()
        mock_k8s.list_pod_names.assert_not_called()

        # But investigate + summarize should still run
        mock_k8s.investigate.assert_called_once()
        mock_summarizer.summarize.assert_called_once()

    def test_ai_selection_fallback(self, tmp_path: Path) -> None:
        """When neither classification nor memory has targets, AI selection runs."""
        orchestrator, mock_k8s, mock_summarizer, notifier, _ = _make_orchestrator(tmp_path)

        classification = AlertClassification(
            is_alert=True,
            severity="warning",
            context="mainnet",
            namespaces=["prover"],
            pod_names=[],
            service_names=[],
        )
        event = _make_event(message_id="777.666", text="completely new issue")

        orchestrator._run(classification, event, notifier, "C001:777.666")

        # No memory match, no classification targets — AI selection should run
        mock_k8s.list_pod_names.assert_called_once()
        mock_summarizer.select_pods.assert_called_once()

    def test_memory_targets_from_other_namespace_do_not_skip_ai_selection(
        self, tmp_path: Path
    ) -> None:
        orchestrator, mock_k8s, mock_summarizer, notifier, memory = _make_orchestrator(tmp_path)

        from onclaw.memory import InvestigationRecord

        memory.store(InvestigationRecord(
            timestamp="2025-01-14T10:00:00Z",
            channel_name="alerts-l2",
            alert_text="weird prover issue",
            severity="warning",
            context="mainnet",
            namespaces=["archive"],
            pod_names=["archive/prover-0"],
            service_names=[],
            unhealthy_pods=[],
            summary="Past investigation in a different namespace.",
        ))

        mock_summarizer.select_pods.return_value = ["prover/prover-1"]

        classification = AlertClassification(
            is_alert=True,
            severity="warning",
            context="mainnet",
            namespaces=["prover"],
            pod_names=[],
            service_names=[],
        )
        event = _make_event(message_id="555.444", text="weird prover issue again")

        orchestrator._run(classification, event, notifier, "C001:555.444")

        mock_k8s.list_pod_names.assert_called_once()
        mock_summarizer.select_pods.assert_called_once()
        call_kwargs = mock_k8s.investigate.call_args.kwargs
        assert call_kwargs["target_pod_names"] == ["prover/prover-1"]

    def test_follow_up_investigation(self, tmp_path: Path) -> None:
        """AI suggests follow-up pods based on logs → those pods get investigated too."""
        orchestrator, mock_k8s, mock_summarizer, notifier, _ = _make_orchestrator(tmp_path)

        # Override config to enable follow-ups
        orchestrator._config.max_follow_up_depth = 2

        from onclaw.k8s_investigator import PodInfo, PodLogSnippet

        # Initial investigation returns data with logs mentioning another pod
        initial_data = InvestigationData(
            timestamp="2025-01-15T10:30:00Z",
            context_used="mainnet",
            namespaces_checked=["app"],
            total_pod_count=5,
            pods=[PodInfo(
                name="api-0", namespace="app", status="Running",
                restart_count=0, ready=True, age="5d", container_statuses=[{"name": "main"}],
            )],
            pod_logs=[PodLogSnippet(
                pod_name="api-0", namespace="app", container_name="main",
                log_lines="ERROR: connection refused to redis-master-0:6379",
            )],
        )
        # Follow-up investigation returns redis data
        followup_data = InvestigationData(
            timestamp="2025-01-15T10:31:00Z",
            context_used="mainnet",
            namespaces_checked=["app"],
            total_pod_count=5,
            pods=[PodInfo(
                name="redis-master-0", namespace="app", status="Running",
                restart_count=3, ready=True, age="5d", container_statuses=[{"name": "redis"}],
            )],
            pod_logs=[PodLogSnippet(
                pod_name="redis-master-0", namespace="app", container_name="redis",
                log_lines="OOM: memory limit exceeded",
            )],
        )

        mock_k8s.investigate.side_effect = [initial_data, followup_data]
        mock_k8s.list_pod_names.return_value = [
            "app/api-0", "app/api-1", "app/redis-master-0", "app/worker-0",
        ]

        # AI suggests redis-master-0 as follow-up, then no more
        mock_summarizer.suggest_follow_up_pods.side_effect = [
            ["app/redis-master-0"],
            [],  # no further follow-ups
        ]

        classification = AlertClassification(
            is_alert=True,
            severity="critical",
            context="mainnet",
            namespaces=["app"],
            pod_names=["api-0"],
            service_names=[],
        )
        event = _make_event(message_id="follow.up", text="api errors")

        orchestrator._run(classification, event, notifier, "C001:follow.up")

        # Two investigate calls: initial + follow-up
        assert mock_k8s.investigate.call_count == 2

        # Follow-up was called with redis pod
        followup_call = mock_k8s.investigate.call_args_list[1]
        assert followup_call.kwargs["target_pod_names"] == ["app/redis-master-0"]

        # AI was asked to suggest follow-ups
        assert mock_summarizer.suggest_follow_up_pods.call_count == 2

        # Summarizer received merged data (both pods' logs)
        summary_call = mock_summarizer.summarize.call_args
        merged = summary_call.kwargs["investigation_data"]
        assert len(merged.pods) == 2
        assert len(merged.pod_logs) == 2

    def test_follow_up_skipped_when_no_logs(self, tmp_path: Path) -> None:
        """Follow-up loop doesn't run when initial investigation has no logs."""
        orchestrator, mock_k8s, mock_summarizer, notifier, _ = _make_orchestrator(tmp_path)
        orchestrator._config.max_follow_up_depth = 2

        # Default mock returns data with no pod_logs
        classification = AlertClassification(
            is_alert=True,
            severity="warning",
            context="mainnet",
            namespaces=["prover"],
            pod_names=["prover-0"],
            service_names=[],
        )
        event = _make_event(message_id="no.logs", text="alert")

        orchestrator._run(classification, event, notifier, "C001:no.logs")

        # Follow-up should not be attempted
        mock_summarizer.suggest_follow_up_pods.assert_not_called()

    def test_memory_only_stores_deliberate_targets(self, tmp_path: Path) -> None:
        """Memory persists AI-selected and follow-up pods, not incidental unhealthy pods."""
        orchestrator, mock_k8s, mock_summarizer, notifier, memory = _make_orchestrator(tmp_path)
        orchestrator._config.max_follow_up_depth = 2

        from onclaw.k8s_investigator import PodInfo, PodLogSnippet

        mock_summarizer.select_pods.return_value = ["app/api-0"]

        initial_target = PodInfo(
            name="api-0", namespace="app", status="Running",
            restart_count=0, ready=True, age="5d", container_statuses=[{"name": "main"}],
        )
        incidental_unhealthy = PodInfo(
            name="crashy-0", namespace="app", status="CrashLoopBackOff",
            restart_count=7, ready=False, age="2d", container_statuses=[{"name": "main"}],
        )
        initial_data = InvestigationData(
            timestamp="2025-01-15T10:30:00Z",
            context_used="mainnet",
            namespaces_checked=["app"],
            total_pod_count=5,
            pods=[initial_target, incidental_unhealthy],
            unhealthy_pods=[incidental_unhealthy],
            pod_logs=[PodLogSnippet(
                pod_name="api-0", namespace="app", container_name="main",
                log_lines="ERROR: connection refused to redis-master-0:6379",
            )],
        )
        followup_data = InvestigationData(
            timestamp="2025-01-15T10:31:00Z",
            context_used="mainnet",
            namespaces_checked=["app"],
            total_pod_count=5,
            pods=[PodInfo(
                name="redis-master-0", namespace="app", status="Running",
                restart_count=1, ready=True, age="5d", container_statuses=[{"name": "redis"}],
            )],
            pod_logs=[PodLogSnippet(
                pod_name="redis-master-0", namespace="app", container_name="redis",
                log_lines="OOM: memory limit exceeded",
            )],
        )

        mock_k8s.investigate.side_effect = [initial_data, followup_data]
        mock_k8s.list_pod_names.return_value = [
            "app/api-0", "app/redis-master-0", "app/crashy-0",
        ]
        mock_summarizer.suggest_follow_up_pods.side_effect = [
            ["app/redis-master-0"],
            [],
        ]

        classification = AlertClassification(
            is_alert=True,
            severity="critical",
            context="mainnet",
            namespaces=["app"],
            pod_names=[],
            service_names=[],
        )
        event = _make_event(message_id="memory.targets", text="api cannot reach redis")

        orchestrator._run(classification, event, notifier, "C001:memory.targets")

        records = memory.search("api cannot reach redis")
        assert len(records) == 1
        assert records[0].pod_names == ["app/api-0", "app/redis-master-0"]

    def test_deduplication(self, tmp_path: Path) -> None:
        orchestrator, _, _, notifier, _ = _make_orchestrator(tmp_path)

        classification = AlertClassification(
            is_alert=True, severity="warning", context="mainnet", namespaces=["prover"]
        )
        event = _make_event(message_id="123.456", text="alert")

        orchestrator._active.add("C001:123.456")
        orchestrator.submit(classification, event, notifier)
