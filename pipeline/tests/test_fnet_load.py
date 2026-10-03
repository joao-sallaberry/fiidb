"""FundosNET processing against a real Postgres (see conftest.py) and a mocked HTTP server."""

from datetime import timedelta
from decimal import Decimal

import httpx
from conftest import FIXTURES

from fiidb import dates, linking, watchlist
from fiidb.sources import fnet

CNPJ = "11728688000147"  # HGLG11 in the CVM fixture
XML = (
    (FIXTURES / "fnet_aviso_rendimento.xml")
    .read_bytes()
    .replace(b"BREGAFCTF006", b"BRHGLGCTF004")
    .replace(b"41224330000148", CNPJ.encode())
)


def client(documents: dict[int, bytes | Exception]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        body = documents[int(request.url.params["id"])]
        if isinstance(body, Exception):
            raise body
        return httpx.Response(200, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def doc(id_: int, version: int = 1, status: str = "A") -> fnet.Document:
    return fnet.Document(id_, version, status, dates.now())


def amounts(conn):
    return conn.execute("select fnet_document_id, amount_per_share from distribution order by 1").fetchall()


def test_new_changed_and_cancelled_documents(conn):
    with client({1: XML}) as http:
        assert fnet.process(conn, http, CNPJ, [doc(1)]) == (1, 0)
        assert fnet.process(conn, http, CNPJ, [doc(1)]) == (0, 0)  # unchanged: not downloaded again
    assert [a for _, a in amounts(conn)] == [Decimal("1.26")]

    with client({1: XML.replace(b"1.26", b"1.30")}) as http:
        assert fnet.process(conn, http, CNPJ, [doc(1, version=2)]) == (1, 0)  # re-presentation
    assert [a for _, a in amounts(conn)] == [Decimal("1.30")]

    with client({}) as http:
        assert fnet.process(conn, http, CNPJ, [doc(1, version=2, status="C")]) == (1, 0)  # cancelled
    assert amounts(conn) == []


def test_failed_download_is_retried_on_next_run(conn, monkeypatch):
    monkeypatch.setattr(fnet, "RETRIES", 1)
    with client({1: httpx.ReadTimeout("hung")}) as http:
        assert fnet.process(conn, http, CNPJ, [doc(1)]) == (0, 1)
    with client({1: XML}) as http:
        assert fnet.process(conn, http, CNPJ, [doc(1)]) == (1, 0)
    assert len(amounts(conn)) == 1


def test_metrics_dy_requires_complete_history_and_fnet_link(conn, load_fixtures):
    load_fixtures(conn)
    linking.link_securities(conn)
    watchlist.add(conn, "HGLG11")
    recent = XML.replace(b"2026-10-02", dates.today().isoformat().encode())
    with client({1: recent}) as http:
        fnet.process(conn, http, CNPJ, [doc(1)])

    def metrics():
        return conn.execute(
            "select last_income, income_12m, dividend_yield_12m from fund_metrics where ticker = 'HGLG11'"
        ).fetchone()

    last, income, dy = metrics()
    assert (last, income, dy) == (Decimal("1.26"), None, None)  # history not loaded yet

    watchlist.mark_history(conn, "HGLG11", dates.today() - timedelta(days=400))
    last, income, dy = metrics()
    assert income == Decimal("1.26") and dy > 0

    assert linking.link_securities(conn)["fnet"] == 1
    assert conn.execute("select link_method from security where ticker = 'HGLG11'").fetchone() == ("fnet",)
