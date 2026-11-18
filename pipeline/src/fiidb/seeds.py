"""Manual data maintained in the repository's seeds/ directory."""

import csv
import logging
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg

from fiidb import db, linking

log = logging.getLogger(__name__)

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
    # fund_profile matches overrides by ticker: one written for an old code no longer applies.
    for old, new in conn.execute(
        "select member_ticker, ticker from ticker_lineage where depth > 0 and member_ticker = any(%s)", (list(rows),)
    ):
        log.warning("%s: %s now trades as %s; rename it there", path.name, old, new)
    return len(rows)


TICKER_CHANGE_COLUMNS = ["old_ticker", "new_ticker", "note"]


def load_ticker_changes(conn: psycopg.Connection, path: Path) -> int:
    """Replace ticker_change_manual with the CSV contents and refresh the lineage. A change whose
    tickers are not both in COTAHIST yet is kept but has no effect until they are."""
    with path.open(encoding="utf-8", newline="") as f:
        rows = {}
        for row in csv.DictReader(f):
            old, new = ((row.get(c) or "").strip().upper() for c in TICKER_CHANGE_COLUMNS[:2])
            if old and new:
                rows[old] = [old, new, (row.get("note") or "").strip() or None]
    known = {
        t
        for (t,) in conn.execute(
            "select ticker from security where ticker = any(%s)", ([t for r in rows.values() for t in r[:2]],)
        )
    }
    for old, new, _ in rows.values():
        if missing := {old, new} - known:
            log.warning("%s: %s -> %s ignored until %s is in COTAHIST", path.name, old, new, ", ".join(sorted(missing)))
    with conn.transaction():
        conn.execute("delete from ticker_change_manual")
        db.upsert(conn, "ticker_change_manual", TICKER_CHANGE_COLUMNS, rows.values(), key=["old_ticker"])
        linking.refresh_lineage(conn)
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
