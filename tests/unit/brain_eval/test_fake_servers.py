"""Unit tests for scripts/brain_eval/fake_servers.py."""

import json

import httpx2
import pytest
from brain_eval import build_corpus
from brain_eval.fake_servers import (
    CallRecorder,
    build_app,
    build_server,
    canned_result,
    load_corpus,
)
from mcp import Client

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def corpus():
    return load_corpus(build_corpus.CORPUS_FILE)


@pytest.fixture
def ledgerline(corpus):
    return next(server for server in corpus.servers if server.name == "ledgerline")


@pytest.fixture
def recorder(tmp_path):
    return CallRecorder(tmp_path / "calls.jsonl")


def _lines(recorder: CallRecorder) -> list[dict]:
    return [json.loads(line) for line in recorder.path.read_text().splitlines()]


class TestCannedResult:
    def test_returns_the_same_result_for_the_same_call(self):
        assert canned_result("s", "post_adjustment", {"a": 1}) == canned_result(
            "s", "post_adjustment", {"a": 1}
        )

    def test_returns_items_for_list_shaped_tools(self):
        result = canned_result("s", "list_journal_entries", {})

        assert len(result["items"]) == 3

    def test_echoes_the_input_for_action_tools(self):
        result = canned_result("s", "lock_period", {"period": "2026-10"})

        assert result["input"] == {"period": "2026-10"}


class TestServerOverMcp:
    @pytest.mark.asyncio
    async def test_lists_every_tool_with_its_corpus_schema(self, ledgerline, recorder):
        async with Client(build_server(ledgerline, recorder)) as client:
            listed = await client.list_tools()

        by_name = {tool.name: tool for tool in listed.tools}
        assert set(by_name) == {tool.name for tool in ledgerline.tools}
        assert by_name["post_adjustment"].input_schema["required"] == [
            "amount",
            "credit_account",
            "debit_account",
            "memo",
        ]
        assert by_name["post_adjustment"].annotations.read_only_hint is False

    @pytest.mark.asyncio
    async def test_records_a_call_and_returns_a_canned_result(self, ledgerline, recorder):
        async with Client(build_server(ledgerline, recorder)) as client:
            result = await client.call_tool("lock_period", {"period": "2026-10"})

        assert result.is_error is False
        assert result.structured_content["tool"] == "lock_period"
        assert _lines(recorder) == [
            {
                "arguments": {"period": "2026-10"},
                "known_tool": True,
                "server": "ledgerline",
                "timestamp": _lines(recorder)[0]["timestamp"],
                "tool": "lock_period",
            }
        ]

    @pytest.mark.asyncio
    async def test_records_and_rejects_an_unknown_tool(self, ledgerline, recorder):
        async with Client(build_server(ledgerline, recorder)) as client:
            result = await client.call_tool("refund_payment", {})

        assert result.is_error is True
        assert _lines(recorder)[0]["known_tool"] is False


class TestApp:
    def test_mounts_only_core_servers_unless_padding_is_asked_for(self, corpus, recorder):
        app = build_app(corpus, recorder, include_padding=False, allowed_hosts=[])

        mounted = {route.path for route in app.routes}
        assert mounted == {f"/{server.name}" for server in corpus.servers if not server.padding}

    @pytest.mark.asyncio
    async def test_answers_a_tool_call_over_streamable_http(self, corpus, recorder):
        app = build_app(corpus, recorder, include_padding=False, allowed_hosts=[])
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_stock_level", "arguments": {"sku": "SKU-1"}},
        }
        headers = {
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": "2025-06-18",
        }

        async with app.router.lifespan_context(app):
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:9100"
            ) as client:
                response = await client.post("/stockroom/mcp", json=request, headers=headers)

        assert response.status_code == 200, response.text
        assert _lines(recorder)[0]["tool"] == "get_stock_level"

    @pytest.mark.asyncio
    async def test_refuses_a_host_header_it_was_not_told_to_accept(self, corpus, recorder):
        app = build_app(corpus, recorder, include_padding=False, allowed_hosts=[])
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        headers = {
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": "2025-06-18",
        }

        async with app.router.lifespan_context(app):
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(
                transport=transport, base_url="http://evil.example"
            ) as client:
                response = await client.post("/stockroom/mcp", json=request, headers=headers)

        assert response.status_code in (400, 403, 421)
