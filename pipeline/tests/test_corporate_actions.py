from datetime import date, timedelta
from decimal import Decimal

from conftest import FIXTURES

from fiidb import dates, db, linking, watchlist
from fiidb.sources import b3_funds


def test_parse_b3_split():
    [action] = b3_funds.parse((FIXTURES / "b3_fund_tepp.json").read_bytes())
    assert (action.isin, action.kind, action.last_date_prior) == ("BRTEPPCTF006", "DESDOBRAMENTO", date(2025, 11, 27))
    assert action.factor_raw == "900,00000000000"
    assert action.multiplier == Decimal(10)


def test_multiplier_conventions():
    assert b3_funds.multiplier("BONIFICACAO", "10,00000000000") == Decimal("1.1")
    assert b3_funds.multiplier("GRUPAMENTO", "10,00000000000") is None  # not confirmed: never applied
    assert b3_funds.parse(b"{}") == []


def add_distribution(conn, doc_id, base_date, amount):
    conn.execute("insert into fnet_document (id, version, fund_cnpj, status) values (%s, 1, 'x', 'A')", (doc_id,))
    conn.execute(
        "insert into distribution (fnet_document_id, isin, ticker, kind, base_date, amount_per_share) "
        "values (%s, 'BRHGLGCTF004', 'HGLG11', 'income', %s, %s)",
        (doc_id, base_date, amount),
    )


def test_distributions_before_a_split_are_restated(conn, load_fixtures):
    load_fixtures(conn)
    linking.link_securities(conn)
    watchlist.add(conn, "HGLG11")
    watchlist.mark_history(conn, "HGLG11", watchlist.FULL_HISTORY)
    split = dates.today() - timedelta(days=60)
    add_distribution(conn, 1, split - timedelta(days=30), Decimal("0.74"))  # paid on the old share
    add_distribution(conn, 2, split, Decimal("0.75"))  # data-com on the split day: still the old share
    add_distribution(conn, 3, split + timedelta(days=30), Decimal("0.074"))
    db.upsert(
        conn,
        "corporate_action",
        b3_funds.COLUMNS,
        [["BRHGLGCTF004", split, "DESDOBRAMENTO", "HGLG11", "900,00000000000", Decimal(10), None]],
        key=["isin", "last_date_prior", "kind"],
    )

    adjusted = conn.execute("select amount_adjusted from distribution_adjusted order by base_date").fetchall()
    assert [a for (a,) in adjusted] == [Decimal("0.074"), Decimal("0.075"), Decimal("0.074")]
    assert conn.execute("select income_12m from fund_metrics where ticker = 'HGLG11'").fetchone() == (
        Decimal("0.22300000"),
    )


def test_unexplained_price_jump_is_reported(conn, load_fixtures):
    load_fixtures(conn)
    day = date(2026, 10, 1)
    conn.execute(
        "insert into quote_daily (ticker, trade_date, isin, close) values ('HGLG11', %s, 'BRHGLGCTF004', 1470)",
        (day - timedelta(days=1),),
    )
    [(ticker, *_)] = b3_funds.unexplained_jumps(conn, ["HGLG11"])
    assert ticker == "HGLG11"

    db.upsert(
        conn,
        "corporate_action",
        b3_funds.COLUMNS,
        [["BRHGLGCTF004", day - timedelta(days=1), "DESDOBRAMENTO", "HGLG11", "900", Decimal(10), None]],
        key=["isin", "last_date_prior", "kind"],
    )
    assert b3_funds.unexplained_jumps(conn, ["HGLG11"]) == []
