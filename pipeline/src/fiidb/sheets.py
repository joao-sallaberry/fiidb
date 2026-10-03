"""Writes watchlist funds to a Google Sheets tab owned by the pipeline.

The tab is rewritten on every publish; the user's own tabs read it with XLOOKUP/PROCX by ticker,
so manual cells are never touched. Prices are not the point here: the sheet uses GOOGLEFINANCE for
live prices, so it gets the ingredients (NAV per share, 12-month income) and computes P/VP and DY.
"""

import logging
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

import httpx
import psycopg
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from fiidb import dates

log = logging.getLogger(__name__)

API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# (header, fund_metrics column). Headers are what the user's lookups reference: renaming one breaks
# their formulas, so only append new columns at the end.
COLUMNS = [
    ("ticker", "ticker"),
    ("nome", "name"),
    ("categoria", "category"),
    ("segmento", "segment"),
    ("vp_cota", "nav_per_share"),
    ("mes_ref_vp", "report_month"),
    ("ultimo_rendimento", "last_income"),
    ("data_com", "last_income_base_date"),
    ("data_pagamento", "last_income_payment_date"),
    ("rendimentos_12m", "income_12m"),
    ("liquidez_media_21d", "avg_volume_21d"),
    ("cotistas", "shareholders"),
    ("preco_fechamento", "price"),
    ("data_fechamento", "price_date"),
]


def _cell(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.astimezone(dates.MARKET_TZ).strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    return value


def build_rows(conn: psycopg.Connection) -> list[list[object]]:
    """Header plus one row per watchlist ticker, with an `atualizado_em` column at the end."""
    columns = ", ".join(column for _, column in COLUMNS)
    records = conn.execute(f"select {columns} from fund_metrics where watched order by ticker").fetchall()
    updated = _cell(dates.now())
    return [[header for header, _ in COLUMNS] + ["atualizado_em"]] + [
        [_cell(v) for v in record] + [updated] for record in records
    ]


def _a1(tab: str, cells: str) -> str:
    return quote(f"'{tab}'!{cells}", safe="")


def write(http: httpx.Client, sheet_id: str, tab: str, rows: Sequence[Sequence[object]]) -> None:
    """Create the tab if needed, write rows from A1 and clear anything below them (rows from a
    previous, longer publish). Values are USER_ENTERED so ISO dates become real dates."""
    meta = http.get(f"{API}/{sheet_id}", params={"fields": "sheets.properties.title"})
    meta.raise_for_status()
    titles = {s["properties"]["title"] for s in meta.json().get("sheets", [])}
    if tab not in titles:
        http.post(
            f"{API}/{sheet_id}:batchUpdate", json={"requests": [{"addSheet": {"properties": {"title": tab}}}]}
        ).raise_for_status()

    http.put(
        f"{API}/{sheet_id}/values/{_a1(tab, 'A1')}",
        params={"valueInputOption": "USER_ENTERED"},
        json={"values": [list(r) for r in rows]},
    ).raise_for_status()
    http.post(f"{API}/{sheet_id}/values/{_a1(tab, f'A{len(rows) + 1}:ZZ')}:clear").raise_for_status()


class CredentialsError(Exception):
    pass


def load_credentials(credentials_file: Path) -> service_account.Credentials:
    """Load the service account key, explaining the usual setup mistakes."""
    if not credentials_file.exists():
        raise CredentialsError(f"service account key not found: {credentials_file}")
    try:
        return service_account.Credentials.from_service_account_file(str(credentials_file), scopes=SCOPES)
    except (ValueError, KeyError) as exc:  # JSONDecodeError is a ValueError
        raise CredentialsError(
            f"{credentials_file} is not a service account JSON key ({exc}). It must be the .json file the browser "
            "downloads on 'Add key > Create new key > JSON' (starts with '{', ~2 KB), not the key id."
        ) from exc


def authorized_client(credentials_file: Path) -> httpx.Client:
    credentials = load_credentials(credentials_file)
    credentials.refresh(Request())
    return httpx.Client(headers={"Authorization": f"Bearer {credentials.token}"}, timeout=60)


def publish(conn: psycopg.Connection, sheet_id: str, tab: str, credentials_file: Path) -> int:
    """Rewrite the pipeline tab. Returns the number of funds written."""
    rows = build_rows(conn)
    with authorized_client(credentials_file) as http:
        write(http, sheet_id, tab, rows)
    log.info("sheets: %d funds written to tab %r", len(rows) - 1, tab)
    return len(rows) - 1
