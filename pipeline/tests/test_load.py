"""Loader tests against a real Postgres. Set FIIDB_TEST_DATABASE_URL to a throwaway database:
the public schema is dropped and recreated from db/migrations."""

import os
from pathlib import Path

import psycopg
import pytest

from fiidb import linking
from fiidb.sources import b3_cotahist, cvm_fii

FIXTURES = Path(__file__).parent / "fixtures"
MIGRATIONS = Path(__file__).parents[2] / "db" / "migrations"
URL = os.environ.get("FIIDB_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not URL, reason="FIIDB_TEST_DATABASE_URL not set")


@pytest.fixture
def conn():
    with psycopg.connect(URL, autocommit=True) as setup:
        setup.execute("drop schema public cascade; create schema public")
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            setup.execute(migration.read_text().split("-- migrate:down")[0])
    with psycopg.connect(URL) as c:
        yield c


def load_fixtures(conn):
    funds, reports = cvm_fii.parse_zip((FIXTURES / "inf_mensal_fii_sample.zip").read_bytes())
    cvm_fii.load(conn, funds, reports)
    lines = (FIXTURES / "cotahist_sample.txt").read_text(encoding="latin-1").splitlines()
    b3_cotahist.load(conn, list(b3_cotahist.parse_lines(lines)))


def counts(conn):
    return [conn.execute(f"select count(*) from {t}").fetchone()[0] for t in ("fund", "monthly_report", "quote_daily")]


def test_loading_twice_is_idempotent(conn):
    load_fixtures(conn)
    first = counts(conn)
    load_fixtures(conn)
    assert counts(conn) == first == [2, 16, 1]


def test_older_report_version_does_not_overwrite_newer(conn):
    load_fixtures(conn)
    funds, reports = cvm_fii.parse_zip((FIXTURES / "inf_mensal_fii_sample.zip").read_bytes())
    for r in reports:
        r.version = 0  # older than every stored row (versions start at 1)
        r.values["shareholders"] = -1
    cvm_fii.load(conn, funds, reports)
    assert conn.execute("select min(shareholders) from monthly_report").fetchone()[0] > 0


def test_ticker_linked_to_fund_by_isin(conn):
    load_fixtures(conn)
    assert linking.link_securities(conn)["isin"] == 1
    cnpj, method = conn.execute(
        "select f.cnpj, s.link_method from security s join fund f on f.id = s.fund_id where s.ticker = 'HGLG11'"
    ).fetchone()
    assert (cnpj, method) == ("11728688000147", "isin")


def test_fund_profile_category_and_override(conn, tmp_path):
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
