"""Corporate actions (splits, groupings, bonus shares) from the B3 listed-fund page.

The endpoint behind https://sistemaswebb3-listados.b3.com.br/fundsPage/ returns, per fund code
(the 4 letters of the ticker), `stockDividends` with the full event history. It is not an official
API. Only watchlist funds are queried, one request each.

Factor conventions: for DESDOBRAMENTO and BONIFICACAO the factor is the percentage of new shares
(900 = 9 new shares per share, i.e. x10). For GRUPAMENTO it is the multiplier itself (0,02 = 50
shares become 1), confirmed by FLMA11 (factor 0,02 on 2021-05-31; price 2.91 -> 139.22). Other kinds
(e.g. RESG TOTAL RV, redemption of receipts) do not change the share and are stored unapplied.
"""

import base64
import json
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import httpx
import psycopg

from fiidb import dates, db
from fiidb import http as fhttp
from fiidb.dates import parse_date

log = logging.getLogger(__name__)

SOURCE = "b3_funds"
URL = "https://sistemaswebb3-listados.b3.com.br/fundsProxy/fundsCall/GetListedSupplementFunds/{payload}"
TIMEOUT = 20
RETRIES = 4

PERCENT_OF_NEW_SHARES = {"DESDOBRAMENTO", "BONIFICACAO", "BONIFICAÇÃO"}
DIRECT_MULTIPLIER = {"GRUPAMENTO"}


@dataclass
class CorporateAction:
    isin: str
    last_date_prior: date
    kind: str
    factor_raw: str
    multiplier: Decimal | None
    approved_on: date | None


def _decimal(value: str) -> Decimal:
    return Decimal(value.replace(".", "").replace(",", "."))


def multiplier(kind: str, factor_raw: str) -> Decimal | None:
    """Shares after per share before, or None when the convention for `kind` is unknown."""
    if kind.upper() in PERCENT_OF_NEW_SHARES:
        return 1 + _decimal(factor_raw) / 100
    if kind.upper() in DIRECT_MULTIPLIER:
        return _decimal(factor_raw)
    return None


def parse(data: bytes) -> list[CorporateAction]:
    payload = json.loads(data or b"{}") or {}
    out: dict[tuple, CorporateAction] = {}  # B3 sometimes repeats an event in the same payload
    for event in payload.get("stockDividends") or []:
        isin = (event.get("isinCode") or event.get("assetIssued") or "").strip()
        last_date = parse_date(event.get("lastDatePrior"))
        kind = (event.get("label") or "").strip().upper()
        factor = (event.get("factor") or "").strip()
        if not (isin and last_date and kind and factor):
            continue
        out[(isin, last_date, kind)] = CorporateAction(
            isin, last_date, kind, factor, multiplier(kind, factor), parse_date(event.get("approvedOn"))
        )
    return list(out.values())


def fetch(http: httpx.Client, ticker: str) -> bytes:
    request = {"cnpj": "0", "identifierFund": ticker[:4].upper(), "typeFund": 7}
    payload = base64.b64encode(json.dumps(request, separators=(",", ":")).encode()).decode()
    return fhttp.fetch(http, URL.format(payload=payload), retries=RETRIES) or b"{}"


COLUMNS = ["isin", "last_date_prior", "kind", "ticker", "factor_raw", "multiplier", "approved_on"]


def ingest(conn: psycopg.Connection, http: httpx.Client, ticker: str) -> int:
    started = dates.now()
    actions = parse(fetch(http, ticker))
    known = set(
        conn.execute(
            "select isin, last_date_prior, kind from corporate_action where isin = any(%s)",
            (list({a.isin for a in actions}),),
        ).fetchall()
    )
    for a in actions:
        if a.multiplier is None and (a.isin, a.last_date_prior, a.kind) not in known:  # warn once, when first seen
            log.warning(
                "%s %s: %s on %s (factor %s) is not a split, grouping or bonus; not applied",
                SOURCE,
                ticker,
                a.kind,
                a.last_date_prior,
                a.factor_raw,
            )
    with conn.transaction():
        db.upsert(
            conn,
            "corporate_action",
            COLUMNS,
            ([a.isin, a.last_date_prior, a.kind, ticker, a.factor_raw, a.multiplier, a.approved_on] for a in actions),
            key=["isin", "last_date_prior", "kind"],
        )
        db.record_run(conn, source=SOURCE, period=ticker, status="ok", started_at=started, rows=len(actions))
    return len(actions)


UNEXPLAINED_JUMPS = """
    with q as (
        select ticker, isin, trade_date, close,
            lag(close) over (partition by ticker order by trade_date) as prev_close,
            lag(trade_date) over (partition by ticker order by trade_date) as prev_date
        from quote_daily
        where ticker = any(%(tickers)s)
    )
    select ticker, prev_date, trade_date, prev_close, close
    from q
    where prev_close is not null and close > 0
        and (prev_close / close >= 1.8 or prev_close / close <= 0.55)
        and not exists (
            select 1 from corporate_action c
            where c.isin = q.isin and c.last_date_prior >= q.prev_date and c.last_date_prior < q.trade_date
        )
    order by ticker, trade_date
"""


def unexplained_jumps(conn: psycopg.Connection, tickers: list[str]) -> list[tuple]:
    """Day-over-day price moves of 45%+ with no corporate action between them: a split B3 does not
    list, or a real crash. Only reported, never applied."""
    return conn.execute(UNEXPLAINED_JUMPS, {"tickers": tickers}).fetchall()
