from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS investigations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    channel_name TEXT NOT NULL,
    alert_text TEXT NOT NULL,
    severity TEXT NOT NULL,
    context TEXT NOT NULL,
    namespaces TEXT NOT NULL,
    pod_names TEXT NOT NULL,
    service_names TEXT NOT NULL,
    unhealthy_pods TEXT NOT NULL,
    summary TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_channel ON investigations(channel_name);
CREATE INDEX IF NOT EXISTS idx_timestamp ON investigations(timestamp);
"""


@dataclass
class InvestigationRecord:
    timestamp: str
    channel_name: str
    alert_text: str
    severity: str
    context: str
    namespaces: list[str]
    pod_names: list[str]
    service_names: list[str]
    unhealthy_pods: list[str]
    summary: str


class Memory:
    def __init__(self, db_path: str | Path = "onclaw_memory.db") -> None:
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def store(self, record: InvestigationRecord) -> None:
        """Store a completed investigation."""
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO investigations
                   (timestamp, channel_name, alert_text, severity, context,
                    namespaces, pod_names, service_names, unhealthy_pods, summary)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.timestamp,
                    record.channel_name,
                    record.alert_text,
                    record.severity,
                    record.context,
                    json.dumps(record.namespaces),
                    json.dumps(record.pod_names),
                    json.dumps(record.service_names),
                    json.dumps(record.unhealthy_pods),
                    record.summary,
                ),
            )
        logger.debug("Stored investigation record for '%s'", record.channel_name)

    def search(self, alert_text: str, limit: int = 5) -> list[InvestigationRecord]:
        """Find past investigations similar to the given alert.

        Uses keyword matching — extracts significant words from the alert
        and searches for records containing any of them.
        """
        keywords = self._extract_keywords(alert_text)
        if not keywords:
            return self._get_recent(limit)

        # Build a query that matches any keyword in alert_text, pod_names, or service_names
        conditions = []
        params: list[str] = []
        for kw in keywords:
            conditions.append(
                "(alert_text LIKE ? OR pod_names LIKE ? OR service_names LIKE ? "
                "OR unhealthy_pods LIKE ?)"
            )
            like = f"%{kw}%"
            params.extend([like, like, like, like])

        where = " OR ".join(conditions)
        query = f"""
            SELECT timestamp, channel_name, alert_text, severity, context,
                   namespaces, pod_names, service_names, unhealthy_pods, summary
            FROM investigations
            WHERE {where}
            ORDER BY timestamp DESC
            LIMIT ?
        """
        params.append(str(limit))

        with self._lock, self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        return [self._row_to_record(row) for row in rows]

    def _get_recent(self, limit: int) -> list[InvestigationRecord]:
        query = """
            SELECT timestamp, channel_name, alert_text, severity, context,
                   namespaces, pod_names, service_names, unhealthy_pods, summary
            FROM investigations
            ORDER BY timestamp DESC
            LIMIT ?
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(query, (limit,)).fetchall()
        return [self._row_to_record(row) for row in rows]

    @staticmethod
    def _extract_keywords(text: str) -> list[str]:
        """Extract significant words for search (skip common/short words)."""
        stop_words = {
            "the", "is", "are", "was", "were", "has", "have", "had", "not", "for",
            "and", "but", "or", "this", "that", "with", "from", "been", "being",
            "status", "firing", "description", "summary", "alert", "last", "any",
            "new", "on", "in", "to", "of", "a", "an", "no", "at", "by",
        }
        words = text.lower().split()
        # Keep words that are 3+ chars, not stop words, and look like identifiers
        return [
            w.strip(".:,;!?()[]{}\"'")
            for w in words
            if len(w) >= 3 and w.lower().strip(".:,;!?()[]{}\"'") not in stop_words
        ][:10]  # Cap at 10 keywords

    @staticmethod
    def _row_to_record(row: tuple) -> InvestigationRecord:
        return InvestigationRecord(
            timestamp=row[0],
            channel_name=row[1],
            alert_text=row[2],
            severity=row[3],
            context=row[4],
            namespaces=json.loads(row[5]),
            pod_names=json.loads(row[6]),
            service_names=json.loads(row[7]),
            unhealthy_pods=json.loads(row[8]),
            summary=row[9],
        )


def format_past_investigations(records: list[InvestigationRecord]) -> str:
    """Format past investigations for inclusion in the AI prompt."""
    if not records:
        return ""

    lines = ["*Past similar investigations:*"]
    for r in records:
        lines.append(
            f"\n[{r.timestamp}] #{r.channel_name} — severity: {r.severity}"
            f"\n  Alert: {r.alert_text[:200]}"
            f"\n  Context: {r.context}, Namespaces: {', '.join(r.namespaces)}"
            f"\n  Unhealthy pods: {', '.join(r.unhealthy_pods) or 'none'}"
            f"\n  Summary: {r.summary[:300]}"
        )
    return "\n".join(lines)
