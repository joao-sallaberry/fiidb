"""Ticker changes: a fund keeps its shares but trades under a new code (and ISIN), e.g. NAVT11 -> VTOT11."""

from datetime import date, timedelta
from decimal import Decimal

from fiidb import dates, db, linking, seeds, sheets, watchlist
from fiidb.sources import b3_funds

OLD_ISIN, NEW_ISIN = "BRHGLGCTF004", "BRHGLXCTF000"  # HGLG11 trades in the fixture on 2026-10-01 only


def add_new_ticker(conn, *, linked: bool = True, close: Decimal = Decimal(160)) -> None:
    """HGLX11 starts trading the next day; linked to the HGLG11 fund unless told otherwise."""
    conn.execute(
        """
        insert into security (ticker, isin, bdi_code, short_name, first_trade_date, last_trade_date, fund_id, link_method)
        select 'HGLX11', %s, bdi_code, short_name, '2026-10-02', '2026-10-03',
            case when %s then fund_id end, case when %s then 'fnet' end
        from security where ticker = 'HGLG11'
        """,
        (NEW_ISIN, linked, linked),
    )
    for day in (date(2026, 10, 2), date(2026, 10, 3)):
        conn.execute(
            "insert into quote_daily (ticker, trade_date, isin, close, volume) values ('HGLX11', %s, %s, %s, 1000)",
            (day, NEW_ISIN, close),
        )


def add_distribution(conn, doc_id: int, isin: str, ticker: str, base_date: date, amount: str) -> None:
    conn.execute("insert into fnet_document (id, version, fund_cnpj, status) values (%s, 1, 'x', 'A')", (doc_id,))
    conn.execute(
        "insert into distribution (fnet_document_id, isin, ticker, kind, base_date, amount_per_share) "
        "values (%s, %s, %s, 'income', %s, %s)",
        (doc_id, isin, ticker, base_date, Decimal(amount)),
    )


def watched_hglg(conn, load_fixtures) -> None:
    load_fixtures(conn)
    linking.link_securities(conn)
    watchlist.add(conn, "HGLG11")
    watchlist.mark_history(conn, "HGLG11", watchlist.FULL_HISTORY)


def test_change_is_detected_and_metrics_follow_it(conn, load_fixtures):
    watched_hglg(conn, load_fixtures)
    add_new_ticker(conn)
    today = dates.today()
    add_distribution(conn, 1, OLD_ISIN, "HGLG11", today - timedelta(days=100), "1.10")
    add_distribution(conn, 2, NEW_ISIN, "HGLX11", today - timedelta(days=20), "1.20")
    linking.link_securities(conn)

    assert conn.execute("select old_ticker, new_ticker, change_date, source from ticker_change").fetchall() == [
        ("HGLG11", "HGLX11", date(2026, 10, 2), "auto")
    ]
    assert watchlist.follow_ticker_changes(conn) == [("HGLG11", "HGLX11")]
    assert watchlist.follow_ticker_changes(conn) == []  # idempotent
    [entry] = watchlist.entries(conn)
    assert (entry.ticker, entry.history_since) == ("HGLX11", watchlist.FULL_HISTORY)

    rows = conn.execute(
        "select ticker, price, last_income, income_12m, watched, previous_tickers from fund_metrics "
        "where ticker in ('HGLG11', 'HGLX11')"
    ).fetchall()
    assert rows == [("HGLX11", Decimal("160.0000"), Decimal("1.2"), Decimal("2.3"), True, "HGLG11")]

    header, row = sheets.build_rows(conn)
    record = dict(zip(header, row, strict=True))
    assert (record["ticker"], record["tickers_anteriores"]) == ("HGLX11", "HGLG11")


def test_payment_stated_under_both_isins_counts_once(conn, load_fixtures):
    watched_hglg(conn, load_fixtures)
    add_new_ticker(conn)
    base = dates.today() - timedelta(days=20)
    add_distribution(conn, 1, OLD_ISIN, "HGLG11", base, "1.20")
    add_distribution(conn, 2, NEW_ISIN, "HGLX11", base, "1.20")
    linking.link_securities(conn)
    watchlist.follow_ticker_changes(conn)
    assert conn.execute("select income_12m from fund_metrics where ticker = 'HGLX11'").fetchone() == (Decimal("1.2"),)


def test_split_under_the_new_ticker_restates_earlier_distributions(conn, load_fixtures):
    watched_hglg(conn, load_fixtures)
    add_new_ticker(conn)
    add_distribution(conn, 1, OLD_ISIN, "HGLG11", date(2026, 9, 15), "1.00")
    linking.link_securities(conn)
    db.upsert(
        conn,
        "corporate_action",
        b3_funds.COLUMNS,
        [[NEW_ISIN, date(2026, 10, 2), "DESDOBRAMENTO", "HGLX11", "900", Decimal(10), None]],
        key=["isin", "last_date_prior", "kind"],
    )
    assert conn.execute("select amount_adjusted from distribution_adjusted").fetchone() == (Decimal("0.1"),)


def test_price_jump_across_the_change_is_reported(conn, load_fixtures):
    load_fixtures(conn)
    linking.link_securities(conn)
    add_new_ticker(conn, close=Decimal(16))  # HGLG11 closed ~160: a 1:10 split on the change day
    linking.link_securities(conn)
    [(ticker, prev_date, trade_date, *_)] = b3_funds.unexplained_jumps(conn, ["HGLX11"])
    assert (ticker, prev_date, trade_date) == ("HGLX11", date(2026, 10, 1), date(2026, 10, 2))

    db.upsert(  # listed under the old ISIN
        conn,
        "corporate_action",
        b3_funds.COLUMNS,
        [[OLD_ISIN, date(2026, 10, 1), "DESDOBRAMENTO", "HGLG11", "900", Decimal(10), None]],
        key=["isin", "last_date_prior", "kind"],
    )
    assert b3_funds.unexplained_jumps(conn, ["HGLX11"]) == []


def test_manual_change_for_an_unlinked_ticker(conn, load_fixtures, tmp_path):
    watched_hglg(conn, load_fixtures)
    add_new_ticker(conn, linked=False)
    linking.link_securities(conn)
    assert conn.execute("select count(*) from ticker_change").fetchone() == (0,)

    seed = tmp_path / "ticker_changes.csv"
    seed.write_text("old_ticker,new_ticker,note\nHGLG11,HGLX11,announcement\nAAAA11,BBBB11,not traded yet\n")
    assert seeds.load_ticker_changes(conn, seed) == 2
    assert conn.execute("select old_ticker, source from ticker_change").fetchall() == [("HGLG11", "manual")]
    assert watchlist.current_ticker(conn, "HGLG11") == "HGLX11"
    assert watchlist.lineage(conn, "HGLX11") == {"HGLG11", "HGLX11"}


def test_watchlist_with_both_tickers_is_merged(conn, load_fixtures):
    watched_hglg(conn, load_fixtures)
    add_new_ticker(conn)
    conn.execute("insert into watchlist (ticker) values ('HGLX11')")  # added before the change was detected
    linking.link_securities(conn)
    watchlist.follow_ticker_changes(conn)
    assert conn.execute("select ticker, cnpj, history_since from watchlist").fetchall() == [
        ("HGLX11", None, watchlist.FULL_HISTORY)
    ]
    # Adding the old code again adds the current one.
    assert watchlist.add(conn, "HGLG11").ticker == "HGLX11"
