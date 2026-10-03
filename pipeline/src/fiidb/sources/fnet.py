"""B3 FundosNET "Aviso aos Cotistas - Estruturado" (distribution notices as XML).

Only funds in the `watchlist` table are queried, one search per fund by CNPJ. The service is
fast when it answers (<1 s) but often hangs or returns 5xx, so requests use a short timeout with
retries, and a failing fund never blocks the others.

A re-presentation keeps the document id and bumps `versao`; cancelled (C) or inactive (I)
documents must not count, so their distributions are removed.
"""

import json
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import httpx
import psycopg

from fiidb import dates, db
from fiidb import http as fhttp
from fiidb.dates import parse_date

log = logging.getLogger(__name__)

SOURCE = "fnet"
SEARCH_URL = "https://fnet.bmfbovespa.com.br/fnet/publico/pesquisarGerenciadorDocumentosDados"
DOCUMENT_URL = "https://fnet.bmfbovespa.com.br/fnet/publico/downloadDocumento"
TIMEOUT = 15  # a hung request does not recover; failing fast and retrying works better
RETRIES = 4
PAGE_SIZE = 200
LATEST_COUNT = 5  # income, amortization and re-presentations may come in separate notices

KINDS = {"Rendimento": "income", "Amortizacao": "amortization"}


@dataclass
class Distribution:
    isin: str
    ticker: str | None
    kind: str
    base_date: date
    payment_date: date | None
    amount_per_share: Decimal
    reference_period: str | None
    tax_exempt: bool | None
    fund_cnpj: str | None


@dataclass
class Document:
    id: int
    version: int
    status: str
    delivered_at: datetime | None


def _text(node: ET.Element, tag: str) -> str | None:
    child = node.find(tag)
    value = (child.text or "").strip() if child is not None else ""
    return value or None


def _decimal(value: str | None) -> Decimal | None:
    if not value:
        return None
    if "," in value:
        value = value.replace(".", "").replace(",", ".")
    return Decimal(value)


def _distribution(node: ET.Element, kind: str, isin: str, ticker: str | None, cnpj: str | None) -> Distribution | None:
    base_date = parse_date(_text(node, "DataBase"))
    amount = _decimal(_text(node, "ValorProvento") or _text(node, "ValorProventoCota"))
    if base_date is None or not amount:
        return None
    exempt = _text(node, "RendimentoIsentoIR")
    return Distribution(
        isin=isin,
        ticker=ticker,
        kind=kind,
        base_date=base_date,
        payment_date=parse_date(_text(node, "DataPagamento")),
        amount_per_share=amount,
        reference_period=_text(node, "PeriodoReferencia"),
        tax_exempt={"sim": True, "true": True, "não": False, "nao": False, "false": False}.get((exempt or "").lower()),
        fund_cnpj=cnpj,
    )


def parse_xml(data: bytes) -> list[Distribution]:
    """Two layouts exist. Since ~Sep/2022 each share class is an InformeRendimentos/Provento with
    its own CodISIN; before, a single class is given in DadosGerais (CodISINCota) and
    Rendimento/Amortizacao sit directly under InformeRendimentos with ValorProventoCota."""
    root = ET.fromstring(data)
    cnpj = _text(root, "DadosGerais/CNPJFundo")
    groups = [
        (provento, _text(provento, "CodISIN"), _text(provento, "CodNegociacao"))
        for provento in root.iterfind("InformeRendimentos/Provento")
    ]
    legacy = root.find("InformeRendimentos")
    if not groups and legacy is not None:
        groups = [(legacy, _text(root, "DadosGerais/CodISINCota"), _text(root, "DadosGerais/CodNegociacaoCota"))]

    out = []
    for node, isin, ticker in groups:
        if not isin:
            continue
        for tag, kind in KINDS.items():
            child = node.find(tag)
            if child is not None and (item := _distribution(child, kind, isin, ticker, cnpj)):
                out.append(item)
    return out


def format_cnpj(digits: str) -> str:
    """FundosNET only filters by the punctuated form (digits-only queries fail with 5xx)."""
    d = digits.zfill(14)
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"


def parse_search(data: bytes) -> tuple[int, list[Document]]:
    """(total records, documents) from a search response."""
    payload = json.loads(data)
    docs = []
    for row in payload.get("data") or []:
        delivered = None
        if row.get("dataEntrega"):
            day, hour = (row["dataEntrega"].split(" ") + ["00:00"])[:2]
            parsed = parse_date(day)
            if parsed:
                h, m = (int(x) for x in hour.split(":"))
                delivered = datetime(parsed.year, parsed.month, parsed.day, h, m, tzinfo=dates.MARKET_TZ)
        docs.append(
            Document(int(row["id"]), int(row.get("versao") or 1), row.get("situacaoDocumento") or "A", delivered)
        )
    return int(payload.get("recordsTotal") or 0), docs


def search(http: httpx.Client, cnpj: str, *, start: int = 0, limit: int = PAGE_SIZE) -> tuple[int, list[Document]]:
    params = {
        "d": 1,
        "s": start,
        "l": limit,
        "o[0][dataEntrega]": "desc",
        "idCategoriaDocumento": 14,  # Aviso aos Cotistas - Estruturado
        "idTipoDocumento": 41,  # Rendimentos e Amortizações
        "idEspecieDocumento": 0,
        "tipoFundo": 1,  # FII
        "cnpjFundo": format_cnpj(cnpj),
    }
    return parse_search(fhttp.fetch(http, SEARCH_URL, params=params, retries=RETRIES) or b"{}")


DISTRIBUTION_COLUMNS = [
    "fnet_document_id",
    "fnet_version",
    "isin",
    "ticker",
    "kind",
    "base_date",
    "payment_date",
    "amount_per_share",
    "reference_period",
    "tax_exempt",
    "fund_cnpj",
    "delivered_at",
]


def _save_document(conn: psycopg.Connection, doc: Document, cnpj: str, error: str | None = None) -> None:
    conn.execute(
        """
        insert into fnet_document (id, version, fund_cnpj, status, delivered_at, fetched_at, error)
        values (%s, %s, %s, %s, %s, now(), %s)
        on conflict (id) do update set version = excluded.version, status = excluded.status,
            delivered_at = excluded.delivered_at, fetched_at = now(), error = excluded.error
        """,
        (doc.id, doc.version, cnpj, doc.status, doc.delivered_at, error),
    )


def process(
    conn: psycopg.Connection, http: httpx.Client, cnpj: str, docs: list[Document], *, force: bool = False
) -> tuple[int, int]:
    """Download new/changed documents (all of them when `force`, e.g. after a parser fix) and store
    their distributions. Returns (documents, failures)."""
    known = {
        id_: (version, status)
        for id_, version, status in conn.execute(
            "select id, version, status from fnet_document where id = any(%s) and error is null",
            ([d.id for d in docs],),
        )
    }
    processed = failures = 0
    for doc in docs:
        if not force and known.get(doc.id) == (doc.version, doc.status):
            continue
        if doc.status != "A":
            with conn.transaction():
                conn.execute("delete from distribution where fnet_document_id = %s", (doc.id,))
                _save_document(conn, doc, cnpj)
            processed += 1
            continue
        try:
            data = fhttp.fetch(http, DOCUMENT_URL, params={"id": doc.id}, retries=RETRIES)
            items = parse_xml(data or b"")
        except (httpx.HTTPError, ET.ParseError) as exc:
            log.warning("%s document %s (fund %s) failed: %s", SOURCE, doc.id, cnpj, exc)
            _save_document(conn, doc, cnpj, error=str(exc) or type(exc).__name__)
            failures += 1
            continue
        rows = {(d.isin, d.kind): d for d in items}  # unique key within a document
        with conn.transaction():
            conn.execute("delete from distribution where fnet_document_id = %s", (doc.id,))
            db.upsert(
                conn,
                "distribution",
                DISTRIBUTION_COLUMNS,
                (
                    [
                        doc.id,
                        doc.version,
                        d.isin,
                        d.ticker,
                        d.kind,
                        d.base_date,
                        d.payment_date,
                        d.amount_per_share,
                        d.reference_period,
                        d.tax_exempt,
                        cnpj,  # the CVM CNPJ the search used; the XML's may be formatted differently
                        doc.delivered_at,
                    ]
                    for d in rows.values()
                ),
                key=["fnet_document_id", "isin", "kind"],
            )
            _save_document(conn, doc, cnpj)
        processed += 1
    return processed, failures


def ingest_latest(conn: psycopg.Connection, http: httpx.Client, cnpj: str, count: int = LATEST_COUNT) -> int:
    started = dates.now()
    _, docs = search(http, cnpj, limit=count)
    processed, failures = process(conn, http, cnpj, docs)
    db.record_run(
        conn,
        source=SOURCE,
        period=f"{cnpj}:latest",
        status="error" if failures else "ok",
        started_at=started,
        rows=processed,
        error=f"{failures} documents failed" if failures else None,
    )
    return processed


def ingest_history(
    conn: psycopg.Connection, http: httpx.Client, cnpj: str, since: date | None = None, *, force: bool = False
) -> bool:
    """Process every notice delivered on or after `since` (all when None). Returns True when
    complete, i.e. every page was read and every document stored."""
    started = dates.now()
    start, processed, failures = 0, 0, 0
    while True:
        total, docs = search(http, cnpj, start=start)
        page = [d for d in docs if since is None or d.delivered_at is None or d.delivered_at.date() >= since]
        done, failed = process(conn, http, cnpj, page, force=force)
        processed, failures = processed + done, failures + failed
        start += len(docs)
        if not docs or start >= total or len(page) < len(docs):
            break
    db.record_run(
        conn,
        source=SOURCE,
        period=f"{cnpj}:history:{since or 'all'}",
        status="error" if failures else "ok",
        started_at=started,
        rows=processed,
        error=f"{failures} documents failed" if failures else None,
    )
    log.info("%s %s history since %s: %d documents, %d failed", SOURCE, cnpj, since or "start", processed, failures)
    return failures == 0
