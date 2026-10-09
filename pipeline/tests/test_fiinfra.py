"""FI-Infra: CVM registry and daily report, B3 cash distributions. Parsers run without a database;
loads use a real Postgres (see conftest.py) and a mocked HTTP server."""

import json
from datetime import date, timedelta
from decimal import Decimal

import httpx
from conftest import FIXTURES

from fiidb import dates, linking, watchlist
from fiidb.sources import b3_funds, cvm_fi

KDIF = "26324298000189"
REGISTRY = (FIXTURES / "registro_fundo_classe_sample.zip").read_bytes()
DAILY = (FIXTURES / "inf_diario_fi_sample.zip").read_bytes()
PAGE = (FIXTURES / "b3_fund_kdif.json").read_bytes()


def test_parse_registry_picks_the_active_registration():
    classes = cvm_fi.parse_registry(REGISTRY)
    kdif = classes[KDIF]
    assert kdif.name.startswith("KINEA INFRA FIF CIC")
    assert (kdif.status, kdif.condominium) == ("Em Funcionamento Normal", "Fechado")
    # registro_fundo has three registrations for this CNPJ; the class points to the active one.
    assert (kdif.administrator_name, kdif.administrator_cnpj) == ("INTRAG DTVM LTDA.", "62418140000131")
    assert kdif.started_at == date(2025, 6, 26)
    assert kdif.looks_like_infra()
    assert set(cvm_fi.parse_registry(REGISTRY, {KDIF})) == {KDIF}


def test_parse_daily_keeps_the_requested_classes():
    reports = cvm_fi.parse_daily(DAILY, {KDIF, "00888897000131"})  # the second one only has subclass rows
    assert [(r.cnpj, r.ref_date) for r in reports] == [(KDIF, date(2026, 9, d)) for d in (28, 29, 30)]
    last = reports[-1]
    assert last.nav_per_share == Decimal("117.998128600000")
    assert last.net_assets == Decimal("2771158833.09")
    assert last.shareholders == 55632


def test_parse_b3_cash_distributions():
    page = b3_funds.parse_page(PAGE)
    assert page.shares == Decimal(23484769)
    assert page.actions == []
    by_key = {(d.isin, d.base_date): d for d in page.distributions}
    assert set(by_key) == {
        ("BRKDIFCTF003", date(2026, 9, 30)),
        ("BRKDIFCTF003", date(2026, 8, 31)),
        ("BRKDIFR12M17", date(2026, 2, 27)),  # subscription receipt, kept under its own ISIN
    }
    sep = by_key[("BRKDIFCTF003", date(2026, 9, 30))]
    assert (sep.kind, sep.amount_per_share, sep.payment_date) == ("income", Decimal(1), date(2026, 10, 7))
    assert sep.reference_period == "SETEMBRO 2026/2026"


def cash_event(label: str, base: date, rate: str, isin: str = "BRKDIFCTF003") -> dict:
    return {"isinCode": isin, "label": label, "lastDatePrior": f"{base:%d/%m/%Y}", "rate": rate, "paymentDate": ""}


def test_cash_labels():
    events = [
        cash_event("AMORTIZACAO RF", date(2026, 4, 8), "0,26294411000"),
        cash_event("JRS CAP PROPRIO", date(2026, 4, 8), "1,0"),
    ]
    page = b3_funds.parse_page(json.dumps({"cashDividends": events}).encode())
    assert [(d.kind, d.amount_per_share) for d in page.distributions] == [("amortization", Decimal("0.26294411000"))]
    assert page.unknown_labels == {"JRS CAP PROPRIO"}


def test_month_and_window_helpers():
    assert cvm_fi.recent_months(date(2026, 1, 15)) == [date(2025, 12, 1), date(2026, 1, 1)]
    assert b3_funds.window_start(date(2028, 2, 29)) == date(2027, 2, 28)


def client(page: bytes = PAGE) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "registro_fundo_classe" in url:
            return httpx.Response(200, content=REGISTRY)
        if "inf_diario_fi_202609" in url:
            return httpx.Response(200, content=DAILY)
        if "fundsProxy" in url:
            return httpx.Response(200, content=page)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def add_kdif(conn) -> int:
    """KDIF11 traded (BDI 14), its fund created from the registry and added to the watchlist."""
    conn.execute(
        "insert into security (ticker, isin, bdi_code, short_name, first_trade_date, last_trade_date) "
        "values ('KDIF11', 'BRKDIFCTF003', '14', 'KINEA INFRAF', %(d)s, %(d)s)",
        {"d": dates.today()},
    )
    conn.execute(
        "insert into quote_daily (ticker, trade_date, isin, close, volume) "
        "values ('KDIF11', %s, 'BRKDIFCTF003', 110, 1000000)",
        (dates.today(),),
    )
    with client() as http:
        cvm_fi.ingest_registry(conn, http, {KDIF}, force=True)
    watchlist.add(conn, "KDIF11", KDIF)
    linking.link_securities(conn)
    return cvm_fi.funds(conn)[KDIF]


def test_daily_report_load_is_idempotent_and_feeds_metrics(conn, load_fixtures):
    load_fixtures(conn)
    fund_id = add_kdif(conn)
    ids = {KDIF: fund_id}
    with client() as http:
        assert cvm_fi.ingest_daily(conn, http, date(2026, 9, 1), ids) == "ok"
        assert cvm_fi.ingest_daily(conn, http, date(2026, 9, 1), ids) == "unchanged"
        assert cvm_fi.ingest_daily(conn, http, date(2026, 9, 1), ids, force=True) == "ok"
        assert cvm_fi.ingest_daily(conn, http, date(2026, 8, 1), ids) == "missing"
    assert conn.execute("select count(*) from daily_report").fetchone() == (3,)

    assert conn.execute("select type from fund where cnpj = %s", (KDIF,)).fetchone() == ("FIINFRA",)
    assert conn.execute("select fund_id, link_method from security where ticker = 'KDIF11'").fetchone() == (
        fund_id,
        "watchlist",
    )
    row = conn.execute(
        "select category, nav_per_share, report_month, price_to_nav from fund_metrics where ticker = 'KDIF11'"
    ).fetchone()
    assert row == ("FI-Infra", Decimal("117.998128600000"), date(2026, 9, 30), Decimal("0.9322"))

    ratio = cvm_fi.shares_ratio(conn, fund_id, Decimal(23484769))  # B3 quantity of KDIF11
    assert abs(ratio - 1) < Decimal("0.001")


def test_bdi14_tickers_are_not_linked_to_fiis_by_isin(conn, load_fixtures):
    load_fixtures(conn)
    # A FII reporting JURO11's ISIN to CVM must not capture the FI-Infra ticker.
    conn.execute("update fund set isin = 'BRJUROCTF002' where cnpj = '11728688000147'")
    linking.link_securities(conn)
    assert conn.execute("select fund_id from security where ticker = 'JURO11'").fetchone() == (None,)


def test_b3_distributions_accumulate_and_feed_metrics(conn, load_fixtures):
    load_fixtures(conn)
    add_kdif(conn)
    today = dates.today()
    old, recent = today - timedelta(days=40), today - timedelta(days=10)
    window = {
        "quantity": "23.484.769",
        "cashDividends": [cash_event("RENDIMENTO", old, "1,45"), cash_event("RENDIMENTO", recent, "1,00")],
    }
    with client(json.dumps(window).encode()) as http:
        page = b3_funds.ingest(conn, http, "KDIF11", cash_cnpj=KDIF)
        b3_funds.ingest(conn, http, "KDIF11", cash_cnpj=KDIF)  # idempotent
    assert page.shares == Decimal(23484769)
    # Next month the oldest event leaves the window: the stored row stays.
    window["cashDividends"] = window["cashDividends"][1:]
    with client(json.dumps(window).encode()) as http:
        b3_funds.ingest(conn, http, "KDIF11", cash_cnpj=KDIF)
    assert conn.execute("select count(*), min(source), max(source) from distribution").fetchone() == (2, "b3", "b3")

    metrics = "select last_income, last_income_base_date, income_12m from fund_metrics where ticker = 'KDIF11'"
    assert conn.execute(metrics).fetchone() == (Decimal("1.00000000"), recent, None)  # history not marked yet
    watchlist.mark_history(conn, "KDIF11", b3_funds.window_start(today))
    assert conn.execute(metrics).fetchone() == (Decimal("1.00000000"), recent, Decimal("2.45000000"))


def test_b3_distributions_are_ignored_without_cnpj(conn, load_fixtures):
    """FIIs keep using FundosNET: the same page call only stores corporate actions."""
    load_fixtures(conn)
    with client() as http:
        b3_funds.ingest(conn, http, "HGLG11")
    assert conn.execute("select count(*) from distribution").fetchone() == (0,)
