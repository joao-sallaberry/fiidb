import json
from datetime import timedelta
from decimal import Decimal
from urllib.parse import unquote

import httpx

from fiidb import config, dates, linking, sheets, watchlist


def mock_api(existing_tabs: list[str]):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, unquote(request.url.path), json.loads(request.content or b"null")))
        if request.method == "GET":
            return httpx.Response(200, json={"sheets": [{"properties": {"title": t}} for t in existing_tabs]})
        return httpx.Response(200, json={})

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_write_creates_tab_writes_and_clears_leftover_rows():
    http, calls = mock_api(existing_tabs=["Carteira"])
    sheets.write(http, "SHEET", "fiidb", [["ticker"], ["HGLG11"], ["KNRI11"]])

    assert [(m, p) for m, p, _ in calls] == [
        ("GET", "/v4/spreadsheets/SHEET"),
        ("POST", "/v4/spreadsheets/SHEET:batchUpdate"),
        ("PUT", "/v4/spreadsheets/SHEET/values/'fiidb'!A1"),
        ("POST", "/v4/spreadsheets/SHEET/values/'fiidb'!A4:ZZ:clear"),
    ]
    assert calls[1][2] == {"requests": [{"addSheet": {"properties": {"title": "fiidb"}}}]}
    assert calls[2][2] == {"values": [["ticker"], ["HGLG11"], ["KNRI11"]]}


def test_write_reuses_existing_tab():
    http, calls = mock_api(existing_tabs=["fiidb"])
    sheets.write(http, "SHEET", "fiidb", [["ticker"]])
    assert "POST" not in [m for m, p, _ in calls if p.endswith(":batchUpdate")]


def test_build_rows_only_watchlist(conn, load_fixtures):
    load_fixtures(conn)
    linking.link_securities(conn)
    assert sheets.build_rows(conn) == [[h for h, _ in sheets.COLUMNS] + ["atualizado_em"]]  # empty watchlist

    watchlist.add(conn, "HGLG11")
    watchlist.mark_history(conn, "HGLG11", dates.today() - timedelta(days=400))
    header, row = sheets.build_rows(conn)
    record = dict(zip(header, row, strict=True))
    assert record["ticker"] == "HGLG11"
    assert record["vp_cota"] == float(Decimal("165.951408188109"))
    assert record["mes_ref_vp"] == "2026-08-01"
    assert record["rendimentos_12m"] == 0.0  # complete history, no distributions in the fixture
    assert record["ultimo_rendimento"] == ""


def test_dotenv(tmp_path):
    env = tmp_path / ".env"
    env.write_text('# comment\nFIIDB_SHEET_ID="abc"\n\nFIIDB_SHEET_TAB = dados\n', encoding="utf-8")
    assert config._dotenv(env) == {"FIIDB_SHEET_ID": "abc", "FIIDB_SHEET_TAB": "dados"}
    assert config._dotenv(tmp_path / "missing") == {}


def test_key_id_instead_of_json_key_is_explained(tmp_path):
    import pytest

    key = tmp_path / "key.json"
    key.write_text("0123456789abcdef0123456789abcdef01234567", encoding="utf-8")
    with pytest.raises(sheets.CredentialsError, match="not a service account JSON key"):
        sheets.load_credentials(key)
    with pytest.raises(sheets.CredentialsError, match="not found"):
        sheets.load_credentials(tmp_path / "missing.json")
