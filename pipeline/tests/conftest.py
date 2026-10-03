"""Shared fixtures. Database tests need FIIDB_TEST_DATABASE_URL pointing to a throwaway database:
its public schema is dropped and recreated from db/migrations for every test."""

import os
from pathlib import Path

import psycopg
import pytest

from fiidb.sources import b3_cotahist, cvm_fii

FIXTURES = Path(__file__).parent / "fixtures"
MIGRATIONS = Path(__file__).parents[2] / "db" / "migrations"
DATABASE_URL = os.environ.get("FIIDB_TEST_DATABASE_URL")


@pytest.fixture
def conn():
    if not DATABASE_URL:
        pytest.skip("FIIDB_TEST_DATABASE_URL not set")
    with psycopg.connect(DATABASE_URL, autocommit=True) as setup:
        setup.execute("drop schema public cascade; create schema public")
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            setup.execute(migration.read_text().split("-- migrate:down")[0])
    with psycopg.connect(DATABASE_URL) as c:
        yield c


@pytest.fixture
def load_fixtures():
    """Loads the CVM and COTAHIST samples (HGLG11 and VIA PARQUE); callable more than once."""

    def load(conn):
        funds, reports = cvm_fii.parse_zip((FIXTURES / "inf_mensal_fii_sample.zip").read_bytes())
        cvm_fii.load(conn, funds, reports)
        lines = (FIXTURES / "cotahist_sample.txt").read_text(encoding="latin-1").splitlines()
        b3_cotahist.load(conn, list(b3_cotahist.parse_lines(lines)))

    return load
