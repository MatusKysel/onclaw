from __future__ import annotations

from unittest.mock import MagicMock, patch

from onclaw.ai_summarizer import AISummarizer, AlertClassification, _format_investigation_data
from onclaw.k8s_investigator import (
    ClusterInfo,
    InvestigationData,
    K8sEvent,
    PodInfo,
    PodLogSnippet,
)


SAMPLE_CLUSTER_INFO = ClusterInfo(
    contexts={
        "mainnet-cluster": ["l2-node", "prover", "sequencer", "monitoring"],
        "testnet-cluster": ["l2-node-testnet", "prover-testnet"],
    }
)


def _make_summarizer(mock_client: MagicMock) -> AISummarizer:
    with patch("onclaw.ai_summarizer.anthropic.Anthropic", return_value=mock_client):
        return AISummarizer(api_key="test-key", model="claude-sonnet-4-20250514", max_tokens=1024)


def _sample_investigation_data() -> InvestigationData:
    unhealthy = PodInfo(
        name="crasher-abc123",
        namespace="sequencer",
        status="Running",
        restart_count=15,
        ready=False,
        age="2h",
        container_statuses=[{"name": "main", "state": "waiting: CrashLoopBackOff"}],
    )
    return InvestigationData(
        timestamp="2025-01-15T10:30:00Z",
        context_used="mainnet-cluster",
        namespaces_checked=["sequencer"],
        pods=[
            PodInfo(
                name="healthy-pod",
                namespace="sequencer",
                status="Running",
                restart_count=0,
                ready=True,
                age="5d",
                container_statuses=[{"name": "main", "state": "running"}],
            ),
            unhealthy,
        ],
        unhealthy_pods=[unhealthy],
        pod_logs=[
            PodLogSnippet(
                pod_name="crasher-abc123",
                namespace="sequencer",
                container_name="main",
                log_lines="panic: runtime error: index out of range\ngoroutine 1 [running]:",
            ),
        ],
        events=[
            K8sEvent(
                namespace="sequencer",
                involved_object="Pod/crasher-abc123",
                reason="BackOff",
                message="Back-off restarting failed container",
                count=42,
                last_timestamp="2025-01-15T10:29:00Z",
            ),
        ],
        errors=[],
    )


class TestFormatInvestigationData:
    def test_includes_all_sections(self) -> None:
        data = _sample_investigation_data()
        formatted = _format_investigation_data(data)

        assert "sequencer" in formatted
        assert "crasher-abc123" in formatted
        assert "CrashLoopBackOff" in formatted
        assert "panic: runtime error" in formatted
        assert "BackOff" in formatted
        assert "mainnet-cluster" in formatted

    def test_empty_data(self) -> None:
        data = InvestigationData(
            timestamp="2025-01-15T10:30:00Z",
            context_used="test-ctx",
            namespaces_checked=["test"],
        )
        formatted = _format_investigation_data(data)
        assert "Total pods in namespace:* 0" in formatted


class TestAISummarizer:
    def test_summarize_calls_claude(self) -> None:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Root cause: OOM in crasher pod")]
        mock_client.messages.create.return_value = mock_response

        summarizer = _make_summarizer(mock_client)
        data = _sample_investigation_data()
        result = summarizer.summarize("L2 head did not move", data)

        assert result == "Root cause: OOM in crasher pod"
        mock_client.messages.create.assert_called_once()

        call_kwargs = mock_client.messages.create.call_args
        messages = call_kwargs.kwargs["messages"]
        assert "L2 head did not move" in messages[0]["content"]

    def test_fallback_on_api_error(self) -> None:
        import anthropic

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = anthropic.APIError(
            message="rate limited",
            request=MagicMock(),
            body=None,
        )

        summarizer = _make_summarizer(mock_client)
        data = _sample_investigation_data()
        result = summarizer.summarize("L2 head did not move", data)

        assert "AI summary unavailable" in result

    def test_classify_detects_alert_and_picks_context(self) -> None:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(
            text='{"is_alert": true, "severity": "critical", '
                 '"context": "mainnet-cluster", "namespaces": ["prover"], '
                 '"pod_names": ["prover-0"], "service_names": ["prover"], '
                 '"keywords": ["proof generation"]}'
        )]
        mock_client.messages.create.return_value = mock_response

        summarizer = _make_summarizer(mock_client)
        result = summarizer.classify_message(
            message_text="Prover Not Proving Blocks - prover-0 stalled",
            channel_name="alerts-l2",
            cluster_info=SAMPLE_CLUSTER_INFO,
        )

        assert result.is_alert is True
        assert result.severity == "critical"
        assert result.context == "mainnet-cluster"
        assert result.namespaces == ["prover"]
        assert result.pod_names == ["prover-0"]

        # Verify the prompt includes cluster inventory and channel name
        call_kwargs = mock_client.messages.create.call_args
        system_prompt = call_kwargs.kwargs["system"]
        assert "mainnet-cluster" in system_prompt
        assert "prover" in system_prompt
        assert "alerts-l2" in system_prompt

    def test_classify_rejects_non_alert(self) -> None:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(
            text='{"is_alert": false, "severity": "info", "context": "", '
                 '"pod_names": [], "service_names": [], "namespaces": [], "keywords": []}'
        )]
        mock_client.messages.create.return_value = mock_response

        summarizer = _make_summarizer(mock_client)
        result = summarizer.classify_message(
            "Deployment completed successfully",
            "alerts-l2",
            SAMPLE_CLUSTER_INFO,
        )
        assert result.is_alert is False

    def test_classify_extracts_from_hostname(self) -> None:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(
            text='{"is_alert": true, "severity": "critical", '
                 '"context": "mainnet-cluster", "namespaces": ["l2-node"], '
                 '"pod_names": ["l2-node-archived-1"], '
                 '"service_names": ["l2-node-archived"], '
                 '"keywords": ["chain head", "not syncing"]}'
        )]
        mock_client.messages.create.return_value = mock_response

        summarizer = _make_summarizer(mock_client)
        result = summarizer.classify_message(
            "L2 Chain Head Not Advancing on nodes: l2-node-archived-1.mainnet:6060",
            "alerts-l2",
            SAMPLE_CLUSTER_INFO,
        )

        assert result.is_alert is True
        assert result.context == "mainnet-cluster"
        assert "l2-node" in result.namespaces
        assert "l2-node-archived-1" in result.pod_names

    def test_classify_returns_non_alert_on_error(self) -> None:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="not valid json")]
        mock_client.messages.create.return_value = mock_response

        summarizer = _make_summarizer(mock_client)
        result = summarizer.classify_message("some msg", "alerts-l2", SAMPLE_CLUSTER_INFO)
        assert result.is_alert is False
