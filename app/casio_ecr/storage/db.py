"""SQLite persistence for Casio ECR sales snapshots.

Each snapshot is one reading of the register's "sales confirm" (job 0014):
cumulative totals since the last Z reset. The PC keeps a timestamped history
so daily/monthly aggregation and trends can be computed locally, independent
of the register's own (volatile, reset-on-Z) memory.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "ecr.db"

# The 9 sales-confirm lines, in order.
LINE_KEYS = ["gross", "net", "caid", "chid", "ckid", "crid1", "crid2", "crid3", "crid4"]


@dataclass
class Snapshot:
    id: int
    captured_at: str  # ISO 8601 local time
    register_serial: str
    gross_qty: int
    gross_amount: int
    net_qty: int
    net_amount: int
    caid_amount: int
    chid_amount: int
    ckid_amount: int
    crid1_amount: int
    crid2_amount: int
    crid3_amount: int
    crid4_amount: int
    labels: str  # JSON of {key: label} as reported by the register


SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at      TEXT NOT NULL,
    register_serial  TEXT NOT NULL DEFAULT '',
    gross_qty        INTEGER NOT NULL DEFAULT 0,
    gross_amount     INTEGER NOT NULL DEFAULT 0,
    net_qty          INTEGER NOT NULL DEFAULT 0,
    net_amount       INTEGER NOT NULL DEFAULT 0,
    caid_amount      INTEGER NOT NULL DEFAULT 0,
    chid_amount      INTEGER NOT NULL DEFAULT 0,
    ckid_amount      INTEGER NOT NULL DEFAULT 0,
    crid1_amount     INTEGER NOT NULL DEFAULT 0,
    crid2_amount     INTEGER NOT NULL DEFAULT 0,
    crid3_amount     INTEGER NOT NULL DEFAULT 0,
    crid4_amount     INTEGER NOT NULL DEFAULT 0,
    labels           TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_snapshots_captured_at ON snapshots(captured_at);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Detailed X/Z reports: the "detalle de movimientos" parsed from a full
-- report-file transfer (job 0013 -> 9004/9005/9006), unlike `snapshots`
-- which holds only the 9 cumulative totals from sales-confirm (0014).
CREATE TABLE IF NOT EXISTS report_captures (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at      TEXT NOT NULL,
    report_type      TEXT NOT NULL DEFAULT 'X',   -- 'X' (read-only) or 'Z' (reset)
    register_serial  TEXT NOT NULL DEFAULT '',
    datetime_raw     TEXT NOT NULL DEFAULT '',     -- register's own SectionH timestamp (raw)
    was_compressed   INTEGER NOT NULL DEFAULT 0,
    raw_bytes        BLOB                          -- kept for offline re-parsing
);
CREATE INDEX IF NOT EXISTS idx_report_captures_at ON report_captures(captured_at);

CREATE TABLE IF NOT EXISTS report_lines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id   INTEGER NOT NULL REFERENCES report_captures(id) ON DELETE CASCADE,
    fno         INTEGER NOT NULL,
    fno_label   TEXT NOT NULL DEFAULT '',
    rec_no      INTEGER NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    qty         INTEGER,
    amount      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_report_lines_report ON report_lines(report_id);
CREATE INDEX IF NOT EXISTS idx_report_lines_fno ON report_lines(fno);
"""


class Database:
    def __init__(self, path: str | Path = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def add_snapshot(self, register_serial: str, lines: dict, labels_json: str) -> int:
        """lines: {key: {'quantity': int|None, 'amount': int}} from sales_confirm."""
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO snapshots
                   (captured_at, register_serial, gross_qty, gross_amount,
                    net_qty, net_amount, caid_amount, chid_amount, ckid_amount,
                    crid1_amount, crid2_amount, crid3_amount, crid4_amount, labels)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    now, register_serial,
                    lines["gross"].get("quantity") or 0, lines["gross"]["amount"],
                    lines["net"].get("quantity") or 0, lines["net"]["amount"],
                    lines["caid"]["amount"], lines["chid"]["amount"], lines["ckid"]["amount"],
                    lines["crid1"]["amount"], lines["crid2"]["amount"],
                    lines["crid3"]["amount"], lines["crid4"]["amount"],
                    labels_json,
                ),
            )
            return cur.lastrowid

    def list_snapshots(self, limit: int = 500) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM snapshots ORDER BY captured_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def latest_snapshot(self) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM snapshots ORDER BY captured_at DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def daily_series(self) -> list[dict]:
        """Last snapshot per calendar day (the day's running total)."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT substr(captured_at,1,10) AS day,
                          MAX(captured_at) AS captured_at,
                          gross_amount, net_amount, gross_qty
                   FROM snapshots
                   GROUP BY substr(captured_at,1,10)
                   ORDER BY day"""
            ).fetchall()
            return [dict(r) for r in rows]

    def monthly_series(self) -> list[dict]:
        """Last snapshot per calendar month."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT substr(captured_at,1,7) AS month,
                          MAX(captured_at) AS captured_at,
                          gross_amount, net_amount, gross_qty
                   FROM snapshots
                   GROUP BY substr(captured_at,1,7)
                   ORDER BY month"""
            ).fetchall()
            return [dict(r) for r in rows]

    # -- Detailed X/Z reports -------------------------------------------------

    def add_report(
        self,
        report_type: str,
        register_serial: str,
        datetime_raw: str,
        was_compressed: bool,
        raw_bytes: bytes,
        lines: list[dict],
    ) -> int:
        """Persist one parsed report plus its movement lines.

        `lines`: [{fno, fno_label, rec_no, name, qty, amount}, ...]
        """
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO report_captures
                   (captured_at, report_type, register_serial, datetime_raw,
                    was_compressed, raw_bytes)
                   VALUES (?,?,?,?,?,?)""",
                (now, report_type, register_serial, datetime_raw,
                 1 if was_compressed else 0, raw_bytes),
            )
            report_id = cur.lastrowid
            c.executemany(
                """INSERT INTO report_lines
                   (report_id, fno, fno_label, rec_no, name, qty, amount)
                   VALUES (?,?,?,?,?,?,?)""",
                [(report_id, ln["fno"], ln["fno_label"], ln["rec_no"],
                  ln["name"], ln.get("qty"), ln.get("amount")) for ln in lines],
            )
            return report_id

    def list_reports(self, limit: int = 100) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT rc.id, rc.captured_at, rc.report_type, rc.register_serial,
                          rc.datetime_raw, rc.was_compressed,
                          COUNT(rl.id) AS line_count,
                          COUNT(DISTINCT rl.fno) AS block_count
                   FROM report_captures rc
                   LEFT JOIN report_lines rl ON rl.report_id = rc.id
                   GROUP BY rc.id
                   ORDER BY rc.captured_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def report_detail(self, report_id: int) -> dict | None:
        with self._conn() as c:
            meta = c.execute(
                """SELECT id, captured_at, report_type, register_serial,
                          datetime_raw, was_compressed
                   FROM report_captures WHERE id=?""",
                (report_id,),
            ).fetchone()
            if not meta:
                return None
            rows = c.execute(
                """SELECT fno, fno_label, rec_no, name, qty, amount
                   FROM report_lines WHERE report_id=? ORDER BY fno, rec_no""",
                (report_id,),
            ).fetchall()
        blocks: dict[int, dict] = {}
        for r in rows:
            b = blocks.setdefault(r["fno"], {"fno": r["fno"], "label": r["fno_label"], "lines": []})
            b["lines"].append({"rec_no": r["rec_no"], "name": r["name"],
                               "qty": r["qty"], "amount": r["amount"]})
        return {"meta": dict(meta), "blocks": list(blocks.values())}

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
