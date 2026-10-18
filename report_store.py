"""Read-only SQLite index built from versioned, hash-verified text fixtures."""
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from threading import RLock

METRICS = {
    "nav_usd": "NAV USD",
    "commitments_usd": "Commitments USD",
    "distributions_usd": "Distributions USD",
}


class ReportStore:
    def __init__(self, directory: Path):
        self.lock = RLock()
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        manifest_bytes = (directory / "manifest.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        if manifest.get("synthetic") is not True:
            raise ValueError("Only the synthetic fixture collection is supported")
        self.dataset_version = manifest["dataset_version"]
        self.dataset_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        self.connection.executescript("""
            CREATE TABLE reports (
                id TEXT PRIMARY KEY, fund_id TEXT, title TEXT, period TEXT,
                body TEXT, sha256 TEXT, UNIQUE(fund_id, period)
            );
            CREATE TABLE metrics (
                document_id TEXT, metric TEXT, value INTEGER, unit TEXT,
                PRIMARY KEY(document_id, metric)
            );
        """)
        for item in manifest["files"]:
            filename = item["file"]
            if not re.fullmatch(r"[a-z0-9_]+\.txt", filename):
                raise ValueError("Invalid fixture filename")
            fixture_path = directory / filename
            if fixture_path.is_symlink() or fixture_path.resolve().parent != directory.resolve():
                raise ValueError("Fixture must be a regular file inside the fixture directory")
            raw = fixture_path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if digest != item["sha256"]:
                raise ValueError(f"Fixture hash mismatch: {filename}")
            body = raw.decode("utf-8")
            if not body.startswith("SYNTHETIC REPORT - LOCAL PROTOTYPE ONLY\n"):
                raise ValueError("Missing synthetic document label")
            fields = dict(line.split(": ", 1) for line in body.splitlines() if ": " in line)
            if fields["Currency"] != "USD":
                raise ValueError("Unsupported fixture currency")
            self.connection.execute(
                "INSERT INTO reports VALUES (?, ?, ?, ?, ?, ?)",
                (fields["Document ID"], fields["Fund ID"], fields["Fund"], fields["Period"], body, digest),
            )
            for key, label in METRICS.items():
                value = int(fields[label])
                if value < 0:
                    raise ValueError("Negative fixture amount")
                self.connection.execute("INSERT INTO metrics VALUES (?, ?, ?, ?)",
                                        (fields["Document ID"], key, value, "USD"))
        self.connection.commit()
        self.connection.execute("PRAGMA query_only = ON")

    def rows(self, sql: str, parameters: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.connection.execute(sql, parameters).fetchall()

    def envelope(self, **data) -> dict:
        return {"synthetic": True, "dataset_version": self.dataset_version,
                "dataset_sha256": self.dataset_sha256, **data}

    @staticmethod
    def source(row: sqlite3.Row) -> dict:
        return {"document_id": row["id"], "fund_id": row["fund_id"],
                "period": row["period"], "uri": f"synthetic://reports/{row['id']}",
                "sha256": row["sha256"]}

    def search(self, query: str, limit: int) -> dict:
        tokens = re.findall(r"[a-z0-9]+", query.lower())
        if not tokens:
            raise ValueError("Enter at least one searchable word")
        clauses = " AND ".join("lower(body) LIKE ?" for _ in tokens)
        rows = self.rows(f"SELECT * FROM reports WHERE {clauses} ORDER BY id LIMIT ?",
                         tuple(f"%{token}%" for token in tokens) + (limit,))
        return self.envelope(results=[{"title": row["title"], "source": self.source(row),
                                       "excerpt": row["body"][:450]} for row in rows])

    def read(self, document_id: str) -> dict:
        rows = self.rows("SELECT * FROM reports WHERE id = ?", (document_id,))
        if not rows:
            raise ValueError("Unknown document ID; use search_reports first")
        row = rows[0]
        return self.envelope(title=row["title"], text=row["body"], source=self.source(row))

    def metric(self, fund_id: str, period: str, metric: str) -> dict:
        if metric not in METRICS:
            raise ValueError("Unsupported metric")
        rows = self.rows("""SELECT r.*, m.metric, m.value, m.unit FROM reports r
            JOIN metrics m ON r.id = m.document_id
            WHERE r.fund_id = ? AND r.period = ? AND m.metric = ?""", (fund_id, period, metric))
        if not rows:
            raise ValueError("No report for this fund and period")
        row = rows[0]
        return self.envelope(metric=metric, value=row["value"], unit=row["unit"], source=self.source(row))

    def compare(self, fund_id: str, start_period: str, end_period: str, metric: str) -> dict:
        if start_period >= end_period:
            raise ValueError("start_period must precede end_period")
        start = self.metric(fund_id, start_period, metric)
        end = self.metric(fund_id, end_period, metric)
        if start["unit"] != end["unit"]:
            raise ValueError("Cannot compare incompatible units")
        delta = end["value"] - start["value"]
        percent = None if start["value"] == 0 else str(
            (Decimal(delta) * 100 / Decimal(start["value"])).quantize(Decimal("0.01")))
        return self.envelope(metric=metric, start_value=start["value"], end_value=end["value"],
                             delta=delta, percent_change=percent, unit=start["unit"],
                             percent_change_note="undefined for zero baseline" if percent is None else None,
                             sources=[start["source"], end["source"]],
                             interpretation="Change in a reported amount, not a total investment return.")
