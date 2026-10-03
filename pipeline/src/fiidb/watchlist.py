"""Funds whose FundosNET notices are ingested (`fiidb watch ...`)."""

from dataclasses import dataclass
from datetime import date

import psycopg

FULL_HISTORY = date(1900, 1, 1)  # history_since value after loading every notice ever published


@dataclass
class Entry:
    ticker: str
    cnpj: str | None  # explicit override, else the linked fund's CNPJ
    name: str | None
    history_since: date | None


def entries(conn: psycopg.Connection, tickers: list[str] | None = None) -> list[Entry]:
    rows = conn.execute(
        """
        select w.ticker, coalesce(w.cnpj, linked.cnpj), f.name, w.history_since
        from watchlist w
        left join security s on s.ticker = w.ticker
        left join fund linked on linked.id = s.fund_id
        left join fund f on f.cnpj = coalesce(w.cnpj, linked.cnpj)
        where %(tickers)s::text[] is null or w.ticker = any(%(tickers)s)
        order by w.ticker
        """,
        {"tickers": tickers},
    ).fetchall()
    return [Entry(*row) for row in rows]


def add(conn: psycopg.Connection, ticker: str, cnpj: str | None = None) -> Entry:
    """Add (or update the CNPJ of) a ticker. Raises ValueError when no CNPJ can be resolved."""
    ticker = ticker.strip().upper()
    if cnpj is None:
        row = conn.execute(
            "select f.cnpj from security s join fund f on f.id = s.fund_id where s.ticker = %s", (ticker,)
        ).fetchone()
        if row is None:
            raise ValueError(f"{ticker}: not linked to a CVM fund; pass --cnpj")
    conn.execute(
        "insert into watchlist (ticker, cnpj) values (%s, %s) on conflict (ticker) do update set cnpj = excluded.cnpj",
        (ticker, cnpj),
    )
    return entries(conn, [ticker])[0]


def remove(conn: psycopg.Connection, ticker: str) -> bool:
    return conn.execute("delete from watchlist where ticker = %s", (ticker.strip().upper(),)).rowcount > 0


def mark_history(conn: psycopg.Connection, ticker: str, since: date) -> None:
    conn.execute(
        "update watchlist set history_since = least(coalesce(history_since, %(since)s), %(since)s) where ticker = %(t)s",
        {"since": since, "t": ticker},
    )
