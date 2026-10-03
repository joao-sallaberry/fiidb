"""Loader tests against a real Postgres (see conftest.py)."""

from conftest import FIXTURES

from fiidb import linking
from fiidb.sources import cvm_fii


def counts(conn):
    return [conn.execute(f"select count(*) from {t}").fetchone()[0] for t in ("fund", "monthly_report", "quote_daily")]


def test_loading_twice_is_idempotent(conn, load_fixtures):
    load_fixtures(conn)
    first = counts(conn)
    load_fixtures(conn)
    assert counts(conn) == first == [2, 16, 1]


def test_older_report_version_does_not_overwrite_newer(conn, load_fixtures):
    load_fixtures(conn)
    funds, reports = cvm_fii.parse_zip((FIXTURES / "inf_mensal_fii_sample.zip").read_bytes())
    for r in reports:
        r.version = 0  # older than every stored row (versions start at 1)
        r.values["shareholders"] = -1
    cvm_fii.load(conn, funds, reports)
    assert conn.execute("select min(shareholders) from monthly_report").fetchone()[0] > 0


def test_ticker_linked_to_fund_by_isin(conn, load_fixtures):
    load_fixtures(conn)
    assert linking.link_securities(conn)["isin"] == 1
    cnpj, method = conn.execute(
        "select f.cnpj, s.link_method from security s join fund f on f.id = s.fund_id where s.ticker = 'HGLG11'"
    ).fetchone()
    assert (cnpj, method) == ("11728688000147", "isin")


def test_fund_profile_category_and_override(conn, load_fixtures, tmp_path):
    from fiidb import seeds

    load_fixtures(conn)
    linking.link_securities(conn)
    category, segment, source = conn.execute(
        "select category, segment, source from fund_profile where ticker = 'HGLG11'"
    ).fetchone()
    assert (category, segment, source) == ("Tijolo", None, "computed")  # CVM says Multicategoria

    csv_path = tmp_path / "fund_overrides.csv"
    csv_path.write_text("ticker,category,segment,note\nhglg11,,Logística,\n", encoding="utf-8")
    assert seeds.load_overrides(conn, csv_path) == 1
    assert conn.execute("select category, segment, source from fund_profile where ticker = 'HGLG11'").fetchone() == (
        "Tijolo",
        "Logística",
        "override",
    )

    csv_path.write_text("ticker,category,segment,note\n", encoding="utf-8")
    seeds.load_overrides(conn, csv_path)
    assert conn.execute("select source from fund_profile where ticker = 'HGLG11'").fetchone() == ("computed",)


def test_isin_shared_by_several_funds(conn, load_fixtures):
    load_fixtures(conn)
    # A feeder fund reporting HGLG11's ISIN must not steal the link while it is not listed...
    conn.execute(
        "insert into fund (cnpj, name, isin, listed) values ('99999999000199', 'FEEDER', 'BRHGLGCTF004', false)"
    )
    linking.link_securities(conn)
    assert conn.execute("select f.name from security s join fund f on f.id = s.fund_id").fetchone()[0] != "FEEDER"

    # ...and when two listed funds claim it, the link is dropped instead of guessed.
    conn.execute("update fund set listed = true where cnpj = '99999999000199'")
    linking.link_securities(conn)
    assert conn.execute("select fund_id, link_method from security where ticker = 'HGLG11'").fetchone() == (None, None)
