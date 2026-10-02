"""Manual data maintained in the repository's seeds/ directory."""

import csv
from pathlib import Path

import psycopg

from fiidb import db

OVERRIDE_COLUMNS = ["ticker", "category", "segment", "note"]


def load_overrides(conn: psycopg.Connection, path: Path) -> int:
    """Replace fund_override with the CSV contents, so removed lines are removed from the database."""
    with path.open(encoding="utf-8", newline="") as f:
        rows = {
            row["ticker"].strip().upper(): [row["ticker"].strip().upper()]
            + [(row.get(c) or "").strip() or None for c in OVERRIDE_COLUMNS[1:]]
            for row in csv.DictReader(f)
            if (row.get("ticker") or "").strip()
        }
    with conn.transaction():
        conn.execute("delete from fund_override")
        db.upsert(conn, "fund_override", OVERRIDE_COLUMNS, rows.values(), key=["ticker"])
    return len(rows)
