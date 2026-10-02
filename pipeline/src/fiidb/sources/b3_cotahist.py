"""B3 COTAHIST historical quotes (fixed-width, 245 chars, latin-1).

Layout: https://www.b3.com.br/data/files/33/67/B9/50/D84057102C784E47AC094EA8/SeriesHistoricas_Layout.pdf
Files: COTAHIST_A{yyyy}.ZIP (year), COTAHIST_D{ddmmyyyy}.ZIP (day, published in the evening).
FIIs trade under BDI code 12; Fiagro/FI-Infra share BDI 14 with ETFs and FIPs.
"""

import io
import logging
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import httpx
import psycopg

from fiidb import dates, db
from fiidb import http as fhttp

log = logging.getLogger(__name__)

SOURCE = "b3_cotahist"
BASE_URL = "https://bvmf.bmfbovespa.com.br/InstDados/SerHist/"
BDI_CODES = {"12"}  # FII only for now
MARKET_SPOT = "010"


@dataclass
class Quote:
    ticker: str
    trade_date: date
    isin: str
    bdi_code: str
    short_name: str
    open: Decimal
    high: Decimal
    low: Decimal
    avg: Decimal
    close: Decimal
    trades: int
    quantity: int
    volume: Decimal


def _price(field: str, factor: int) -> Decimal:
    return Decimal(int(field)) / 100 / factor


def parse_lines(lines: Iterable[str], bdi_codes: set[str] = BDI_CODES) -> Iterator[Quote]:
    for line in lines:
        if line[:2] != "01" or line[10:12] not in bdi_codes or line[24:27] != MARKET_SPOT:
            continue
        factor = int(line[210:217]) or 1
        yield Quote(
            ticker=line[12:24].strip(),
            trade_date=date(int(line[2:6]), int(line[6:8]), int(line[8:10])),
            isin=line[230:242],
            bdi_code=line[10:12],
            short_name=line[27:39].strip(),
            open=_price(line[56:69], factor),
            high=_price(line[69:82], factor),
            low=_price(line[82:95], factor),
            avg=_price(line[95:108], factor),
            close=_price(line[108:121], factor),
            trades=int(line[147:152]),
            quantity=int(line[152:170]),
            volume=Decimal(int(line[170:188])) / 100,
        )


def parse_zip(data: bytes) -> list[Quote]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        text = archive.read(archive.namelist()[0]).decode("latin-1")
    return list(parse_lines(text.splitlines()))


QUOTE_COLUMNS = ["ticker", "trade_date", "isin", "open", "high", "low", "avg", "close", "trades", "quantity", "volume"]


def load(conn: psycopg.Connection, quotes: list[Quote]) -> int:
    if not quotes:
        return 0
    securities: dict[str, list] = {}
    for q in sorted(quotes, key=lambda q: q.trade_date):
        s = securities.setdefault(q.ticker, [q.ticker, q.isin, q.bdi_code, q.short_name, q.trade_date, q.trade_date])
        s[1], s[3], s[5] = q.isin, q.short_name, q.trade_date
    db.upsert(
        conn,
        "security",
        ["ticker", "isin", "bdi_code", "short_name", "first_trade_date", "last_trade_date"],
        securities.values(),
        key=["ticker"],
        update=[],
    )
    # Merge date ranges; identity fields follow the most recent trade.
    for ticker, isin, _bdi, name, first, last in securities.values():
        conn.execute(
            """
            update security set
                first_trade_date = least(first_trade_date, %(first)s),
                isin = case when %(last)s >= last_trade_date then %(isin)s else isin end,
                short_name = case when %(last)s >= last_trade_date then %(name)s else short_name end,
                last_trade_date = greatest(last_trade_date, %(last)s)
            where ticker = %(ticker)s
            """,
            {"ticker": ticker, "isin": isin, "name": name, "first": first, "last": last},
        )
    return db.upsert(
        conn,
        "quote_daily",
        QUOTE_COLUMNS,
        ([getattr(q, c) for c in QUOTE_COLUMNS] for q in quotes),
        key=["ticker", "trade_date"],
    )


def _ingest(
    conn: psycopg.Connection, http: httpx.Client, filename: str, period: str, *, cache: bool, cache_dir
) -> int | None:
    url = BASE_URL + filename
    started = dates.now()
    data = fhttp.fetch(http, url, cache_dir=cache_dir if cache else None)
    if data is None:
        return None
    with conn.transaction():
        rows = load(conn, parse_zip(data))
        db.record_run(
            conn,
            source=SOURCE,
            period=period,
            status="ok",
            started_at=started,
            url=url,
            sha256=fhttp.sha256(data),
            rows=rows,
        )
    log.info("%s %s: %d quotes upserted", SOURCE, period, rows)
    return rows


def ingest_year(conn, http, year: int, *, cache_dir=None) -> int | None:
    closed = year < dates.today().year
    return _ingest(conn, http, f"COTAHIST_A{year}.ZIP", f"A{year}", cache=closed, cache_dir=cache_dir)


def ingest_day(conn, http, day: date) -> int | None:
    return _ingest(conn, http, f"COTAHIST_D{day:%d%m%Y}.ZIP", f"D{day:%Y%m%d}", cache=False, cache_dir=None)


def catch_up(
    conn: psycopg.Connection, http: httpx.Client, start_year: int, *, cache_dir=None, today: date | None = None
) -> None:
    """Closed years come from yearly files (once); the current year from the yearly file on
    first run, then one daily file per missing weekday."""
    today = today or dates.today()
    for year in range(start_year, today.year):
        previous = db.last_run(conn, SOURCE, f"A{year}")
        if previous is None or previous[0] != "ok":
            ingest_year(conn, http, year, cache_dir=cache_dir)

    (last,) = conn.execute(
        "select max(trade_date) from quote_daily where trade_date >= %s", (date(today.year, 1, 1),)
    ).fetchone()
    if last is None:
        ingest_year(conn, http, today.year)
        (last,) = conn.execute(
            "select max(trade_date) from quote_daily where trade_date >= %s", (date(today.year, 1, 1),)
        ).fetchone()
    day = (last or date(today.year, 1, 1) - timedelta(days=1)) + timedelta(days=1)
    while day <= today:
        if day.weekday() < 5 and ingest_day(conn, http, day) is None:
            log.info("%s %s: no file (holiday or not yet published)", SOURCE, day)
        day += timedelta(days=1)
