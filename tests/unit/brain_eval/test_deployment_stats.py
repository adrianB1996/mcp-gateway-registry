"""Unit tests for scripts/brain_eval/deployment_stats.py."""

from datetime import UTC, datetime, timedelta

import pytest
from brain_eval.deployment_stats import (
    AnalysisConfig,
    Episode,
    ToolCall,
    build_report,
    split_episodes,
    summarize_episodes,
    summarize_identity,
    summarize_repeats,
    summarize_searches,
    summarize_tool_collisions,
    tool_call_from_audit_event,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


def _call(
    caller: str | None,
    minute: float,
    server: str,
    tool: str,
) -> ToolCall:
    return ToolCall(
        caller=caller, timestamp=T0 + timedelta(minutes=minute), server=server, tool=tool
    )


def _episode(
    caller: str,
    *calls: tuple[float, str, str],
) -> Episode:
    return Episode(caller=caller, calls=[_call(caller, m, s, t) for m, s, t in calls])


def _audit(
    **overrides,
) -> dict:
    doc = {
        "log_type": "mcp_server_access",
        "timestamp": datetime(2026, 11, 1, 9, 0),
        "identity": {"username": "alice@example.com"},
        "mcp_server": {"name": "ledger"},
        "mcp_request": {"method": "tools/call", "tool_name": "list_entries"},
        "mcp_response": {"status": "success"},
    }
    doc.update(overrides)
    return doc


@pytest.fixture
def config() -> AnalysisConfig:
    return AnalysisConfig()


class TestToolCollisions:
    def test_counts_names_shared_by_two_or_more_servers(self):
        servers = [
            {"path": "/ledger", "tool_list": [{"name": "get_item", "description": "Get an item"}]},
            {"path": "/stock", "tool_list": [{"name": "get_item", "description": "Get an item"}]},
            {"path": "/crm", "tool_list": [{"name": "get_item", "description": "Fetch a contact"}]},
            {
                "path": "/files",
                "tool_list": [{"name": "write_xlsx", "description": "Write a sheet"}],
            },
        ]

        stats = summarize_tool_collisions(servers, top_n=5)

        assert (stats.tool_count, stats.shared_name_count, stats.shared_tool_count) == (4, 1, 3)
        assert stats.shared_tool_share == 0.75
        assert stats.servers_per_name_buckets["3-5"] == 1

    def test_flags_shared_names_with_identical_descriptions(self):
        servers = [
            {"path": "/a", "tool_list": [{"name": "get_item", "description": "Get an  ITEM "}]},
            {"path": "/b", "tool_list": [{"name": "get_item", "description": "get an item"}]},
        ]

        stats = summarize_tool_collisions(servers, top_n=5)

        assert stats.shared_names_with_identical_description == 1
        assert stats.top_shared_names[0].distinct_descriptions == 1

    def test_counts_a_tool_repeated_on_one_server_once(self):
        servers = [{"path": "/a", "tool_list": [{"name": "x"}, {"name": "x"}]}]

        stats = summarize_tool_collisions(servers, top_n=5)

        assert (stats.tool_count, stats.shared_name_count) == (1, 0)

    def test_returns_zeros_for_an_empty_registry(self):
        stats = summarize_tool_collisions([], top_n=5)

        assert (stats.server_count, stats.tool_count, stats.shared_tool_share) == (0, 0, 0.0)


class TestToolCallFromAuditEvent:
    def test_keeps_an_authorised_tool_call_as_utc(self):
        call = tool_call_from_audit_event(_audit())

        assert call == ToolCall(
            "alice@example.com", datetime(2026, 11, 1, 9, 0, tzinfo=UTC), "ledger", "list_entries"
        )

    @pytest.mark.parametrize(
        "overrides",
        [
            {"log_type": "registry_api_access"},
            {"mcp_request": {"method": "tools/list"}},
            {"mcp_request": {"method": "tools/call"}},
            {"mcp_response": {"status": "error"}},
            {"mcp_server": {}},
            {"timestamp": "not a date"},
        ],
    )
    def test_rejects_records_that_are_not_authorised_tool_calls(self, overrides):
        assert tool_call_from_audit_event(_audit(**overrides)) is None

    @pytest.mark.parametrize("username", ["anonymous", "", "Unknown", None])
    def test_marks_tokens_without_a_user_as_no_caller(self, username):
        call = tool_call_from_audit_event(_audit(identity={"username": username}))

        assert call is not None and call.caller is None


class TestSplitEpisodes:
    def test_starts_a_new_episode_after_the_idle_gap(self):
        calls = [_call("a", 0, "s", "t"), _call("a", 10, "s", "t"), _call("a", 50, "s", "t")]

        episodes = split_episodes(calls, timedelta(minutes=30))

        assert [len(e.calls) for e in episodes] == [2, 1]

    def test_skips_calls_without_a_caller(self):
        episodes = split_episodes([_call(None, 0, "s", "t")], timedelta(minutes=30))

        assert episodes == []

    def test_orders_calls_by_time_within_an_episode(self):
        calls = [_call("a", 5, "s", "late"), _call("a", 1, "s", "early")]

        episodes = split_episodes(calls, timedelta(minutes=30))

        assert [c.tool for c in episodes[0].calls] == ["early", "late"]


class TestIdentity:
    def test_reports_calls_without_a_user_and_caller_concentration(self):
        calls = [_call("a", 0, "s", "t")] * 3 + [_call("b", 0, "s", "t"), _call(None, 0, "s", "t")]

        stats = summarize_identity(calls)

        assert (stats.calls_without_user, stats.distinct_callers) == (1, 2)
        assert (stats.top_caller_share, stats.top5_caller_share) == (0.6, 0.8)

    def test_holds_no_usernames(self):
        stats = summarize_identity([_call("alice@example.com", 0, "s", "t")])

        assert "alice" not in stats.model_dump_json()


class TestSearches:
    def test_counts_a_search_followed_by_a_backend_call(self, config):
        episode = _episode("a", (0, "mcpgw", "search_registry"), (1, "ledger", "list_entries"))

        stats = summarize_searches([episode], config)

        assert (
            stats.searches,
            stats.followed_by_backend_call,
            stats.backend_calls_after_search,
        ) == (1, 1, 1)

    def test_counts_a_skill_load_after_a_search(self, config):
        episode = _episode("a", (0, "mcpgw", "search_registry"), (1, "mcpgw", "get_skill_content"))

        stats = summarize_searches([episode], config)

        assert (stats.followed_by_skill_load, stats.followed_by_backend_call) == (1, 0)

    def test_counts_a_quick_second_search_as_a_repeat(self, config):
        episode = _episode("a", (0, "mcpgw", "search_registry"), (2, "mcpgw", "search_registry"))

        stats = summarize_searches([episode], config)

        assert stats.repeated_without_call == 1

    def test_ignores_a_second_search_outside_the_repeat_window(self, config):
        episode = _episode("a", (0, "mcpgw", "search_registry"), (20, "mcpgw", "search_registry"))

        stats = summarize_searches([episode], config)

        assert stats.repeated_without_call == 0

    def test_does_not_count_a_search_with_a_call_between_as_a_repeat(self, config):
        episode = _episode(
            "a",
            (0, "mcpgw", "search_registry"),
            (1, "ledger", "list_entries"),
            (2, "mcpgw", "search_registry"),
        )

        stats = summarize_searches([episode], config)

        assert stats.repeated_without_call == 0

    def test_counts_backend_calls_made_without_a_search_first(self, config):
        episode = _episode("a", (0, "ledger", "list_entries"), (1, "mcpgw", "search_registry"))

        stats = summarize_searches([episode], config)

        assert (stats.backend_calls, stats.backend_calls_after_search) == (1, 0)


class TestEpisodes:
    def test_reports_the_share_of_episodes_spanning_two_servers(self, config):
        episodes = [
            _episode("a", (0, "ledger", "list_entries"), (1, "files", "write_xlsx")),
            _episode("b", (0, "ledger", "list_entries")),
            _episode("c", (0, "mcpgw", "search_registry")),
        ]

        stats = summarize_episodes(episodes, config)

        assert (stats.episodes_with_backend_calls, stats.multi_server_episodes) == (2, 1)
        assert stats.multi_server_share == 0.5


class TestRepeats:
    def test_counts_an_episode_that_overlaps_an_earlier_one_by_the_same_caller(self, config):
        episodes = [
            _episode("a", (0, "ledger", "list_entries"), (1, "ledger", "lock_period")),
            _episode(
                "a",
                (100, "ledger", "list_entries"),
                (101, "ledger", "lock_period"),
                (102, "files", "write_xlsx"),
            ),
        ]

        stats = summarize_repeats(episodes, config)

        assert (stats.personal_repeat_episodes, stats.callers_with_a_repeat) == (1, 1)

    def test_does_not_count_an_overlap_below_the_threshold(self, config):
        episodes = [
            _episode("a", (0, "ledger", "list_entries")),
            _episode(
                "a", (100, "ledger", "list_entries"), (101, "files", "a"), (102, "files", "b")
            ),
        ]

        stats = summarize_repeats(episodes, config)

        assert stats.personal_repeat_episodes == 0

    def test_does_not_count_another_callers_episode_as_a_personal_repeat(self, config):
        episodes = [_episode("a", (0, "ledger", "x")), _episode("b", (1, "ledger", "x"))]

        stats = summarize_repeats(episodes, config)

        assert stats.personal_repeat_episodes == 0

    def test_counts_a_tool_set_as_shared_only_at_the_user_floor(self, config):
        episodes = [_episode(user, (0, "ledger", "x"), (1, "files", "y")) for user in ("a", "b")]
        episodes.append(_episode("c", (0, "ledger", "x")))

        below = summarize_repeats(episodes, config)
        above = summarize_repeats(
            episodes + [_episode("d", (0, "ledger", "x"), (1, "files", "y"))], config
        )

        assert below.shared_tool_sets == 0
        assert above.shared_tool_sets == 1
        assert above.top_shared_sets[0].tools == ["files/y", "ledger/x"]


class TestBuildReport:
    def test_builds_a_report_that_holds_no_usernames(self, config):
        servers = [{"path": "/ledger", "tool_list": [{"name": "list_entries"}]}]
        calls = [
            _call("alice@example.com", 0, "mcpgw", "search_registry"),
            _call("alice@example.com", 1, "ledger", "list_entries"),
        ]

        report = build_report(servers, calls, config)

        assert report.searches.followed_by_backend_call == 1
        assert report.window_start == T0
        assert "alice" not in report.model_dump_json()
