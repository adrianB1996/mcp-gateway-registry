"""Unit tests for scripts/brain_eval/measure_deployment.py (the I/O wrapper)."""

import argparse
from datetime import UTC, datetime

import pytest
from brain_eval import measure_deployment

pytestmark = pytest.mark.unit


class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def __aiter__(self):
        self._iter = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as stop:
            raise StopAsyncIteration from stop


class _Collection:
    def __init__(self, docs):
        self.docs = docs
        self.queries = []

    def find(self, query, projection):
        self.queries.append((query, projection))
        return _Cursor(self.docs)


def _args(**overrides) -> argparse.Namespace:
    values = {
        "days": 7,
        "episode_gap_minutes": 30,
        "repeat_search_minutes": 5,
        "overlap_threshold": 0.5,
        "shared_min_users": None,
        "gateway_server": "mcpgw",
        "search_tool": "search_registry",
        "skill_tool": "get_skill_content",
        "top": 20,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class TestBuildConfig:
    def test_reads_the_user_floor_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("BRAIN_SHARED_MIN_USERS", "5")

        config = measure_deployment._build_config(_args())

        assert config.shared_min_users == 5

    def test_prefers_the_flag_over_the_environment(self, monkeypatch):
        monkeypatch.setenv("BRAIN_SHARED_MIN_USERS", "5")

        config = measure_deployment._build_config(_args(shared_min_users=2))

        assert config.shared_min_users == 2


class TestFetchServers:
    @pytest.mark.asyncio
    async def test_drops_disabled_servers_by_default(self):
        db = {
            "servers": _Collection(
                [{"_id": "/a", "is_enabled": True}, {"_id": "/b", "is_enabled": False}]
            )
        }

        servers = await measure_deployment._fetch_servers(db, "servers", include_disabled=False)

        assert [s["path"] for s in servers] == ["/a"]

    @pytest.mark.asyncio
    async def test_fails_loudly_when_the_collection_is_empty(self):
        db = {"servers": _Collection([])}

        with pytest.raises(measure_deployment.MeasurementError, match="holds no servers"):
            await measure_deployment._fetch_servers(db, "servers", include_disabled=False)


class TestFetchToolCalls:
    @pytest.mark.asyncio
    async def test_reads_only_the_projected_fields_in_the_window(self):
        since = datetime(2026, 11, 1, tzinfo=UTC)
        collection = _Collection(
            [
                {
                    "log_type": "mcp_server_access",
                    "timestamp": datetime(2026, 11, 2),
                    "identity": {"username": "a"},
                    "mcp_server": {"name": "ledger"},
                    "mcp_request": {"method": "tools/call", "tool_name": "x"},
                    "mcp_response": {"status": "success"},
                }
            ]
        )

        calls = await measure_deployment._fetch_tool_calls({"audit": collection}, "audit", since)

        query, projection = collection.queries[0]
        assert len(calls) == 1
        assert query["timestamp"] == {"$gte": since}
        assert "request" not in projection and "identity" not in projection

    @pytest.mark.asyncio
    async def test_fails_loudly_and_names_the_audit_setting_when_nothing_is_found(self):
        db = {"audit": _Collection([])}

        with pytest.raises(measure_deployment.MeasurementError, match="AUDIT_LOG_MONGODB_ENABLED"):
            await measure_deployment._fetch_tool_calls(db, "audit", datetime.now(UTC))


class TestOpenDirect:
    @pytest.mark.asyncio
    async def test_uses_the_connection_string_namespace_and_ttl_from_the_environment(
        self, monkeypatch
    ):
        monkeypatch.setenv(
            "MONGODB_CONNECTION_STRING", "mongodb://localhost:27017/?directConnection=true"
        )
        monkeypatch.setenv("DOCUMENTDB_NAMESPACE", "prod")
        monkeypatch.setenv("AUDIT_LOG_MONGODB_TTL_DAYS", "14")
        monkeypatch.delenv("DOCUMENTDB_DATABASE", raising=False)
        args = argparse.Namespace(host=None, namespace=None, tls_ca_file=None)

        db, collection_name, ttl_days, close = await measure_deployment._open_direct(args)
        await close()

        assert (db.name, collection_name("audit_events"), ttl_days) == (
            "mcp_registry",
            "audit_events_prod",
            14,
        )

    @pytest.mark.asyncio
    async def test_prefers_the_namespace_flag_over_the_environment(self, monkeypatch):
        monkeypatch.setenv("MONGODB_CONNECTION_STRING", "mongodb://localhost:27017/")
        monkeypatch.setenv("DOCUMENTDB_NAMESPACE", "prod")
        args = argparse.Namespace(host=None, namespace="staging", tls_ca_file=None)

        _, collection_name, _, close = await measure_deployment._open_direct(args)
        await close()

        assert collection_name("mcp_servers") == "mcp_servers_staging"


class TestMain:
    @pytest.mark.asyncio
    async def test_writes_a_report_without_usernames(self, monkeypatch, tmp_path):
        output = tmp_path / "report.json"
        monkeypatch.setattr("sys.argv", ["measure_deployment.py", "--output", str(output)])
        calls = [
            measure_deployment.ToolCall(
                "alice@example.com",
                datetime(2026, 11, 1, 9, tzinfo=UTC),
                "mcpgw",
                "search_registry",
            ),
            measure_deployment.ToolCall(
                "alice@example.com", datetime(2026, 11, 1, 9, 1, tzinfo=UTC), "ledger", "x"
            ),
        ]

        async def _fake_collect(args, config):
            return [{"path": "/ledger", "tool_list": [{"name": "x"}]}], calls

        monkeypatch.setattr(measure_deployment, "_collect", _fake_collect)

        exit_code = await measure_deployment.main()

        text = output.read_text()
        assert exit_code == 0
        assert '"followed_by_backend_call": 1' in text
        assert "alice" not in text

    @pytest.mark.asyncio
    async def test_returns_a_failure_code_when_the_deployment_has_no_data(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["measure_deployment.py"])

        async def _empty_collect(args, config):
            raise measure_deployment.MeasurementError("no data")

        monkeypatch.setattr(measure_deployment, "_collect", _empty_collect)

        assert await measure_deployment.main() == 1
