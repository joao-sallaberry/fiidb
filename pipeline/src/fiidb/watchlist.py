"""Funds whose distributions are ingested (`fiidb watch ...`): FundosNET for FIIs, the B3
listed-fund page for FI-Infra."""

import logging
from dataclasses import dataclass
from datetime import date

import psycopg

log = logging.getLogger(__name__)

FULL_HISTORY = date(1900, 1, 1)  # history_since value after loading every notice ever published


@dataclass
class Entry:
    ticker: str
    cnpj: str | None  # explicit override, else the linked fund's CNPJ
    name: str | None
    history_since: date | None
    fund_type: str | None  # FII | FIAGRO | FIINFRA; None when the CNPJ is not in `fund` yet

    @property
    def is_infra(self) -> bool:
        return self.fund_type == "FIINFRA"


def entries(conn: psycopg.Connection, tickers: list[str] | None = None) -> list[Entry]:
    rows = conn.execute(
        """
        select w.ticker, coalesce(w.cnpj, linked.cnpj), f.name, w.history_since, f.type
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


def current_ticker(conn: psycopg.Connection, ticker: str) -> str:
    """The ticker the shares trade under today (`ticker` itself unless it changed code)."""
    row = conn.execute("select ticker from ticker_lineage where member_ticker = %s", (ticker,)).fetchone()
    return row[0] if row else ticker


def lineage(conn: psycopg.Connection, ticker: str) -> set[str]:
    """The ticker and every earlier code of the same shares."""
    rows = conn.execute("select member_ticker from ticker_lineage where ticker = %s", (ticker,)).fetchall()
    return {ticker} | {t for (t,) in rows}


def add(conn: psycopg.Connection, ticker: str, cnpj: str | None = None) -> Entry:
    """Add (or update the CNPJ of) a ticker; an old code is replaced by the current one. Raises
    ValueError when no CNPJ can be resolved."""
    ticker = ticker.strip().upper()
    if (current := current_ticker(conn, ticker)) != ticker:
        log.info("%s now trades as %s; adding %s", ticker, current, current)
        ticker = current
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


def notice_tickers(conn: psycopg.Connection, cnpj: str) -> set[str]:
    """Tickers named in the FundosNET notices stored for a CNPJ."""
    rows = conn.execute(
        "select distinct ticker from distribution where fund_cnpj = %s and ticker is not null", (cnpj,)
    ).fetchall()
    return {t for (t,) in rows}


def mark_history(conn: psycopg.Connection, ticker: str, since: date) -> None:
    conn.execute(
        "update watchlist set history_since = least(coalesce(history_since, %(since)s), %(since)s) where ticker = %(t)s",
        {"since": since, "t": ticker},
    )


def follow_ticker_changes(conn: psycopg.Connection) -> list[tuple[str, str]]:
    """Rename watchlist entries whose ticker changed code (see ticker_lineage), keeping the CNPJ and
    the history coverage: notices are fetched by CNPJ, so they cover both codes. Returns (old, new)."""
    changes = conn.execute(
        """
        select w.ticker, l.ticker
        from watchlist w
        join ticker_lineage l on l.member_ticker = w.ticker
        where l.depth > 0
        order by w.ticker
        """
    ).fetchall()
    with conn.transaction():
        for old, new in changes:
            merged = conn.execute(
                """
                update watchlist n set
                    cnpj = coalesce(n.cnpj, o.cnpj),
                    history_since = least(n.history_since, o.history_since)
                from watchlist o
                where n.ticker = %(new)s and o.ticker = %(old)s
                """,
                {"old": old, "new": new},
            ).rowcount
            if merged:
                conn.execute("delete from watchlist where ticker = %s", (old,))
            else:
                conn.execute("update watchlist set ticker = %s where ticker = %s", (new, old))
    return [(old, new) for old, new in changes]
