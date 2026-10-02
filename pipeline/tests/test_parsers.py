from datetime import date
from decimal import Decimal
from pathlib import Path

from fiidb.sources import b3_cotahist, cvm_fii, fnet

FIXTURES = Path(__file__).parent / "fixtures"


def test_cotahist_keeps_only_fii_spot_quotes():
    lines = (FIXTURES / "cotahist_sample.txt").read_text(encoding="latin-1").splitlines()
    quotes = list(b3_cotahist.parse_lines(lines))

    assert [q.ticker for q in quotes] == ["HGLG11"]  # stock, Fiagro and FI-Infra lines are skipped
    q = quotes[0]
    assert q.trade_date == date(2026, 10, 1)
    assert q.isin == "BRHGLGCTF004"
    assert q.close > 0 and q.low <= q.close <= q.high
    assert q.volume > 0 and q.trades > 0


def test_cotahist_bdi14_includes_fiagro_and_infra():
    lines = (FIXTURES / "cotahist_sample.txt").read_text(encoding="latin-1").splitlines()
    tickers = {q.ticker for q in b3_cotahist.parse_lines(lines, bdi_codes={"14"})}
    assert tickers == {"KNCA11", "JURO11"}


def test_cvm_fii_2026_layout():
    funds, reports = cvm_fii.parse_zip((FIXTURES / "inf_mensal_fii_sample.zip").read_bytes())

    by_cnpj = {f.cnpj: f for f in funds}
    assert set(by_cnpj) == {"00332266000131", "11728688000147"}
    via = by_cnpj["00332266000131"]
    assert via.name == "VIA PARQUE SHOPPING FII RESP LTDA"
    assert via.isin == "BRFVPQCTF015"
    assert via.segment == "Shoppings"
    assert via.listed is True
    assert via.last_report_month == date(2026, 8, 1)

    jan = next(r for r in reports if r.cnpj == "00332266000131" and r.ref_month == date(2026, 1, 1))
    assert jan.version == 2
    assert jan.values["shareholders"] == 3639
    assert jan.values["net_assets"] == Decimal("258202136.67")
    assert jan.values["nav_per_share"] == Decimal("92.2101419138767")
    assert jan.values["dividend_yield_ratio"] == Decimal("0.004342")
    assert jan.values["admin_fee_ratio"] == Decimal("0.000219")
    assert jan.raw["Rendimentos_Distribuir"] == "1132796.07"
    assert "Nome_Fundo" not in jan.raw  # registry columns are not repeated in raw
    assert len(reports) == 16


def test_cvm_fii_2016_layout_uses_old_column_names():
    funds, reports = cvm_fii.parse_zip((FIXTURES / "inf_mensal_fii_2016_sample.zip").read_bytes())
    assert len(funds) == 1 and funds[0].cnpj and funds[0].name
    assert reports and all(r.cnpj == funds[0].cnpj for r in reports)


def test_fnet_income_notice():
    [d] = fnet.parse_xml((FIXTURES / "fnet_aviso_rendimento.xml").read_bytes())
    assert d.ticker == "EGAF11"
    assert d.kind == "income"
    assert d.base_date == date(2026, 10, 2)
    assert d.payment_date == date(2026, 10, 9)
    assert d.amount_per_share == Decimal("1.26")
    assert d.tax_exempt is True
    assert d.fund_cnpj == "41224330000148"


def test_fnet_income_and_amortization_in_one_notice():
    items = fnet.parse_xml((FIXTURES / "fnet_aviso_amortizacao.xml").read_bytes())
    assert [(d.kind, d.amount_per_share) for d in items] == [
        ("income", Decimal("0.09")),
        ("amortization", Decimal("0.33")),
    ]
    assert all(d.ticker == "RTEL16" for d in items)


def test_cvm_fii_implausible_ratios_are_nulled_and_kept_raw():
    import io
    import zipfile

    src = zipfile.ZipFile(FIXTURES / "inf_mensal_fii_sample.zip")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        for name in src.namelist():
            text = src.read(name).decode("latin-1")
            if "complemento" in name:
                # Jan/2026 VIA PARQUE: effective return reported as 10675991303 instead of a fraction.
                text = text.replace(";0.005242;", ";10675991303;", 1)
            dst.writestr(name, text.encode("latin-1"))

    _, reports = cvm_fii.parse_zip(out.getvalue())
    jan = next(r for r in reports if r.cnpj == "00332266000131" and r.ref_month == date(2026, 1, 1))
    assert jan.values["return_effective_ratio"] is None
    assert jan.raw["Percentual_Rentabilidade_Efetiva_Mes"] == "10675991303"
    assert jan.values["dividend_yield_ratio"] == Decimal("0.004342")
