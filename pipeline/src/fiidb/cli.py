import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated

import typer

from fiidb import config, dates, db, linking, seeds, sheets, watchlist
from fiidb import http as fhttp
from fiidb.sources import b3_cotahist, b3_funds, cvm_fi, cvm_fiagro, cvm_fii, fnet

log = logging.getLogger("fiidb")

app = typer.Typer(no_args_is_help=True, help="fiidb ingestion pipeline")
watch_app = typer.Typer(no_args_is_help=True, help="Funds whose distributions are ingested (FII, Fiagro, FI-Infra)")
app.add_typer(watch_app, name="watch")

# Enough FundosNET history for DY 12m, with margin for notices delivered before the data-com.
DEFAULT_HISTORY_DAYS = 400


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


@app.command("catch-up")
def catch_up() -> None:
    """Ingest everything missing since the last run (safe to run repeatedly)."""
    settings = config.load()
    failed = []
    with db.connect(settings.database_url) as conn, fhttp.client() as http:
        for year in range(settings.start_year, dates.today().year + 1):
            try:
                cvm_fii.ingest_year(conn, http, year)
            except Exception as exc:
                log.exception("cvm_fii %s failed", year)
                failed.append(f"cvm_fii {year}")
                db.record_run(
                    conn,
                    source=cvm_fii.SOURCE,
                    period=str(year),
                    status="error",
                    started_at=dates.now(),
                    error=str(exc),
                )
        for month in cvm_fiagro.months():
            try:
                cvm_fiagro.ingest_month(conn, http, month)
            except Exception as exc:
                log.exception("cvm_fiagro %s failed", f"{month:%Y-%m}")
                failed.append(f"cvm_fiagro {month:%Y-%m}")
                db.record_run(
                    conn,
                    source=cvm_fiagro.SOURCE,
                    period=f"{month:%Y-%m}",
                    status="error",
                    started_at=dates.now(),
                    error=str(exc),
                )
        try:
            b3_cotahist.catch_up(conn, http, settings.start_year, cache_dir=settings.cache_dir)
        except Exception:
            log.exception("b3_cotahist failed")
            failed.append("b3_cotahist")
        try:
            cvm_fi.catch_up(conn, http)
        except Exception:
            log.exception("cvm_fi failed")
            failed.append("cvm_fi")
        _relink(conn)  # new tickers and ticker changes before fetching the watchlist funds
        failed += _fnet_latest(conn)
        _relink(conn)
        _load_seeds(conn, settings)
        failed += _publish_sheet(conn, settings)
    if failed:
        typer.echo(f"failed: {', '.join(failed)}", err=True)
        raise typer.Exit(1)


def _publish_sheet(conn, settings: config.Settings) -> list[str]:
    """Rewrite the spreadsheet tab when one is configured. Returns ["sheets"] on failure."""
    if not settings.sheet_id:
        log.info("sheets: FIIDB_SHEET_ID not set; skipped")
        return []
    try:
        sheets.publish(conn, settings.sheet_id, settings.sheet_tab, settings.google_credentials)
    except Exception:
        log.exception("sheets publish failed")
        return ["sheets"]
    return []


def _load_seeds(conn, settings: config.Settings) -> None:
    changes = seeds.load_ticker_changes(conn, settings.seeds_dir / "ticker_changes.csv")
    log.info("manual ticker changes loaded: %d", changes)
    log.info("fund overrides loaded: %d", seeds.load_overrides(conn, settings.seeds_dir / "fund_overrides.csv"))
    actions = seeds.load_corporate_actions(conn, settings.seeds_dir / "corporate_actions.csv")
    log.info("manual corporate actions loaded: %d", actions)
    _follow_ticker_changes(conn)


def _relink(conn) -> None:
    with conn.transaction():
        links = linking.link_securities(conn)
    log.info("tickers linked to funds: %s", links)
    _follow_ticker_changes(conn)


def _follow_ticker_changes(conn) -> None:
    for old, new in watchlist.follow_ticker_changes(conn):
        log.warning(
            "%s now trades as %s: watchlist updated; the sheet row is now %s (update lookups and GOOGLEFINANCE)",
            old,
            new,
            new,
        )


def _b3_funds(conn, entries: list[watchlist.Entry]) -> tuple[list[str], dict[str, b3_funds.Page]]:
    """Refresh B3 splits/groupings of the given watchlist funds, plus the cash distributions of
    FI-Infra funds, and warn about unexplained price jumps. Returns what failed and the pages."""
    failed, pages = [], {}
    with fhttp.client(timeout=b3_funds.TIMEOUT) as http:
        for entry in entries:
            try:
                pages[entry.ticker] = b3_funds.ingest(
                    conn, http, entry.ticker, cash_cnpj=entry.cnpj if entry.is_infra else None
                )
            except Exception:
                log.exception("b3_funds %s failed", entry.ticker)
                failed.append(f"b3_funds {entry.ticker}")
                continue
            if entry.is_infra:  # the page covers the last ~12 months of distributions
                watchlist.mark_history(conn, entry.ticker, b3_funds.window_start(dates.today()))
    tickers = [e.ticker for e in entries]
    for ticker, prev_date, trade_date, prev_close, close in b3_funds.unexplained_jumps(conn, tickers):
        log.warning(
            "%s: price %s -> %s between %s and %s with no split/grouping listed by B3; check it",
            ticker,
            prev_close,
            close,
            prev_date,
            trade_date,
        )
    return failed, pages


def _fnet_latest(conn) -> list[str]:
    """Latest notices and corporate actions for every watchlist fund (B3 distributions for FI-Infra).
    Returns what failed."""
    failed, _ = _b3_funds(conn, watchlist.entries(conn))
    with fhttp.client(timeout=fnet.TIMEOUT) as http:
        for entry in watchlist.entries(conn):
            if entry.is_infra:  # FundosNET has no FI-Infra notices; see _b3_funds
                continue
            if entry.cnpj is None:
                log.warning("watchlist %s: no CNPJ (ticker no longer linked?); skipped", entry.ticker)
                failed.append(f"fnet {entry.ticker}")
                continue
            try:
                count = fnet.ingest_latest(conn, http, entry.cnpj)
                log.info("fnet %s: %d new or changed documents", entry.ticker, count)
            except Exception:
                log.exception("fnet %s failed", entry.ticker)
                failed.append(f"fnet {entry.ticker}")
    return failed


def _fnet_history(conn, entries: list[watchlist.Entry], since: date | None, *, force: bool = False) -> list[str]:
    failed = []
    with fhttp.client(timeout=fnet.TIMEOUT) as http:
        for entry in entries:
            if entry.is_infra:
                log.info(
                    "%s: FI-Infra distributions come from the B3 page (last ~12 months), not FundosNET", entry.ticker
                )
                continue
            try:
                if fnet.ingest_history(conn, http, entry.cnpj, since, force=force):
                    watchlist.mark_history(conn, entry.ticker, since or watchlist.FULL_HISTORY)
                else:
                    failed.append(entry.ticker)
                named = watchlist.notice_tickers(conn, entry.cnpj)
                if named and named.isdisjoint(watchlist.lineage(conn, entry.ticker)):
                    log.warning(
                        "%s: the notices of CNPJ %s name %s, not %s; wrong fund? fix with `fiidb watch add %s --cnpj ...`",
                        entry.ticker,
                        entry.cnpj,
                        ", ".join(sorted(named)),
                        entry.ticker,
                        entry.ticker,
                    )
            except Exception:
                log.exception("fnet history %s failed", entry.ticker)
                failed.append(entry.ticker)
    return failed


@app.command("fnet-latest")
def fnet_latest_cmd() -> None:
    """Fetch the latest FundosNET notices (FII, Fiagro), B3 distributions (FI-Infra) and B3 corporate actions
    for every watchlist fund (run often)."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        failed = _fnet_latest(conn)
        _relink(conn)
        failed += _publish_sheet(conn, settings)
    if failed:
        typer.echo(f"failed: {', '.join(failed)}", err=True)
        raise typer.Exit(1)


@app.command("fnet-history")
def fnet_history_cmd(
    tickers: Annotated[list[str], typer.Argument(help="Watchlist tickers")],
    since: str | None = typer.Option(None, help="Only notices delivered on/after YYYY-MM-DD (default: all)"),
    force: bool = typer.Option(False, help="Re-download documents already processed (after a parser fix)"),
) -> None:
    """Load the FundosNET history of some watchlist funds (slow; run ad hoc)."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        tickers = [watchlist.current_ticker(conn, t.strip().upper()) for t in tickers]
        entries = watchlist.entries(conn, tickers)
        missing = set(tickers) - {e.ticker for e in entries}
        if missing:
            raise typer.BadParameter(f"not in the watchlist: {', '.join(sorted(missing))}")
        failed = _fnet_history(conn, entries, date.fromisoformat(since) if since else None, force=force)
        _relink(conn)
    if failed:
        typer.echo(f"incomplete (retry later): {', '.join(failed)}", err=True)
        raise typer.Exit(1)


@watch_app.command("add")
def watch_add(
    tickers: list[str],
    cnpj: str | None = typer.Option(
        None, help="Fund CNPJ, for a ticker not linked to a CVM fund, e.g. a FI-Infra (one ticker only)"
    ),
    history: bool = typer.Option(
        True, help=f"Load the last {DEFAULT_HISTORY_DAYS} days of notices (needed for DY 12m)"
    ),
) -> None:
    """Add tickers to the watchlist."""
    if cnpj and len(tickers) > 1:
        raise typer.BadParameter("--cnpj applies to a single ticker")
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        added = []
        for ticker in tickers:
            try:
                if cnpj and _is_infra_candidate(conn, ticker.strip().upper(), _cnpj_digits(cnpj)):
                    entry = _add_infra(conn, ticker.strip().upper(), _cnpj_digits(cnpj))
                else:
                    entry = watchlist.add(conn, ticker, _cnpj_digits(cnpj) if cnpj else None)
            except ValueError as exc:
                typer.echo(str(exc), err=True)
                continue
            typer.echo(f"added {entry.ticker} ({entry.cnpj} {entry.name or ''})")
            added.append(entry)
        if added:
            _relink(conn)
            _, pages = _b3_funds(conn, added)
            for entry in added:
                if entry.is_infra and entry.ticker in pages:
                    _check_infra_shares(conn, entry, pages[entry.ticker].shares)
        if history and added:
            failed = _fnet_history(conn, added, dates.today() - timedelta(days=DEFAULT_HISTORY_DAYS))
            _relink(conn)
            if failed:
                typer.echo(f"history incomplete, retry with `fiidb fnet-history`: {', '.join(failed)}", err=True)
    if len(added) < len(tickers):
        raise typer.Exit(1)


def _is_infra_candidate(conn, ticker: str, cnpj: str) -> bool:
    """A BDI 14 ticker whose CNPJ is not a FII or Fiagro known from the CVM monthly reports."""
    security = conn.execute("select bdi_code from security where ticker = %s", (ticker,)).fetchone()
    fund = conn.execute("select type from fund where cnpj = %s", (cnpj,)).fetchone()
    if fund is not None and fund[0] in ("FII", "FIAGRO"):
        return False
    if security is None:
        raise ValueError(f"{ticker}: not in COTAHIST; run `fiidb catch-up` first")
    return security[0] == "14"


def _add_infra(conn, ticker: str, cnpj: str) -> watchlist.Entry:
    """Create the FI-Infra fund from the CVM registry, load its recent daily reports and add it."""
    with fhttp.client() as http:
        classes = cvm_fi.ingest_registry(conn, http, {cnpj}, force=True)
        found = classes.get(cnpj)
        if found is None:
            raise ValueError(f"{ticker}: CNPJ {cnpj} not found in the CVM fund registry")
        if not found.looks_like_infra():
            log.warning("%s: %s does not look like a FI-Infra (%s); check the CNPJ", ticker, found.name, cnpj)
        if found.status and found.status.startswith("Cancelad"):
            log.warning("%s: CVM registry says %r for %s", ticker, found.status, cnpj)
        entry = watchlist.add(conn, ticker, cnpj)
        ids = {cnpj: cvm_fi.funds(conn)[cnpj]}
        for month in cvm_fi.recent_months():
            cvm_fi.ingest_daily(conn, http, month, ids, force=True)
    return watchlist.entries(conn, [entry.ticker])[0]


# Shares from B3 x NAV per share from CVM must give the net assets when the CNPJ is right.
SHARES_TOLERANCE = Decimal("0.02")


def _check_infra_shares(conn, entry: watchlist.Entry, shares: Decimal | None) -> None:
    (fund_id,) = conn.execute("select id from fund where cnpj = %s", (entry.cnpj,)).fetchone()
    ratio = cvm_fi.shares_ratio(conn, fund_id, shares) if shares else None
    if ratio is None:
        log.warning("%s: could not check the CNPJ (no B3 share count or CVM daily report)", entry.ticker)
    elif abs(ratio - 1) > SHARES_TOLERANCE:
        log.warning(
            "%s: B3 shares x CVM NAV per share = %.1f%% of the net assets of %s; wrong CNPJ? "
            "fix with `fiidb watch add %s --cnpj ...`",
            entry.ticker,
            ratio * 100,
            entry.cnpj,
            entry.ticker,
        )
    else:
        log.info("%s: CNPJ %s checked (B3 shares x NAV = %.2f%% of net assets)", entry.ticker, entry.cnpj, ratio * 100)


def _cnpj_digits(cnpj: str) -> str:
    digits = "".join(ch for ch in cnpj if ch.isdigit())
    if len(digits) != 14:
        raise typer.BadParameter(f"invalid CNPJ: {cnpj}")
    return digits


@watch_app.command("remove")
def watch_remove(tickers: list[str]) -> None:
    """Remove tickers from the watchlist (their distributions stay in the database)."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        for ticker in tickers:
            ticker = watchlist.current_ticker(conn, ticker.strip().upper())
            typer.echo(f"{ticker}: {'removed' if watchlist.remove(conn, ticker) else 'not in the watchlist'}")


@watch_app.command("list")
def watch_list() -> None:
    """Show the watchlist with history coverage and the latest income."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        for e in watchlist.entries(conn):
            row = conn.execute(
                "select last_income, last_income_base_date, dividend_yield_12m from fund_metrics where ticker = %s",
                (e.ticker,),
            ).fetchone() or (None, None, None)
            history = "full" if e.history_since == watchlist.FULL_HISTORY else (e.history_since or "none")
            dy = f"{row[2]:.2%}" if row[2] is not None else "-"
            typer.echo(
                f"{e.ticker:8} {e.cnpj or '?':14}  history={history!s:10}  last={row[0]} on {row[1]}  dy12m={dy}"
            )


@app.command("cvm-fii")
def cvm_fii_cmd(year: int, force: bool = typer.Option(False, help="Reload even if unchanged")) -> None:
    """Load one year of CVM FII monthly reports."""
    settings = config.load()
    with db.connect(settings.database_url) as conn, fhttp.client() as http:
        typer.echo(cvm_fii.ingest_year(conn, http, year, force=force))


@app.command("cvm-fiagro")
def cvm_fiagro_cmd(
    month: Annotated[str, typer.Argument(help="Reference month, YYYY-MM")],
    force: bool = typer.Option(False, help="Reload even if unchanged"),
) -> None:
    """Load one month of CVM Fiagro monthly reports."""
    settings = config.load()
    with db.connect(settings.database_url) as conn, fhttp.client() as http:
        typer.echo(cvm_fiagro.ingest_month(conn, http, date.fromisoformat(f"{month}-01"), force=force))


@app.command("cotahist")
def cotahist_cmd(
    year: int | None = typer.Option(None, help="Load a yearly file"),
    day: str | None = typer.Option(None, help="Load a daily file (YYYY-MM-DD)"),
) -> None:
    """Load one COTAHIST file."""
    if (year is None) == (day is None):
        raise typer.BadParameter("pass exactly one of --year or --day")
    settings = config.load()
    with db.connect(settings.database_url) as conn, fhttp.client() as http:
        if year is not None:
            rows = b3_cotahist.ingest_year(conn, http, year, cache_dir=settings.cache_dir)
        else:
            rows = b3_cotahist.ingest_day(conn, http, date.fromisoformat(day))
    typer.echo("not published" if rows is None else f"{rows} quotes")


@app.command("sheets")
def sheets_cmd() -> None:
    """Rewrite the spreadsheet tab with the watchlist funds (also done by catch-up and fnet-latest)."""
    settings = config.load()
    if not settings.sheet_id:
        raise typer.BadParameter("set FIIDB_SHEET_ID in .env (see README)")
    try:
        sheets.load_credentials(settings.google_credentials)
    except sheets.CredentialsError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    with db.connect(settings.database_url) as conn:
        typer.echo(f"{sheets.publish(conn, settings.sheet_id, settings.sheet_tab, settings.google_credentials)} funds")


@app.command()
def seed() -> None:
    """Reload manual data from seeds/ (also done at the end of catch-up)."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        _load_seeds(conn, settings)


@app.command()
def status() -> None:
    """Show row counts and the latest runs."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        for table in ("fund", "monthly_report", "daily_report", "security", "quote_daily", "distribution", "watchlist"):
            (count,) = conn.execute(f"select count(*) from {table}").fetchone()
            typer.echo(f"{table:16} {count:>10}")
        typer.echo("")
        for row in conn.execute(
            "select distinct on (source) source, period, status, rows, finished_at "
            "from ingestion_run order by source, started_at desc"
        ):
            typer.echo("  ".join(str(v) for v in row))
