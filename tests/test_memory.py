from __future__ import annotations

import tempfile
from pathlib import Path

from onclaw.memory import InvestigationRecord, Memory, format_past_investigations


def _make_record(**overrides) -> InvestigationRecord:
    defaults = dict(
        timestamp="2025-01-15T10:30:00Z",
        channel_name="alerts-l2",
        alert_text="L2 head did not move for 10 minutes",
        severity="critical",
        context="mainnet-cluster",
        namespaces=["l2-node"],
        pod_names=["l2-node-archived-1"],
        service_names=["l2-node-archived"],
        unhealthy_pods=["l2-node-archived-1"],
        summary="Root cause: l2-node-archived-1 lost peer connections.",
    )
    defaults.update(overrides)
    return InvestigationRecord(**defaults)


class TestMemory:
    def test_store_and_search(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        memory = Memory(db_path=db)

        record = _make_record()
        memory.store(record)

        results = memory.search("L2 head did not move")
        assert len(results) == 1
        assert results[0].alert_text == record.alert_text
        assert results[0].namespaces == ["l2-node"]
        assert results[0].unhealthy_pods == ["l2-node-archived-1"]

    def test_search_by_pod_name(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        memory = Memory(db_path=db)

        memory.store(_make_record())

        results = memory.search("something about l2-node-archived-1")
        assert len(results) == 1

    def test_search_no_match(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        memory = Memory(db_path=db)

        memory.store(_make_record())

        results = memory.search("completely unrelated bridge issue xyz123")
        assert len(results) == 0

    def test_search_returns_recent_on_empty_keywords(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        memory = Memory(db_path=db)

        memory.store(_make_record())

        results = memory.search("", limit=5)
        assert len(results) == 1

    def test_multiple_records_ordered_by_time(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        memory = Memory(db_path=db)

        memory.store(_make_record(timestamp="2025-01-15T08:00:00Z", summary="First"))
        memory.store(_make_record(timestamp="2025-01-15T10:00:00Z", summary="Second"))
        memory.store(_make_record(timestamp="2025-01-15T12:00:00Z", summary="Third"))

        results = memory.search("L2 head", limit=2)
        assert len(results) == 2
        assert results[0].summary == "Third"   # Most recent first
        assert results[1].summary == "Second"

    def test_search_across_different_alerts(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        memory = Memory(db_path=db)

        memory.store(_make_record(
            alert_text="prover-0 not proving blocks",
            pod_names=["prover-0"],
            service_names=["prover"],
        ))
        memory.store(_make_record(
            alert_text="L2 head did not move",
            pod_names=["l2-node-0"],
            service_names=["l2-node"],
        ))

        results = memory.search("prover not proving")
        assert len(results) == 1
        assert "prover" in results[0].alert_text


class TestFormatPastInvestigations:
    def test_formats_records(self) -> None:
        records = [_make_record()]
        formatted = format_past_investigations(records)

        assert "Past similar investigations" in formatted
        assert "l2-node-archived-1" in formatted
        assert "Root cause" in formatted

    def test_empty_records(self) -> None:
        assert format_past_investigations([]) == ""
