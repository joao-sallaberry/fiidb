"""B3 FundosNET "Aviso aos Cotistas - Estruturado" (distribution notices as XML).

Ingestion lands in phase 3; for now only the document parser exists.
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from fiidb.dates import parse_date

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


def parse_xml(data: bytes) -> list[Distribution]:
    root = ET.fromstring(data)
    cnpj = _text(root, "DadosGerais/CNPJFundo")
    out = []
    for provento in root.iterfind("InformeRendimentos/Provento"):
        isin = _text(provento, "CodISIN")
        if not isin:
            continue
        for tag, kind in KINDS.items():
            node = provento.find(tag)
            if node is None:
                continue
            base_date = parse_date(_text(node, "DataBase"))
            amount = _decimal(_text(node, "ValorProvento"))
            if base_date is None or not amount:
                continue
            exempt = _text(node, "RendimentoIsentoIR")
            out.append(
                Distribution(
                    isin=isin,
                    ticker=_text(provento, "CodNegociacao"),
                    kind=kind,
                    base_date=base_date,
                    payment_date=parse_date(_text(node, "DataPagamento")),
                    amount_per_share=amount,
                    reference_period=_text(node, "PeriodoReferencia"),
                    tax_exempt={"Sim": True, "Não": False, "Nao": False}.get(exempt) if exempt else None,
                    fund_cnpj=cnpj,
                )
            )
    return out
