"""Fiagro: CVM monthly report of its own, loaded into fund/monthly_report. The parser runs without a
database; loads use a real Postgres (see conftest.py)."""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import httpx
from conftest import FIXTURES

from fiidb import cli, linking
from fiidb.sources import cvm_fiagro, cvm_fii, fnet

SAMPLE = (FIXTURES / "inf_mensal_fiagro_sample.zip").read_bytes()
SNAG = "28152777000190"
KNCA = "41745701000137"
RURA = "42479593000160"
RURA_TWIN = "68164780000149"  # another fund reporting RURA11's ISIN
SINGLE_SHARE = "30492529000130"
SEP = date(2026, 9, 1)


def test_parse_fiagro_monthly_report():
    funds, reports = cvm_fiagro.parse_zip(SAMPLE)  # the subclass CSV in the zip is ignored
    by_cnpj = {f.cnpj: f for f in funds}
    assert set(by_cnpj) == {SNAG, KNCA, RURA, RURA_TWIN, SINGLE_SHARE, "48565175000172"}
    snag = by_cnpj[SNAG]
    assert snag.name.startswith("SUNO AGRO - FIAGRO")
    assert (snag.isin, snag.listed, snag.last_report_month) == ("BRSNAGCTF000", True, SEP)
    assert snag.target_audience == "INVESTIDORES EM GERAL"
    assert snag.started_at == date(2022, 4, 29)

    report = next(r for r in reports if r.cnpj == SNAG)
    assert (report.ref_month, report.version, report.delivered_at) == (SEP, 1, date(2026, 10, 14))
    assert report.values["net_assets"] == Decimal("892702289.74")
    assert report.values["shares_outstanding"] == Decimal(89445790)
    assert report.values["shareholders"] == 141824
    # Reported with two decimals (9.98); net assets / shares agrees, so the precise value is kept.
    assert report.values["nav_per_share"] == Decimal("9.98037235")
    # Percentages come as percent (1.09), unlike the FII report.
    assert report.values["dividend_yield_ratio"] == Decimal("0.0109")
    assert report.values["return_nav_ratio"] == Decimal("-0.0146")
    assert report.raw["informe"] == "fiagro"
    assert "CRA" in report.raw and "Nome_Classe" not in report.raw
    assert next(r.version for r in reports if r.cnpj == "48565175000172") == 2


def test_parse_fiagro_nulls_the_nav_of_single_share_funds():
    _, reports = cvm_fiagro.parse_zip(SAMPLE)
    report = next(r for r in reports if r.cnpj == SINGLE_SHARE)
    assert report.values["nav_per_share"] is None
    assert report.values["net_assets"] == Decimal("1203467275.37")
    assert report.raw["Valor_Patrimonial_Cotas"] == "1203467275.37"


def test_fiagro_months():
    assert cvm_fiagro.months(date(2025, 5, 31)) == [date(2025, 5, 1)]
    months = cvm_fiagro.months(date(2026, 2, 10))
    assert (months[0], months[7], months[8], months[-1], len(months)) == (
        date(2025, 5, 1),
        date(2025, 12, 1),
        date(2026, 1, 1),
        date(2026, 2, 1),
        10,
    )


def test_fnet_search_does_not_filter_by_fund_type():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json={"recordsTotal": 0, "data": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        assert fnet.search(http, SNAG) == (0, [])
    # A Fiagro's notices are split between tipoFundo 1 (as a FII, until 2025) and 11.
    assert "tipoFundo" not in seen
    assert (seen["cnpjFundo"], seen["idTipoDocumento"]) == ("28.152.777/0001-90", "41")


def load_sample(conn) -> int:
    funds, reports = cvm_fiagro.parse_zip(SAMPLE)
    return cvm_fiagro.load(conn, funds, reports)


def add_fii_era_snag(conn) -> int:
    """SNAG11 as it was loaded from the FII report: type FII, with segment and one monthly report."""
    (fund_id,) = conn.execute(
        "insert into fund (cnpj, name, isin, segment, last_report_month) "
        "values (%s, 'SUNO AGRO - FIAGRO - IMOBILIÁRIO', 'BRSNAGCTF000', 'Outros', '2025-08-01') returning id",
        (SNAG,),
    ).fetchone()
    conn.execute(
        "insert into monthly_report (fund_id, ref_month, version, nav_per_share, raw) "
        'values (%s, \'2025-08-01\', 1, 10.3152555789394, \'{"Total_Investido": "100", "CRI_CRA": "90"}\')',
        (fund_id,),
    )
    return fund_id


def test_fii_becomes_fiagro_and_keeps_its_history(conn, load_fixtures):
    load_fixtures(conn)
    fund_id = add_fii_era_snag(conn)
    assert load_sample(conn) == 6
    assert load_sample(conn) == 6  # idempotent: same rows upserted again
    assert conn.execute("select count(*) from monthly_report where raw ->> 'informe' = 'fiagro'").fetchone() == (6,)

    row = conn.execute("select id, type, segment, isin, last_report_month from fund where cnpj = %s", (SNAG,))
    assert row.fetchone() == (fund_id, "FIAGRO", "Outros", "BRSNAGCTF000", SEP)
    months = conn.execute("select ref_month from monthly_report where fund_id = %s order by 1", (fund_id,)).fetchall()
    assert months == [(date(2025, 8, 1),), (SEP,)]
    assert conn.execute("select type, count(*) from fund group by 1 order by 1").fetchall() == [
        ("FIAGRO", 6),
        ("FII", 2),
    ]


def test_fii_report_does_not_undo_the_fiagro_one(conn, load_fixtures):
    load_fixtures(conn)
    load_sample(conn)
    funds, reports = cvm_fiagro.parse_zip(SAMPLE)
    snag = next(f for f in funds if f.cnpj == SNAG)
    report = next(r for r in reports if r.cnpj == SNAG)

    # The same month filed in the FII report with a higher version, then an older Fiagro version.
    fii_values = dict.fromkeys(cvm_fii.REPORT_COLUMNS.values()) | {"nav_per_share": Decimal(1)}
    cvm_fii.load(conn, [replace(snag, name="OLD NAME")], [cvm_fii.Report(SNAG, SEP, 9, None, fii_values, {})])
    cvm_fiagro.load(conn, [snag], [replace(report, version=0, values=report.values | {"net_assets": Decimal(1)})])
    # A blank ISIN in a later report keeps the one known.
    cvm_fiagro.load(conn, [replace(snag, isin=None, last_report_month=date(2026, 10, 1))], [])

    assert conn.execute(
        "select f.type, f.isin, m.version, m.nav_per_share, m.net_assets from fund f "
        "join monthly_report m on m.fund_id = f.id where f.cnpj = %s",
        (SNAG,),
    ).fetchall() == [("FIAGRO", "BRSNAGCTF000", 1, Decimal("9.98037235"), Decimal("892702289.74"))]


def test_fiagro_report_older_than_the_fii_one_is_kept_without_changing_the_fund(conn, load_fixtures):
    load_fixtures(conn)
    fund_id = add_fii_era_snag(conn)
    # Still filing the FII report: a later month there, and the same month here with a higher version.
    conn.execute("update fund set last_report_month = '2026-10-01' where id = %s", (fund_id,))
    conn.execute(
        "insert into monthly_report (fund_id, ref_month, version, nav_per_share, raw) values (%s, %s, 3, 1, '{}')",
        (fund_id, SEP),
    )
    load_sample(conn)
    assert conn.execute("select type, name from fund where id = %s", (fund_id,)).fetchone() == (
        "FII",
        "SUNO AGRO - FIAGRO - IMOBILIÁRIO",
    )
    assert conn.execute(
        "select version, nav_per_share, raw ->> 'informe' from monthly_report where fund_id = %s and ref_month = %s",
        (fund_id, SEP),
    ).fetchone() == (1, Decimal("9.98037235"), "fiagro")


def test_fiagro_tickers_are_linked_by_isin_and_issuer(conn, load_fixtures):
    load_fixtures(conn)  # KNCA11 trades (BDI 14) as BRKNCACTF006; the fund reports BRKNCACTF014
    for ticker, isin in (("SNAG11", "BRSNAGCTF000"), ("RURA11", "BRRURACTF004")):
        conn.execute(
            "insert into security (ticker, isin, bdi_code, short_name, first_trade_date, last_trade_date) "
            "values (%s, %s, '14', 'FIAGRO', '2026-10-01', '2026-10-01')",
            (ticker, isin),
        )
    load_sample(conn)
    linking.link_securities(conn)
    assert linking.link_securities(conn) == dict.fromkeys(linking.STEPS, 0)  # nothing left to change

    links = dict(
        conn.execute(
            "select s.ticker, s.link_method || ' ' || f.cnpj from security s join fund f on f.id = s.fund_id "
            "where s.bdi_code = '14'"
        ).fetchall()
    )
    # RURA11: two funds report its (receipt) ISIN, so nothing links it; `watch add --cnpj` does.
    assert links == {"SNAG11": f"isin {SNAG}", "KNCA11": f"isin_issuer {KNCA}"}

    row = conn.execute(
        "select category, segment, nav_per_share, net_assets, shareholders, report_month, price is not null "
        "from fund_metrics where ticker = 'KNCA11'"
    ).fetchone()
    assert row == ("Fiagro", None, Decimal("100.21917381"), Decimal("2164726036.54"), row[4], SEP, True)
    assert row[4] > 0


def test_watch_add_with_cnpj_treats_a_fiagro_like_a_fii(conn, load_fixtures):
    load_fixtures(conn)
    assert cli._is_infra_candidate(conn, "KNCA11", KNCA)  # unknown CNPJ on a BDI 14 ticker: FI-Infra flow
    load_sample(conn)
    assert not cli._is_infra_candidate(conn, "KNCA11", KNCA)


def test_ingest_month_is_idempotent(conn):
    def handler(request: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(200, content=SAMPLE)
            if "inf_mensal_fiagro_202609" in str(request.url)
            else httpx.Response(404)
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        assert cvm_fiagro.ingest_month(conn, http, SEP) == "ok"
        assert cvm_fiagro.ingest_month(conn, http, SEP) == "unchanged"
        assert cvm_fiagro.ingest_month(conn, http, SEP, force=True) == "ok"
        assert cvm_fiagro.ingest_month(conn, http, date(2026, 10, 1)) == "missing"
    assert conn.execute("select count(*) from monthly_report").fetchone() == (6,)
    runs = conn.execute("select period, status, rows from ingestion_run where source = %s", (cvm_fiagro.SOURCE,))
    assert runs.fetchall() == [("2026-09", "ok", 6), ("2026-09", "ok", 6)]
