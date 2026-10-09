"""CVM data for FI-Infra (funds under the general fund rules, RCVM 175 annex I): fund registry
and daily report.

- Registry: registro_fundo_classe.zip (~7 MB) with registro_classe.csv (one row per class; the
  listed share is the class CNPJ), registro_fundo.csv (administrator, joined by ID_Registro_Fundo;
  one CNPJ may have several cancelled registrations) and registro_subclasse.csv. No ISIN anywhere,
  so the ticker -> CNPJ link comes from `fiidb watch add --cnpj` and is checked against B3.
- Daily report: inf_diario_fi_YYYYMM.zip (~11 MB, all funds in Brazil), one row per class and day
  with NAV per share, net assets and shareholders. Only the CNPJs of FI-Infra funds in the
  database are kept.
"""

import csv
import io
import logging
import zipfile
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import httpx
import psycopg

from fiidb import dates, db
from fiidb import http as fhttp
from fiidb.dates import parse_date
from fiidb.sources.cvm_fii import digits

log = logging.getLogger(__name__)

SOURCE_REGISTRY = "cvm_fi_registro"
SOURCE_DAILY = "cvm_fi_inf_diario"
REGISTRY_URL = "https://dados.cvm.gov.br/dados/FI/CAD/DADOS/registro_fundo_classe.zip"
DAILY_URL = "https://dados.cvm.gov.br/dados/FI/DOC/INF_DIARIO/DADOS/inf_diario_fi_{month:%Y%m}.zip"

INFRA_HINTS = ("INFRA", "INCENTIVAD", "12.431")


@dataclass
class FundClass:
    cnpj: str
    name: str
    status: str | None
    condominium: str | None  # Aberto | Fechado (listed funds are closed)
    target_audience: str | None
    administrator_name: str | None
    administrator_cnpj: str | None
    started_at: date | None

    def looks_like_infra(self) -> bool:
        return any(hint in self.name.upper() for hint in INFRA_HINTS)


@dataclass
class DailyReport:
    cnpj: str
    ref_date: date
    nav_per_share: Decimal | None
    net_assets: Decimal | None
    total_value: Decimal | None
    shareholders: int | None


def _text(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _decimal(value: str | None) -> Decimal | None:
    value = _text(value)
    return Decimal(value) if value else None


def _read_csv(archive: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    text = archive.read(name).decode("latin-1")
    return list(csv.DictReader(io.StringIO(text), delimiter=";"))


def parse_registry(data: bytes, cnpjs: set[str] | None = None) -> dict[str, FundClass]:
    """Classes by CNPJ (digits), optionally only the given ones. When a CNPJ has several
    registrations, an active one wins over a cancelled one, then the most recent status."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        classes = _read_csv(archive, "registro_classe.csv")
        funds = {row["ID_Registro_Fundo"]: row for row in _read_csv(archive, "registro_fundo.csv")}

    def rank(row: dict[str, str]) -> tuple:
        return (not (row.get("Situacao") or "").startswith("Cancelad"), row.get("Data_Inicio_Situacao") or "")

    best: dict[str, dict[str, str]] = {}
    for row in classes:
        cnpj = digits(row.get("CNPJ_Classe"))
        if not cnpj or (cnpjs is not None and cnpj not in cnpjs):
            continue
        if cnpj not in best or rank(row) >= rank(best[cnpj]):
            best[cnpj] = row

    out = {}
    for cnpj, row in best.items():
        fund = funds.get(row["ID_Registro_Fundo"], {})
        out[cnpj] = FundClass(
            cnpj=cnpj,
            name=_text(row.get("Denominacao_Social")) or cnpj,
            status=_text(row.get("Situacao")),
            condominium=_text(row.get("Forma_Condominio")),
            target_audience=_text(row.get("Publico_Alvo")),
            administrator_name=_text(fund.get("Administrador")),
            administrator_cnpj=digits(fund.get("CNPJ_Administrador")),
            started_at=parse_date(row.get("Data_Inicio")),
        )
    return out


def parse_daily(data: bytes, cnpjs: set[str]) -> list[DailyReport]:
    """Daily rows of the given CNPJs (digits). Subclass rows are skipped (listed classes have none);
    a non-positive NAV per share becomes None."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        text = archive.read(archive.namelist()[0]).decode("latin-1")
    out: dict[tuple[str, date], DailyReport] = {}
    for row in csv.DictReader(io.StringIO(text), delimiter=";"):
        cnpj = digits(row.get("CNPJ_FUNDO_CLASSE"))
        ref_date = parse_date(row.get("DT_COMPTC"))
        if cnpj not in cnpjs or ref_date is None or _text(row.get("ID_SUBCLASSE")):
            continue
        nav = _decimal(row.get("VL_QUOTA"))
        shareholders = _decimal(row.get("NR_COTST"))
        out[(cnpj, ref_date)] = DailyReport(
            cnpj=cnpj,
            ref_date=ref_date,
            nav_per_share=nav if nav is not None and nav > 0 else None,
            net_assets=_decimal(row.get("VL_PATRIM_LIQ")),
            total_value=_decimal(row.get("VL_TOTAL")),
            shareholders=int(shareholders) if shareholders is not None else None,
        )
    return list(out.values())


def funds(conn: psycopg.Connection) -> dict[str, int]:
    """CNPJ -> fund id of the FI-Infra funds in the database (created by `fiidb watch add --cnpj`)."""
    return dict(conn.execute("select cnpj, id from fund where type = 'FIINFRA'").fetchall())


FUND_COLUMNS = ["cnpj", "type", "name", "target_audience", "administrator_name", "administrator_cnpj", "started_at"]


def load_registry(conn: psycopg.Connection, classes: list[FundClass]) -> int:
    rows = db.upsert(
        conn,
        "fund",
        FUND_COLUMNS,
        (
            [c.cnpj, "FIINFRA", c.name, c.target_audience, c.administrator_name, c.administrator_cnpj, c.started_at]
            for c in classes
        ),
        key=["cnpj"],
        where="fund.type = 'FIINFRA'",  # never turn a FII into something else
    )
    conn.execute("update fund set updated_at = now() where cnpj = any(%s)", ([c.cnpj for c in classes],))
    return rows


def ingest_registry(
    conn: psycopg.Connection, http: httpx.Client, cnpjs: set[str], *, force: bool = False
) -> dict[str, FundClass] | None:
    """Create or refresh the given FI-Infra funds from the CVM registry. Returns the classes found,
    or None when the file is unchanged since the last run (and not forced)."""
    started = dates.now()
    data = fhttp.fetch(http, REGISTRY_URL)
    if data is None:
        raise RuntimeError(f"{REGISTRY_URL}: not found")
    digest = fhttp.sha256(data)
    if not force and db.last_run(conn, SOURCE_REGISTRY, "registro") == ("ok", digest):
        log.info("%s: unchanged", SOURCE_REGISTRY)
        return None
    classes = parse_registry(data, cnpjs)
    with conn.transaction():
        rows = load_registry(conn, list(classes.values()))
        db.record_run(
            conn,
            source=SOURCE_REGISTRY,
            period="registro",
            status="ok",
            started_at=started,
            url=REGISTRY_URL,
            sha256=digest,
            rows=rows,
        )
    return classes


def load_daily(conn: psycopg.Connection, ids: dict[str, int], reports: list[DailyReport]) -> int:
    return db.upsert(
        conn,
        "daily_report",
        ["fund_id", "ref_date", "nav_per_share", "net_assets", "total_value", "shareholders"],
        ([ids[r.cnpj], r.ref_date, r.nav_per_share, r.net_assets, r.total_value, r.shareholders] for r in reports),
        key=["fund_id", "ref_date"],
    )


def ingest_daily(
    conn: psycopg.Connection, http: httpx.Client, month: date, ids: dict[str, int], *, force: bool = False
) -> str:
    """Load one month of the daily report for the funds in `ids` (CNPJ -> fund id). Skips when the
    file is unchanged since the last successful run; pass force after adding a fund. Returns the
    run status."""
    period = f"{month:%Y-%m}"
    url = DAILY_URL.format(month=month)
    started = dates.now()
    data = fhttp.fetch(http, url)
    if data is None:
        log.info("%s %s: not published", SOURCE_DAILY, period)
        return "missing"
    digest = fhttp.sha256(data)
    if not force and db.last_run(conn, SOURCE_DAILY, period) == ("ok", digest):
        log.info("%s %s: unchanged", SOURCE_DAILY, period)
        return "unchanged"
    with conn.transaction():
        rows = load_daily(conn, ids, parse_daily(data, set(ids)))
        db.record_run(
            conn, source=SOURCE_DAILY, period=period, status="ok", started_at=started, url=url, sha256=digest, rows=rows
        )
    log.info("%s %s: %d daily reports upserted", SOURCE_DAILY, period, rows)
    return "ok"


def recent_months(today: date | None = None) -> list[date]:
    """The previous and the current month: CVM keeps republishing both."""
    today = today or dates.today()
    current = today.replace(day=1)
    previous = (current.replace(day=1) - date.resolution).replace(day=1)
    return [previous, current]


def catch_up(conn: psycopg.Connection, http: httpx.Client, *, today: date | None = None) -> None:
    """Refresh the registry and the recent daily reports of the FI-Infra funds, if there are any."""
    ids = funds(conn)
    if not ids:
        return
    ingest_registry(conn, http, set(ids))
    for month in recent_months(today):
        ingest_daily(conn, http, month, ids)


def shares_ratio(conn: psycopg.Connection, fund_id: int, shares: Decimal) -> Decimal | None:
    """shares x latest NAV per share / net assets. Close to 1 when `shares` (from B3) belong to this
    fund: the check behind a CNPJ given by hand. None without a daily report."""
    row = conn.execute(
        "select nav_per_share, net_assets from daily_report where fund_id = %s and nav_per_share is not null "
        "order by ref_date desc limit 1",
        (fund_id,),
    ).fetchone()
    if row is None or not row[1]:
        return None
    return shares * row[0] / row[1]
