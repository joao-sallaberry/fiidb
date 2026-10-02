import logging
from datetime import date

import typer

from fiidb import config, dates, db, linking, seeds
from fiidb import http as fhttp
from fiidb.sources import b3_cotahist, cvm_fii

log = logging.getLogger("fiidb")

app = typer.Typer(no_args_is_help=True, help="fiidb ingestion pipeline")


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
        with conn.transaction():
            links = linking.link_securities(conn)
        log.info("tickers linked to funds: %s", links)
        log.info("fund overrides loaded: %d", seeds.load_overrides(conn, settings.seeds_dir / "fund_overrides.csv"))
    if failed:
        typer.echo(f"failed: {', '.join(failed)}", err=True)
        raise typer.Exit(1)


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
        for table in ("fund", "monthly_report", "security", "quote_daily", "distribution"):
            (count,) = conn.execute(f"select count(*) from {table}").fetchone()
            typer.echo(f"{table:16} {count:>10}")
        typer.echo("")
        for row in conn.execute(
            "select distinct on (source) source, period, status, rows, finished_at "
            "from ingestion_run order by source, started_at desc"
        ):
            typer.echo("  ".join(str(v) for v in row))
