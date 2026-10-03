import logging
from datetime import date, timedelta
from typing import Annotated

import typer

from fiidb import config, dates, db, linking, seeds, watchlist
from fiidb import http as fhttp
from fiidb.sources import b3_cotahist, cvm_fii, fnet

log = logging.getLogger("fiidb")

app = typer.Typer(no_args_is_help=True, help="fiidb ingestion pipeline")
watch_app = typer.Typer(no_args_is_help=True, help="Funds whose FundosNET distributions are ingested")
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
        try:
            b3_cotahist.catch_up(conn, http, settings.start_year, cache_dir=settings.cache_dir)
        except Exception:
            log.exception("b3_cotahist failed")
            failed.append("b3_cotahist")
        failed += _fnet_latest(conn)
        _relink(conn)
        log.info("fund overrides loaded: %d", seeds.load_overrides(conn, settings.seeds_dir / "fund_overrides.csv"))
    if failed:
        typer.echo(f"failed: {', '.join(failed)}", err=True)
        raise typer.Exit(1)


def _relink(conn) -> None:
    with conn.transaction():
        links = linking.link_securities(conn)
    log.info("tickers linked to funds: %s", links)


def _fnet_latest(conn) -> list[str]:
    """Latest notices for every watchlist fund. Returns the tickers that failed."""
    failed = []
    with fhttp.client(timeout=fnet.TIMEOUT) as http:
        for entry in watchlist.entries(conn):
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
            try:
                if fnet.ingest_history(conn, http, entry.cnpj, since, force=force):
                    watchlist.mark_history(conn, entry.ticker, since or watchlist.FULL_HISTORY)
                else:
                    failed.append(entry.ticker)
            except Exception:
                log.exception("fnet history %s failed", entry.ticker)
                failed.append(entry.ticker)
    return failed


@app.command("fnet-latest")
def fnet_latest_cmd() -> None:
    """Fetch the latest FundosNET notices for every watchlist fund (run often)."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        failed = _fnet_latest(conn)
        _relink(conn)
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
        entries = watchlist.entries(conn, [t.upper() for t in tickers])
        missing = {t.upper() for t in tickers} - {e.ticker for e in entries}
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
    cnpj: str | None = typer.Option(None, help="Fund CNPJ, for a ticker not linked to a CVM fund (one ticker only)"),
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
                entry = watchlist.add(conn, ticker, _cnpj_digits(cnpj) if cnpj else None)
            except ValueError as exc:
                typer.echo(str(exc), err=True)
                continue
            typer.echo(f"added {entry.ticker} ({entry.cnpj} {entry.name or ''})")
            added.append(entry)
        if history and added:
            failed = _fnet_history(conn, added, dates.today() - timedelta(days=DEFAULT_HISTORY_DAYS))
            _relink(conn)
            if failed:
                typer.echo(f"history incomplete, retry with `fiidb fnet-history`: {', '.join(failed)}", err=True)
    if len(added) < len(tickers):
        raise typer.Exit(1)


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
            typer.echo(f"{ticker.upper()}: {'removed' if watchlist.remove(conn, ticker) else 'not in the watchlist'}")


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


@app.command()
def seed() -> None:
    """Reload manual data from seeds/ (also done at the end of catch-up)."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        typer.echo(f"{seeds.load_overrides(conn, settings.seeds_dir / 'fund_overrides.csv')} fund overrides")


@app.command()
def status() -> None:
    """Show row counts and the latest runs."""
    settings = config.load()
    with db.connect(settings.database_url) as conn:
        for table in ("fund", "monthly_report", "security", "quote_daily", "distribution", "watchlist"):
            (count,) = conn.execute(f"select count(*) from {table}").fetchone()
            typer.echo(f"{table:16} {count:>10}")
        typer.echo("")
        for row in conn.execute(
            "select distinct on (source) source, period, status, rows, finished_at "
            "from ingestion_run order by source, started_at desc"
        ):
            typer.echo("  ".join(str(v) for v in row))
