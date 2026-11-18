"""CVM "Informe Mensal" for Fiagro (CVM Resolution 175, annex VI).

One zip per month since 2025-05 (<= 75 KB) with two CSVs (latin-1, ';'): the main one, with one row
per class (registry, NAV, shareholders, ratios and balance sheet in 133 columns), and a subclass
one, which is ignored (listed classes have a single subclass).

Until ~2025-08 the Fiagro-Imobiliario funds filed the FII monthly report, so most of them already
exist in `fund` with `type = 'FII'` and their history in `monthly_report`. The same CNPJ keeps going
here: the fund becomes `FIAGRO` and the reports land in the same table.

Unlike the FII report, percentages come as percent (1.30 = 1.30%) and NAV per share has two
decimals only.
"""

import csv
import io
import json
import logging
import zipfile
from datetime import date
from decimal import Decimal

import httpx
import psycopg

from fiidb import dates, db
from fiidb import http as fhttp
from fiidb.dates import parse_date
from fiidb.sources.cvm_fii import FUND_COLUMNS, MAX_ABS_RATIO, Fund, Report, _decimal, _text, digits

log = logging.getLogger(__name__)

SOURCE = "cvm_fiagro_inf_mensal"
URL = "https://dados.cvm.gov.br/dados/FIAGRO/DOC/INF_MENSAL/DADOS/inf_mensal_fiagro_{month:%Y%m}.zip"
FIRST_MONTH = date(2025, 5, 1)

# Columns mapped to monthly_report; everything else non-empty goes to `raw`.
REPORT_COLUMNS = {
    "Cotas_Emitidas": "shares_outstanding",
    "Valor_Ativo": "total_assets",
    "Patrimonio_Liquido": "net_assets",
    "Valor_Patrimonial_Cotas": "nav_per_share",
    "Numero_Cotistas": "shareholders",
    "Percentual_Despesas_Taxa_Administracao": "admin_fee_ratio",
    "Rentabilidade_Efetiva_Mes": "return_effective_ratio",
    "Rentabilidade_Patrimonial_Mes": "return_nav_ratio",
    "Dividend_Yield_Mes": "dividend_yield_ratio",
    "Percentual_Amortizacao_Cotas_Mes": "amortization_ratio",
}
# Registry columns, stored in `fund` (or not worth keeping), so left out of `raw`.
FUND_SOURCE_COLUMNS = {
    "CNPJ_Classe",
    "Nome_Classe",
    "Data_Referencia",
    "Data_Entrega",
    "Versao",
    "CNPJ_Administrador",
    "Nome_Administrador",
    "Data_Registro",
    "Publico_Alvo",
    "Codigo_ISIN",
    "Email_Administrador",
    "Servico_Atendimento_Cotista",
    "Site",
}
# Marks the rows of this report in `raw`: a few funds filed both reports for the same month, and
# the FII loader leaves marked rows alone (see cvm_fii.load), so the result does not depend on order.
RAW_MARKER = {"informe": "fiagro"}
PERCENT = Decimal(100)
NAV_ROUNDING = Decimal("0.005")  # the reported NAV per share has two decimals


def _nav_per_share(reported: Decimal | None, net_assets: Decimal | None, shares: Decimal | None) -> Decimal | None:
    """Net assets / shares when it agrees with the two-decimal reported value (more precision),
    else the reported value. None when non-positive or when the fund has a single share
    (over-the-counter funds whose "share" is the whole fund)."""
    if reported is None or reported <= 0 or shares is None or shares <= 1:
        return None
    if net_assets is not None:
        computed = net_assets / shares
        if abs(computed - reported) <= NAV_ROUNDING:
            return computed.quantize(Decimal("1e-8"))
    return reported


def parse_zip(data: bytes) -> tuple[list[Fund], list[Report]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        name = next(n for n in archive.namelist() if "subclasse" not in n)
        text = archive.read(name).decode("latin-1")

    latest: dict[tuple[str, str], dict[str, str]] = {}  # highest Versao per (cnpj, ref date)
    for row in csv.DictReader(io.StringIO(text), delimiter=";"):
        key = (digits(row.get("CNPJ_Classe")), row.get("Data_Referencia"))
        if key not in latest or int(row["Versao"] or 0) >= int(latest[key]["Versao"] or 0):
            latest[key] = row

    funds: dict[str, Fund] = {}
    reports: list[Report] = []
    for (cnpj, ref), row in latest.items():
        ref_month = parse_date(ref)
        if not cnpj or not ref_month:
            continue
        ref_month = ref_month.replace(day=1)

        values: dict[str, Decimal | int | None] = {}
        rejected: dict[str, str] = {}
        for src, dst in REPORT_COLUMNS.items():
            value = _decimal(row.get(src))
            if dst == "shareholders" and value is not None:
                value = int(value)
            elif dst.endswith("_ratio") and value is not None:
                value = value / PERCENT
                if abs(value) > MAX_ABS_RATIO:
                    rejected[src] = row[src].strip()
                    value = None
            values[dst] = value
        reported_nav = values["nav_per_share"]
        values["nav_per_share"] = _nav_per_share(reported_nav, values["net_assets"], values["shares_outstanding"])
        if reported_nav is not None and values["nav_per_share"] is None:
            rejected["Valor_Patrimonial_Cotas"] = row["Valor_Patrimonial_Cotas"].strip()

        raw = (
            {
                k: v.strip()
                for k, v in row.items()
                if k not in FUND_SOURCE_COLUMNS and k not in REPORT_COLUMNS and v and v.strip()
            }
            | rejected
            | RAW_MARKER
        )
        reports.append(
            Report(cnpj, ref_month, int(row["Versao"] or 0), parse_date(row.get("Data_Entrega")), values, raw)
        )

        current = funds.get(cnpj)
        if current is None or ref_month >= current.last_report_month:
            funds[cnpj] = Fund(
                cnpj=cnpj,
                name=_text(row.get("Nome_Classe")) or cnpj,
                isin=_text(row.get("Codigo_ISIN")),
                segment=None,
                mandate=None,
                management_type=None,
                target_audience=_text(row.get("Publico_Alvo")),
                administrator_name=_text(row.get("Nome_Administrador")),
                administrator_cnpj=digits(row.get("CNPJ_Administrador")),
                started_at=parse_date(row.get("Data_Registro")),
                listed=(row.get("Mercado_Negociacao") or "").strip().upper() == "BOLSA",
                last_report_month=ref_month,
            )
    return list(funds.values()), reports


# Not in this report: on a fund that used to file the FII report, the old values stay.
KEPT_COLUMNS = {"isin", "segment", "mandate", "management_type"}


def load(conn: psycopg.Connection, funds: list[Fund], reports: list[Report]) -> int:
    columns = ["type", *FUND_COLUMNS]
    db.upsert(
        conn,
        "fund",
        columns,
        (["FIAGRO", *(getattr(f, c) for c in FUND_COLUMNS)] for f in funds),
        key=["cnpj"],
        update=[c for c in columns if c != "cnpj" and c not in KEPT_COLUMNS],
        # A fund coming from the FII report becomes FIAGRO; older files never overwrite fresher data.
        where="fund.type <> 'FIINFRA' "
        "and (fund.last_report_month is null or excluded.last_report_month >= fund.last_report_month)",
    )
    cnpjs = [f.cnpj for f in funds]
    # An empty ISIN here must not erase the one reported before (many funds leave it blank).
    with conn.cursor() as cur:
        cur.executemany(
            "update fund set isin = %s where cnpj = %s and type = 'FIAGRO' and last_report_month = %s "
            "and isin is distinct from %s",
            [(f.isin, f.cnpj, f.last_report_month, f.isin) for f in funds if f.isin],
        )
    conn.execute("update fund set updated_at = now() where cnpj = any(%s)", (cnpjs,))
    # Includes funds still typed FII (their FII report is fresher than this file): the report is kept.
    ids = dict(
        conn.execute("select cnpj, id from fund where cnpj = any(%s) and type <> 'FIINFRA'", (cnpjs,)).fetchall()
    )

    value_cols = list(REPORT_COLUMNS.values())
    return db.upsert(
        conn,
        "monthly_report",
        ["fund_id", "ref_month", "version", "delivered_at", *value_cols, "raw"],
        (
            [ids[r.cnpj], r.ref_month, r.version, r.delivered_at, *(r.values[c] for c in value_cols), json.dumps(r.raw)]
            for r in reports
            if r.cnpj in ids
        ),
        key=["fund_id", "ref_month"],
        # Over a row from the FII report this one always wins, whatever the version.
        where="excluded.version >= monthly_report.version or monthly_report.raw ->> 'informe' is distinct from 'fiagro'",
    )


def ingest_month(conn: psycopg.Connection, http: httpx.Client, month: date, *, force: bool = False) -> str:
    """Download and load one monthly file. Skips when the file is unchanged since the last
    successful run. Returns the run status."""
    period = f"{month:%Y-%m}"
    url = URL.format(month=month)
    started = dates.now()
    data = fhttp.fetch(http, url)
    if data is None:
        log.info("%s %s: not published", SOURCE, period)
        return "missing"
    digest = fhttp.sha256(data)
    if not force and db.last_run(conn, SOURCE, period) == ("ok", digest):
        log.info("%s %s: unchanged", SOURCE, period)
        return "unchanged"

    with conn.transaction():
        funds, reports = parse_zip(data)
        rows = load(conn, funds, reports)
        db.record_run(
            conn, source=SOURCE, period=period, status="ok", started_at=started, url=url, sha256=digest, rows=rows
        )
    log.info("%s %s: %d funds, %d reports upserted", SOURCE, period, len(funds), rows)
    return "ok"


def months(today: date | None = None) -> list[date]:
    """Every month with a file, from the first one published to the current month."""
    end = (today or dates.today()).replace(day=1)
    out, month = [], FIRST_MONTH
    while month <= end:
        out.append(month)
        month = date(month.year + month.month // 12, month.month % 12 + 1, 1)
    return out
