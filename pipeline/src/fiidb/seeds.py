"""Manual data maintained in the repository's seeds/ directory."""

import csv
from datetime import date
from decimal import Decimal
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


def load_corporate_actions(conn: psycopg.Connection, path: Path) -> int:
    """Replace the manual corporate actions (events the B3 page does not list) with the CSV contents.
    The ticker's current ISIN is used, so the ticker must be in COTAHIST."""
    with path.open(encoding="utf-8", newline="") as f:
        rows = [row for row in csv.DictReader(f) if (row.get("ticker") or "").strip()]
    isins = dict(conn.execute("select ticker, isin from security").fetchall())
    records = {}
    for row in rows:
        ticker = row["ticker"].strip().upper()
        if ticker not in isins:
            raise ValueError(f"{path.name}: unknown ticker {ticker}")
        multiplier = Decimal(row["multiplier"].strip())
        day = date.fromisoformat(row["last_date_prior"].strip())
        kind = row["kind"].strip().upper()
        records[(isins[ticker], day, kind)] = [isins[ticker], day, kind, ticker, str(multiplier), multiplier, "manual"]
    with conn.transaction():
        conn.execute("delete from corporate_action where source = 'manual'")
        db.upsert(
            conn,
            "corporate_action",
            ["isin", "last_date_prior", "kind", "ticker", "factor_raw", "multiplier", "source"],
            records.values(),
            key=["isin", "last_date_prior", "kind"],
        )
    return len(records)
