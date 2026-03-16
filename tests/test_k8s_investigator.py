from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from onclaw.k8s_investigator import (
    K8sInvestigator,
    _compute_age,
    _extract_pod_info,
    _is_unhealthy,
)


def _make_pod(
    name: str = "test-pod",
    namespace: str = "default",
    phase: str = "Running",
    ready: bool = True,
    restart_count: int = 0,
    waiting_reason: str | None = None,
    terminated_reason: str | None = None,
) -> MagicMock:
    """Create a mock V1Pod."""
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.namespace = namespace
    pod.metadata.creation_timestamp = datetime.now(timezone.utc)

    pod.status.phase = phase

    container_status = MagicMock()
    container_status.name = "main"
    container_status.ready = ready
    container_status.restart_count = restart_count

    container_status.state.running = MagicMock() if not waiting_reason and not terminated_reason else None
    container_status.state.waiting = None
    container_status.state.terminated = None

    if waiting_reason:
        container_status.state.running = None
        container_status.state.waiting = MagicMock()
        container_status.state.waiting.reason = waiting_reason
    if terminated_reason:
        container_status.state.running = None
        container_status.state.terminated = MagicMock()
        container_status.state.terminated.reason = terminated_reason

    pod.status.container_statuses = [container_status]
    return pod


class TestComputeAge:
    def test_none_timestamp(self) -> None:
        assert _compute_age(None) == "unknown"

    def test_recent_timestamp(self) -> None:
        ts = datetime.now(timezone.utc)
        age = _compute_age(ts)
        assert age.endswith("s")


class TestIsUnhealthy:
    def test_healthy_pod(self) -> None:
        pod = _make_pod(ready=True, restart_count=0)
        assert _is_unhealthy(pod) is False

    def test_crashloop_pod(self) -> None:
        pod = _make_pod(ready=False, waiting_reason="CrashLoopBackOff")
        assert _is_unhealthy(pod) is True

    def test_oomkilled_pod(self) -> None:
        pod = _make_pod(ready=False, terminated_reason="OOMKilled")
        assert _is_unhealthy(pod) is True

    def test_high_restart_count(self) -> None:
        pod = _make_pod(restart_count=5, ready=True)
        assert _is_unhealthy(pod) is True

    def test_not_ready_pod(self) -> None:
        pod = _make_pod(ready=False)
        assert _is_unhealthy(pod) is True

    def test_failed_phase(self) -> None:
        pod = _make_pod(phase="Failed")
        assert _is_unhealthy(pod) is True


class TestExtractPodInfo:
    def test_extracts_basic_info(self) -> None:
        pod = _make_pod(name="my-pod", namespace="prod", restart_count=3)
        info = _extract_pod_info(pod)

        assert info.name == "my-pod"
        assert info.namespace == "prod"
        assert info.restart_count == 3
        assert len(info.container_statuses) == 1
        assert info.container_statuses[0]["name"] == "main"


class TestMatchesTargets:
    def test_matches_pod_name(self) -> None:
        from onclaw.k8s_investigator import PodInfo

        pod = PodInfo(
            name="prover-0", namespace="taiko", status="Running",
            restart_count=0, ready=True, age="1d", container_statuses=[],
        )
        assert K8sInvestigator._matches_targets(pod, ["prover-0"], []) is True

    def test_matches_partial_pod_name(self) -> None:
        from onclaw.k8s_investigator import PodInfo

        pod = PodInfo(
            name="prover-deployment-abc123", namespace="taiko", status="Running",
            restart_count=0, ready=True, age="1d", container_statuses=[],
        )
        assert K8sInvestigator._matches_targets(pod, ["prover"], []) is True

    def test_matches_service_name(self) -> None:
        from onclaw.k8s_investigator import PodInfo

        pod = PodInfo(
            name="sequencer-7b4f9c-xyz", namespace="default", status="Running",
            restart_count=0, ready=True, age="1d", container_statuses=[],
        )
        assert K8sInvestigator._matches_targets(pod, [], ["sequencer"]) is True

    def test_matches_qualified_name(self) -> None:
        from onclaw.k8s_investigator import PodInfo

        pod = PodInfo(
            name="api-0", namespace="ns-a", status="Running",
            restart_count=0, ready=True, age="1d", container_statuses=[],
        )
        # Exact namespace match
        assert K8sInvestigator._matches_targets(pod, ["ns-a/api-0"], []) is True
        # Wrong namespace — should NOT match
        assert K8sInvestigator._matches_targets(pod, ["ns-b/api-0"], []) is False

    def test_no_match(self) -> None:
        from onclaw.k8s_investigator import PodInfo

        pod = PodInfo(
            name="nginx-abc123", namespace="default", status="Running",
            restart_count=0, ready=True, age="1d", container_statuses=[],
        )
        assert K8sInvestigator._matches_targets(pod, ["prover"], ["sequencer"]) is False


class TestK8sInvestigator:
    @patch("onclaw.k8s_investigator.k8s_config")
    @patch("onclaw.k8s_investigator.client.CoreV1Api")
    @patch("onclaw.k8s_investigator.client.ApiClient")
    def test_investigate_collects_pods_and_events(
        self,
        mock_api_client_cls: MagicMock,
        mock_core_api_cls: MagicMock,
        mock_k8s_config: MagicMock,
    ) -> None:
        mock_api = mock_core_api_cls.return_value

        # Setup mock pods
        healthy_pod = _make_pod(name="healthy", namespace="test-ns")
        unhealthy_pod = _make_pod(
            name="crasher", namespace="test-ns",
            ready=False, waiting_reason="CrashLoopBackOff"
        )
        pod_list = MagicMock()
        pod_list.items = [healthy_pod, unhealthy_pod]
        mock_api.list_namespaced_pod.return_value = pod_list

        # Setup mock events
        event = MagicMock()
        event.last_timestamp = datetime.now(timezone.utc)
        event.event_time = None
        event.involved_object.kind = "Pod"
        event.involved_object.name = "crasher"
        event.reason = "BackOff"
        event.message = "Back-off restarting failed container"
        event.count = 5
        event_list = MagicMock()
        event_list.items = [event]
        mock_api.list_namespaced_event.return_value = event_list

        # Setup mock logs
        mock_api.read_namespaced_pod_log.return_value = "Error: connection refused"

        investigator = K8sInvestigator(kubeconfig_path="/tmp/fake")

        data = investigator.investigate(
            context="test-ctx",
            namespaces=["test-ns"],
            max_log_lines=50,
        )

        # Only unhealthy pods included (no targets specified)
        assert len(data.pods) == 1
        assert data.pods[0].name == "crasher"
        assert len(data.unhealthy_pods) == 1
        assert data.unhealthy_pods[0].name == "crasher"
        assert data.total_pod_count == 2  # both pods counted
        assert data.context_used == "test-ctx"
        assert len(data.events) == 1
        assert len(data.pod_logs) >= 1
        assert data.errors == []
