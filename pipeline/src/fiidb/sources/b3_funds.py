"""Corporate actions (splits, groupings, bonus shares) and cash distributions from the B3
listed-fund page.

The endpoint behind https://sistemaswebb3-listados.b3.com.br/fundsPage/ returns, per fund code
(the 4 letters of the ticker), `stockDividends` with the full event history, `cashDividends` with a
rolling window of about 12 months (subscription receipts included, under their own ISIN) and
`quantity`, the shares outstanding. It is not an official API. Only watchlist funds are queried,
one request each. Cash distributions are stored only for FI-Infra: FundosNET does not carry their
notices, while FIIs keep using FundosNET (full history).

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
# cashDividends labels by prefix: RENDIMENTO, AMORTIZACAO RF...
CASH_KINDS = {"RENDIMENTO": "income", "AMORTIZA": "amortization"}


@dataclass
class CorporateAction:
    isin: str
    last_date_prior: date
    kind: str
    factor_raw: str
    multiplier: Decimal | None
    approved_on: date | None


@dataclass
class CashDistribution:
    isin: str
    kind: str  # income | amortization
    base_date: date  # data-com (lastDatePrior)
    payment_date: date | None
    amount_per_share: Decimal
    reference_period: str | None


@dataclass
class Page:
    shares: Decimal | None  # `quantity`: shares outstanding
    actions: list[CorporateAction]
    distributions: list[CashDistribution]
    unknown_labels: set[str]  # cashDividends labels that are neither income nor amortization


def _decimal(value: str) -> Decimal:
    return Decimal(value.replace(".", "").replace(",", "."))


def _text(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def cash_kind(label: str) -> str | None:
    return next((kind for prefix, kind in CASH_KINDS.items() if label.upper().startswith(prefix)), None)


def multiplier(kind: str, factor_raw: str) -> Decimal | None:
    """Shares after per share before, or None when the convention for `kind` is unknown."""
    if kind.upper() in PERCENT_OF_NEW_SHARES:
        return 1 + _decimal(factor_raw) / 100
    if kind.upper() in DIRECT_MULTIPLIER:
        return _decimal(factor_raw)
    return None


def parse_page(data: bytes) -> Page:
    payload = json.loads(data or b"{}") or {}
    distributions: dict[tuple, CashDistribution] = {}
    unknown = set()
    for event in payload.get("cashDividends") or []:
        isin = (event.get("isinCode") or event.get("assetIssued") or "").strip()
        base_date = parse_date(event.get("lastDatePrior"))
        label = (event.get("label") or "").strip()
        rate = (event.get("rate") or "").strip()
        kind = cash_kind(label)
        if kind is None:
            unknown.add(label)
            continue
        if not (isin and base_date and rate):
            continue
        distributions[(isin, kind, base_date)] = CashDistribution(
            isin, kind, base_date, parse_date(event.get("paymentDate")), _decimal(rate), _text(event.get("relatedTo"))
        )
    quantity = (payload.get("quantity") or "").strip()
    return Page(
        shares=_decimal(quantity) if quantity else None,
        actions=_parse_actions(payload),
        distributions=list(distributions.values()),
        unknown_labels=unknown,
    )


def parse(data: bytes) -> list[CorporateAction]:
    return parse_page(data).actions


def _parse_actions(payload: dict) -> list[CorporateAction]:
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
DISTRIBUTION_COLUMNS = [
    "source",
    "isin",
    "ticker",
    "kind",
    "base_date",
    "payment_date",
    "amount_per_share",
    "reference_period",
    "fund_cnpj",
]


def load_distributions(conn: psycopg.Connection, ticker: str, cnpj: str, items: list[CashDistribution]) -> int:
    """Upsert B3 cash distributions. Rows outside B3's window stay: the history accumulates."""
    return db.upsert(
        conn,
        "distribution",
        DISTRIBUTION_COLUMNS,
        (
            ["b3", d.isin, ticker, d.kind, d.base_date, d.payment_date, d.amount_per_share, d.reference_period, cnpj]
            for d in items
        ),
        key=["isin", "kind", "base_date"],
        key_where="source = 'b3'",
    )


def window_start(today: date) -> date:
    """First data-com covered by the cashDividends window (about 12 months)."""
    try:
        return today.replace(year=today.year - 1)
    except ValueError:  # Feb 29
        return today.replace(year=today.year - 1, day=28)


def ingest(conn: psycopg.Connection, http: httpx.Client, ticker: str, *, cash_cnpj: str | None = None) -> Page:
    """Load the corporate actions of a ticker, and its cash distributions when `cash_cnpj` (the
    fund's CNPJ) is given."""
    started = dates.now()
    page = parse_page(fetch(http, ticker))
    actions = page.actions
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
        rows = len(actions)
        if cash_cnpj:
            rows += load_distributions(conn, ticker, cash_cnpj, page.distributions)
            for label in page.unknown_labels:
                log.warning("%s %s: cash event %r is neither income nor amortization; skipped", SOURCE, ticker, label)
        db.record_run(conn, source=SOURCE, period=ticker, status="ok", started_at=started, rows=rows)
    return page


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
