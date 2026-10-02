"""CVM "Informe Mensal Estruturado" for FIIs.

One zip per year with three CSVs (latin-1, ';'): geral (registry), complemento (NAV,
shareholders, ratios) and ativo_passivo (balance sheet). Since 2023 the key columns were
renamed from *_Fundo to *_Fundo_Classe (CVM Resolution 175).
"""

import csv
import io
import json
import logging
import zipfile
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import httpx
import psycopg

from fiidb import db
from fiidb import http as fhttp
from fiidb.dates import now, parse_date

log = logging.getLogger(__name__)

SOURCE = "cvm_fii_inf_mensal"
URL = "https://dados.cvm.gov.br/dados/FII/DOC/INF_MENSAL/DADOS/inf_mensal_fii_{year}.zip"

RENAMES = {"CNPJ_Fundo_Classe": "CNPJ_Fundo", "Nome_Fundo_Classe": "Nome_Fundo"}

# complemento columns mapped to monthly_report; everything else non-empty goes to `raw`.
REPORT_COLUMNS = {
    "Cotas_Emitidas": "shares_outstanding",
    "Valor_Ativo": "total_assets",
    "Patrimonio_Liquido": "net_assets",
    "Valor_Patrimonial_Cotas": "nav_per_share",
    "Total_Numero_Cotistas": "shareholders",
    "Percentual_Despesas_Taxa_Administracao": "admin_fee_ratio",
    "Percentual_Rentabilidade_Efetiva_Mes": "return_effective_ratio",
    "Percentual_Rentabilidade_Patrimonial_Mes": "return_nav_ratio",
    "Percentual_Dividend_Yield_Mes": "dividend_yield_ratio",
    "Percentual_Amortizacao_Cotas_Mes": "amortization_ratio",
}
MAX_ABS_RATIO = Decimal(1)  # >100% in a month: almost always a unit error by the administrator
KEY_COLUMNS = {"CNPJ_Fundo", "Data_Referencia", "Versao", "Tipo_Fundo_Classe"}


@dataclass
class Fund:
    cnpj: str
    name: str
    isin: str | None
    segment: str | None
    mandate: str | None
    management_type: str | None
    target_audience: str | None
    administrator_name: str | None
    administrator_cnpj: str | None
    started_at: date | None
    listed: bool | None
    last_report_month: date


@dataclass
class Report:
    cnpj: str
    ref_month: date
    version: int
    delivered_at: date | None
    values: dict[str, Decimal | int | None]
    raw: dict[str, str] = field(default_factory=dict)


def digits(value: str | None) -> str | None:
    if not value:
        return None
    return "".join(ch for ch in value if ch.isdigit()) or None


def _text(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _decimal(value: str | None) -> Decimal | None:
    value = _text(value)
    return Decimal(value.replace(",", ".")) if value else None


def _read_csv(archive: zipfile.ZipFile, kind: str) -> list[dict[str, str]]:
    name = next((n for n in archive.namelist() if f"_{kind}_" in n), None)
    if name is None:
        return []
    text = archive.read(name).decode("latin-1")
    rows = []
    for row in csv.DictReader(io.StringIO(text), delimiter=";"):
        rows.append({RENAMES.get(k, k): v for k, v in row.items()})
    return rows


def _latest_versions(rows: list[dict[str, str]]) -> dict[tuple[str, str], dict[str, str]]:
    """Index rows by (cnpj, ref date), keeping the highest Versao."""
    out: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        key = (digits(row["CNPJ_Fundo"]), row["Data_Referencia"])
        if key not in out or int(row["Versao"] or 0) >= int(out[key]["Versao"] or 0):
            out[key] = row
    return out


def parse_zip(data: bytes) -> tuple[list[Fund], list[Report]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        geral = _latest_versions(_read_csv(archive, "geral"))
        complemento = _latest_versions(_read_csv(archive, "complemento"))
        ativo_passivo = _latest_versions(_read_csv(archive, "ativo_passivo"))

    funds: dict[str, Fund] = {}
    reports: list[Report] = []
    for key, g in geral.items():
        cnpj, ref = key
        ref_month = parse_date(ref)
        if not cnpj or not ref_month:
            continue
        c = complemento.get(key, {})
        version = int(g["Versao"] or 0)

        values: dict[str, Decimal | int | None] = {}
        rejected: dict[str, str] = {}
        for src, dst in REPORT_COLUMNS.items():
            value = _decimal(c.get(src))
            if dst == "shareholders" and value is not None:
                value = int(value)
            elif dst.endswith("_ratio") and value is not None and abs(value) > MAX_ABS_RATIO:
                rejected[src] = c[src].strip()
                value = None
            values[dst] = value
        if values["shares_outstanding"] is None:
            values["shares_outstanding"] = _decimal(g.get("Quantidade_Cotas_Emitidas"))

        raw = {
            k: v.strip()
            for source in (c, ativo_passivo.get(key, {}))
            for k, v in source.items()
            if k not in KEY_COLUMNS and k not in REPORT_COLUMNS and v and v.strip()
        } | rejected
        reports.append(Report(cnpj, ref_month, version, parse_date(g.get("Data_Entrega")), values, raw))

        current = funds.get(cnpj)
        if current is None or ref_month >= current.last_report_month:
            funds[cnpj] = Fund(
                cnpj=cnpj,
                name=_text(g.get("Nome_Fundo")) or cnpj,
                isin=_text(g.get("Codigo_ISIN")),
                segment=_text(g.get("Segmento_Atuacao")),
                mandate=_text(g.get("Mandato")),
                management_type=_text(g.get("Tipo_Gestao")),
                target_audience=_text(g.get("Publico_Alvo")),
                administrator_name=_text(g.get("Nome_Administrador")),
                administrator_cnpj=digits(g.get("CNPJ_Administrador")),
                started_at=parse_date(g.get("Data_Funcionamento")),
                listed={"S": True, "N": False}.get((g.get("Mercado_Negociacao_Bolsa") or "").strip()),
                last_report_month=ref_month,
            )
    return list(funds.values()), reports


FUND_COLUMNS = [
    "cnpj",
    "name",
    "isin",
    "segment",
    "mandate",
    "management_type",
    "target_audience",
    "administrator_name",
    "administrator_cnpj",
    "started_at",
    "listed",
    "last_report_month",
]


def load(conn: psycopg.Connection, funds: list[Fund], reports: list[Report]) -> int:
    db.upsert(
        conn,
        "fund",
        FUND_COLUMNS,
        ([getattr(f, c) for c in FUND_COLUMNS] for f in funds),
        key=["cnpj"],
        # Older yearly files must not overwrite fresher registry data.
        where="fund.last_report_month is null or excluded.last_report_month >= fund.last_report_month",
    )
    conn.execute("update fund set updated_at = now() where cnpj = any(%s)", ([f.cnpj for f in funds],))
    ids = dict(conn.execute("select cnpj, id from fund where cnpj = any(%s)", ([f.cnpj for f in funds],)).fetchall())

    value_cols = list(REPORT_COLUMNS.values())
    columns = ["fund_id", "ref_month", "version", "delivered_at", *value_cols, "raw"]
    return db.upsert(
        conn,
        "monthly_report",
        columns,
        (
            [ids[r.cnpj], r.ref_month, r.version, r.delivered_at, *(r.values[c] for c in value_cols), json.dumps(r.raw)]
            for r in reports
        ),
        key=["fund_id", "ref_month"],
        where="excluded.version >= monthly_report.version",
    )


def ingest_year(conn: psycopg.Connection, http: httpx.Client, year: int, *, force: bool = False) -> str:
    """Download and load one yearly file. Skips when the file is unchanged since the last
    successful run. Returns the run status."""
    url = URL.format(year=year)
    started = now()
    data = fhttp.fetch(http, url)
    if data is None:
        log.info("%s %s: not published", SOURCE, year)
        return "missing"
    digest = fhttp.sha256(data)
    previous = db.last_run(conn, SOURCE, str(year))
    if not force and previous == ("ok", digest):
        log.info("%s %s: unchanged", SOURCE, year)
        return "unchanged"

    with conn.transaction():
        funds, reports = parse_zip(data)
        rows = load(conn, funds, reports)
        db.record_run(
            conn, source=SOURCE, period=str(year), status="ok", started_at=started, url=url, sha256=digest, rows=rows
        )
    log.info("%s %s: %d funds, %d reports upserted", SOURCE, year, len(funds), rows)
    return "ok"
