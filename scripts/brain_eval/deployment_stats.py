"""Analysis behind the Phase 0 deployment measurement.

Everything here takes plain records and returns plain results, with no I/O, so
it can be tested without a database. ``measure_deployment.py`` reads the records
from DocumentDB and calls :func:`build_report`.

The report answers the two questions the design doc asks before any brain code
is written (docs/design/knowledge-graph-search.md, "Measure the problem first"):

- How many tool names collide across servers, which says how much Phase 1 has
  to fix.
- Whether callers search and then call tools, whether they repeat the same set
  of tools, and whether several users use the same set. That says whether
  memory has anything to remember.

The audit records hold no query text, so "the same goal" is measured as "the
same set of tools", which is a lower bound.
"""

import logging
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Audit record type and JSON-RPC method for an MCP tool call.
MCP_ACCESS_LOG_TYPE: str = "mcp_server_access"
TOOLS_CALL_METHOD: str = "tools/call"
SUCCESS_STATUS: str = "success"

# Usernames that mean the token named no user. A caller with one of these
# cannot be told apart from other callers, so its calls are excluded from the
# per-user analysis and counted separately.
NO_USER_NAMES: frozenset[str] = frozenset({"", "anonymous", "unknown", "none", "null"})

# Bucket edges for "how many servers expose this tool name".
SERVERS_PER_NAME_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("2", 2, 2),
    ("3-5", 3, 5),
    ("6-10", 6, 10),
    ("11-25", 11, 25),
    ("26+", 26, 10**9),
)

_WHITESPACE = re.compile(r"\s+")


class AnalysisConfig(BaseModel):
    """Settings for one measurement run."""

    window_days: int = Field(default=7, ge=1, le=365, description="Days of audit records read")
    episode_gap_minutes: int = Field(
        default=30,
        ge=1,
        description="A caller's calls further apart than this start a new episode",
    )
    repeat_search_minutes: int = Field(
        default=5,
        ge=1,
        description="A second search within this many minutes, with no call between, "
        "counts as a repeat (the first answer probably did not help)",
    )
    overlap_threshold: float = Field(
        default=0.5,
        gt=0.0,
        le=1.0,
        description="Jaccard overlap at which two episodes' tool sets count as the same goal",
    )
    shared_min_users: int = Field(
        default=3,
        ge=1,
        description="Distinct users a tool set needs before it counts as shared "
        "(BRAIN_SHARED_MIN_USERS in the design)",
    )
    gateway_server: str = Field(default="mcpgw", description="Audit server name of mcpgw")
    search_tool: str = Field(default="search_registry", description="mcpgw search tool name")
    skill_tool: str = Field(default="get_skill_content", description="mcpgw skill load tool")
    top_n: int = Field(default=20, ge=1, le=500, description="Rows in each top list")


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One authorised MCP tool call, reduced to the fields the analysis needs."""

    caller: str | None
    timestamp: datetime
    server: str
    tool: str


@dataclass(slots=True)
class Episode:
    """A caller's run of calls with no gap longer than the episode gap."""

    caller: str
    calls: list[ToolCall] = field(default_factory=list)


class SharedName(BaseModel):
    """A tool name that more than one server exposes."""

    name: str
    server_count: int
    distinct_descriptions: int


class CollisionStats(BaseModel):
    """How often tool names repeat across servers."""

    server_count: int
    tool_count: int
    distinct_tool_names: int
    shared_name_count: int = Field(description="Tool names exposed by two or more servers")
    shared_tool_count: int = Field(description="Tool entries whose name another server also uses")
    shared_tool_share: float
    shared_names_with_identical_description: int = Field(
        description="Shared names where at least two servers use the same description"
    )
    max_servers_per_name: int
    servers_per_name_buckets: dict[str, int]
    top_shared_names: list[SharedName]


class IdentityStats(BaseModel):
    """Whether calls carry a user identity the brain can learn from."""

    tool_calls: int
    calls_without_user: int
    calls_without_user_share: float
    distinct_callers: int
    top_caller_share: float = Field(
        description="Share of calls from the busiest caller. A high value suggests a shared "
        "service token"
    )
    top5_caller_share: float


class SearchStats(BaseModel):
    """What callers do after a search_registry call."""

    searches: int
    followed_by_backend_call: int
    followed_by_backend_call_share: float
    followed_by_skill_load: int
    repeated_without_call: int = Field(
        description="Searches followed by another search within the repeat window and no call "
        "in between"
    )
    repeated_without_call_share: float
    backend_calls: int
    backend_calls_after_search: int = Field(
        description="Backend calls made after a search earlier in the same episode"
    )
    backend_calls_after_search_share: float


class EpisodeStats(BaseModel):
    """Shape of callers' episodes."""

    episodes: int
    episodes_with_backend_calls: int
    multi_server_episodes: int = Field(description="Episodes calling tools on two or more servers")
    multi_server_share: float = Field(description="Of episodes with backend calls")
    median_backend_calls: float
    tool_set_size_buckets: dict[str, int]


class SharedToolSet(BaseModel):
    """A set of tools that several distinct users used in one episode."""

    tools: list[str]
    users: int
    episodes: int


class RepeatStats(BaseModel):
    """Whether goals, measured as tool sets, repeat."""

    episodes_with_tool_set: int
    personal_repeat_episodes: int = Field(
        description="Episodes whose tool set overlaps an earlier episode by the same caller"
    )
    personal_repeat_share: float
    callers_with_a_repeat: int
    shared_tool_sets: int = Field(description="Exact tool sets used by at least shared_min_users")
    episodes_in_shared_sets: int
    episodes_in_shared_sets_share: float
    top_shared_sets: list[SharedToolSet]


class DeploymentReport(BaseModel):
    """Everything the Phase 0 measurement reports. Holds no usernames."""

    generated_at: datetime
    config: AnalysisConfig
    window_start: datetime | None
    window_end: datetime | None
    collisions: CollisionStats
    identity: IdentityStats
    searches: SearchStats
    episodes: EpisodeStats
    repeats: RepeatStats

    model_config = {
        "json_schema_extra": {
            "example": {
                "collisions": {"tool_count": 4200, "shared_name_count": 310},
                "repeats": {"personal_repeat_share": 0.41, "shared_tool_sets": 57},
            }
        }
    }


def _share(
    part: int,
    whole: int,
) -> float:
    """Return part / whole rounded to four places, or 0.0 when whole is zero."""
    if whole <= 0:
        return 0.0
    return round(part / whole, 4)


def _normalize_description(
    text: str | None,
) -> str:
    """Lower-case a description and collapse whitespace, for equality checks."""
    return _WHITESPACE.sub(" ", (text or "").strip().lower())


def _bucket_label(
    count: int,
) -> str | None:
    """Return the servers-per-name bucket label for a count of two or more."""
    for label, low, high in SERVERS_PER_NAME_BUCKETS:
        if low <= count <= high:
            return label
    return None


def _size_label(
    size: int,
) -> str:
    """Return a coarse label for the number of tools in an episode."""
    if size <= 3:
        return str(size)
    if size <= 6:
        return "4-6"
    return "7+"


def _jaccard(
    left: frozenset[str],
    right: frozenset[str],
) -> float:
    """Return the Jaccard overlap of two non-empty sets."""
    union = len(left | right)
    if union == 0:
        return 0.0
    return len(left & right) / union


def _as_utc(
    value: Any,
) -> datetime | None:
    """Return value as an aware UTC datetime, or None when it is not a datetime.

    Motor returns naive datetimes for BSON dates, which are UTC.
    """
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _collect_names(
    servers: Iterable[dict[str, Any]],
) -> tuple[int, int, dict[str, set[str]], dict[str, list[str]]]:
    """Index tool names to the servers that expose them and their descriptions.

    Returns:
        Server count, tool count, name -> server paths, name -> normalized
        descriptions (one per server).
    """
    servers_by_name: dict[str, set[str]] = defaultdict(set)
    descriptions_by_name: dict[str, list[str]] = defaultdict(list)
    server_count = 0
    tool_count = 0
    for server in servers:
        server_count += 1
        path = str(server.get("path") or server.get("_id") or f"server-{server_count}")
        for tool in server.get("tool_list") or []:
            name = (tool or {}).get("name")
            if not name or path in servers_by_name[name]:
                continue
            tool_count += 1
            servers_by_name[name].add(path)
            descriptions_by_name[name].append(_normalize_description(tool.get("description")))
    return server_count, tool_count, servers_by_name, descriptions_by_name


def summarize_tool_collisions(
    servers: Iterable[dict[str, Any]],
    top_n: int,
) -> CollisionStats:
    """Count tool names that more than one server exposes.

    Args:
        servers: Server documents with ``path`` (or ``_id``) and ``tool_list``.
        top_n: How many of the most-shared names to list.

    Returns:
        Collision counts across the registry.
    """
    server_count, tool_count, servers_by_name, descriptions_by_name = _collect_names(servers)
    shared = {name: paths for name, paths in servers_by_name.items() if len(paths) >= 2}
    buckets: Counter[str] = Counter()
    identical = 0
    rows: list[SharedName] = []
    for name, paths in shared.items():
        label = _bucket_label(len(paths))
        if label:
            buckets[label] += 1
        descriptions = descriptions_by_name[name]
        distinct = len(set(descriptions))
        if distinct < len(descriptions):
            identical += 1
        rows.append(SharedName(name=name, server_count=len(paths), distinct_descriptions=distinct))
    rows.sort(key=lambda row: (-row.server_count, row.name))
    shared_tool_count = sum(len(paths) for paths in shared.values())
    return CollisionStats(
        server_count=server_count,
        tool_count=tool_count,
        distinct_tool_names=len(servers_by_name),
        shared_name_count=len(shared),
        shared_tool_count=shared_tool_count,
        shared_tool_share=_share(shared_tool_count, tool_count),
        shared_names_with_identical_description=identical,
        max_servers_per_name=max((len(paths) for paths in shared.values()), default=0),
        servers_per_name_buckets={
            label: buckets.get(label, 0) for label, _, _ in SERVERS_PER_NAME_BUCKETS
        },
        top_shared_names=rows[:top_n],
    )


def tool_call_from_audit_event(
    doc: dict[str, Any],
) -> ToolCall | None:
    """Reduce an audit document to a ToolCall, or None if it is not one.

    Keeps only authorised ``tools/call`` records that name a tool. The audit
    record is written before nginx forwards the call, so "success" means the
    call was allowed, not that it worked.

    Args:
        doc: A document from the audit_events collection.

    Returns:
        The call, with ``caller`` set to None when the token named no user.
    """
    if doc.get("log_type") != MCP_ACCESS_LOG_TYPE:
        return None
    request = doc.get("mcp_request") or {}
    response = doc.get("mcp_response") or {}
    if request.get("method") != TOOLS_CALL_METHOD or response.get("status") != SUCCESS_STATUS:
        return None
    tool = request.get("tool_name")
    timestamp = _as_utc(doc.get("timestamp"))
    server = (doc.get("mcp_server") or {}).get("name")
    if not tool or not server or timestamp is None:
        return None
    username = str((doc.get("identity") or {}).get("username") or "").strip()
    caller = None if username.lower() in NO_USER_NAMES else username
    return ToolCall(caller=caller, timestamp=timestamp, server=str(server), tool=str(tool))


def split_episodes(
    calls: Iterable[ToolCall],
    gap: timedelta,
) -> list[Episode]:
    """Group each caller's calls into episodes separated by idle gaps.

    Calls with no caller are skipped: they cannot be attributed to a user.

    Args:
        calls: Tool calls in any order.
        gap: Idle time that ends an episode.

    Returns:
        Episodes, each with its calls in time order.
    """
    by_caller: dict[str, list[ToolCall]] = defaultdict(list)
    for call in calls:
        if call.caller is not None:
            by_caller[call.caller].append(call)
    episodes: list[Episode] = []
    for caller, caller_calls in by_caller.items():
        caller_calls.sort(key=lambda call: call.timestamp)
        current = Episode(caller=caller)
        for call in caller_calls:
            if current.calls and call.timestamp - current.calls[-1].timestamp > gap:
                episodes.append(current)
                current = Episode(caller=caller)
            current.calls.append(call)
        if current.calls:
            episodes.append(current)
    return episodes


def summarize_identity(
    calls: list[ToolCall],
) -> IdentityStats:
    """Count calls without a user identity and how concentrated callers are.

    Args:
        calls: All tool calls in the window.

    Returns:
        Identity coverage. No usernames appear in the result.
    """
    per_caller = Counter(call.caller for call in calls if call.caller is not None)
    without_user = sum(1 for call in calls if call.caller is None)
    top = [count for _, count in per_caller.most_common(5)]
    return IdentityStats(
        tool_calls=len(calls),
        calls_without_user=without_user,
        calls_without_user_share=_share(without_user, len(calls)),
        distinct_callers=len(per_caller),
        top_caller_share=_share(top[0] if top else 0, len(calls)),
        top5_caller_share=_share(sum(top), len(calls)),
    )


def _is_search(
    call: ToolCall,
    config: AnalysisConfig,
) -> bool:
    return call.server == config.gateway_server and call.tool == config.search_tool


def _is_skill_load(
    call: ToolCall,
    config: AnalysisConfig,
) -> bool:
    return call.server == config.gateway_server and call.tool == config.skill_tool


def _is_backend(
    call: ToolCall,
    config: AnalysisConfig,
) -> bool:
    return call.server != config.gateway_server


def _search_followups(
    calls: list[ToolCall],
    start: int,
    config: AnalysisConfig,
) -> tuple[bool, bool, bool]:
    """Look at the calls after the search at ``start`` until the next search.

    Returns:
        (a backend call followed, a skill load followed, a repeat search came
        within the repeat window with neither in between)
    """
    backend = skill = False
    window = timedelta(minutes=config.repeat_search_minutes)
    for call in calls[start + 1 :]:
        if _is_search(call, config):
            repeated = (
                not backend and not skill and call.timestamp - calls[start].timestamp <= window
            )
            return backend, skill, repeated
        backend = backend or _is_backend(call, config)
        skill = skill or _is_skill_load(call, config)
    return backend, skill, False


def summarize_searches(
    episodes: list[Episode],
    config: AnalysisConfig,
) -> SearchStats:
    """Count what callers do after each search_registry call.

    Args:
        episodes: Episodes from :func:`split_episodes`.
        config: Run settings naming the gateway server and its tools.

    Returns:
        Follow-up counts for searches and the share of backend calls that came
        after a search.
    """
    searches = followed = skill_loads = repeated = 0
    backend_calls = after_search = 0
    for episode in episodes:
        seen_search = False
        for index, call in enumerate(episode.calls):
            if _is_search(call, config):
                seen_search = True
                searches += 1
                has_backend, has_skill, is_repeat = _search_followups(episode.calls, index, config)
                followed += has_backend
                skill_loads += has_skill
                repeated += is_repeat
            elif _is_backend(call, config):
                backend_calls += 1
                after_search += seen_search
    return SearchStats(
        searches=searches,
        followed_by_backend_call=followed,
        followed_by_backend_call_share=_share(followed, searches),
        followed_by_skill_load=skill_loads,
        repeated_without_call=repeated,
        repeated_without_call_share=_share(repeated, searches),
        backend_calls=backend_calls,
        backend_calls_after_search=after_search,
        backend_calls_after_search_share=_share(after_search, backend_calls),
    )


def episode_tool_set(
    episode: Episode,
    config: AnalysisConfig,
) -> frozenset[str]:
    """Return the backend tools an episode called, as ``server/tool`` keys."""
    return frozenset(
        f"{call.server}/{call.tool}" for call in episode.calls if _is_backend(call, config)
    )


def summarize_episodes(
    episodes: list[Episode],
    config: AnalysisConfig,
) -> EpisodeStats:
    """Describe episode sizes and how many span more than one backend server.

    Args:
        episodes: Episodes from :func:`split_episodes`.
        config: Run settings naming the gateway server.

    Returns:
        Episode counts and the multi-server share.
    """
    backend_counts: list[int] = []
    multi = 0
    sizes: Counter[str] = Counter()
    for episode in episodes:
        backend = [call for call in episode.calls if _is_backend(call, config)]
        if not backend:
            continue
        backend_counts.append(len(backend))
        multi += len({call.server for call in backend}) >= 2
        sizes[_size_label(len(episode_tool_set(episode, config)))] += 1
    return EpisodeStats(
        episodes=len(episodes),
        episodes_with_backend_calls=len(backend_counts),
        multi_server_episodes=multi,
        multi_server_share=_share(multi, len(backend_counts)),
        median_backend_calls=float(statistics.median(backend_counts)) if backend_counts else 0.0,
        tool_set_size_buckets=dict(sorted(sizes.items())),
    )


def _personal_repeats(
    episodes: list[Episode],
    config: AnalysisConfig,
) -> tuple[int, int]:
    """Count episodes whose tool set overlaps an earlier one by the same caller.

    Uses an index from tool to earlier sets, so a caller with thousands of
    episodes is compared only against sets that share a tool.

    Returns:
        (repeat episodes, callers with at least one repeat)
    """
    by_caller: dict[str, list[frozenset[str]]] = defaultdict(list)
    for episode in sorted(episodes, key=lambda item: item.calls[0].timestamp):
        tools = episode_tool_set(episode, config)
        if tools:
            by_caller[episode.caller].append(tools)
    repeats = 0
    callers = 0
    for sets in by_caller.values():
        seen: list[frozenset[str]] = []
        index: dict[str, list[int]] = defaultdict(list)
        caller_repeats = 0
        for tools in sets:
            candidates = {position for tool in tools for position in index[tool]}
            if any(
                _jaccard(tools, seen[position]) >= config.overlap_threshold
                for position in candidates
            ):
                caller_repeats += 1
            for tool in tools:
                index[tool].append(len(seen))
            seen.append(tools)
        repeats += caller_repeats
        callers += caller_repeats > 0
    return repeats, callers


def summarize_repeats(
    episodes: list[Episode],
    config: AnalysisConfig,
) -> RepeatStats:
    """Measure whether callers, and groups of callers, reuse the same tools.

    Args:
        episodes: Episodes from :func:`split_episodes`.
        config: Run settings with the overlap threshold and user floor.

    Returns:
        Personal repeat rate and the tool sets several users share. Shared sets
        use exact equality, so they are a lower bound.
    """
    users_by_set: dict[frozenset[str], set[str]] = defaultdict(set)
    episodes_by_set: Counter[frozenset[str]] = Counter()
    with_set = 0
    for episode in episodes:
        tools = episode_tool_set(episode, config)
        if not tools:
            continue
        with_set += 1
        users_by_set[tools].add(episode.caller)
        episodes_by_set[tools] += 1
    shared = [
        tools for tools, users in users_by_set.items() if len(users) >= config.shared_min_users
    ]
    in_shared = sum(episodes_by_set[tools] for tools in shared)
    rows = [
        SharedToolSet(
            tools=sorted(tools), users=len(users_by_set[tools]), episodes=episodes_by_set[tools]
        )
        for tools in shared
    ]
    rows.sort(key=lambda row: (-row.users, -row.episodes, row.tools))
    repeats, callers = _personal_repeats(episodes, config)
    return RepeatStats(
        episodes_with_tool_set=with_set,
        personal_repeat_episodes=repeats,
        personal_repeat_share=_share(repeats, with_set),
        callers_with_a_repeat=callers,
        shared_tool_sets=len(shared),
        episodes_in_shared_sets=in_shared,
        episodes_in_shared_sets_share=_share(in_shared, with_set),
        top_shared_sets=rows[: config.top_n],
    )


def build_report(
    servers: Iterable[dict[str, Any]],
    calls: list[ToolCall],
    config: AnalysisConfig,
) -> DeploymentReport:
    """Build the full Phase 0 deployment report.

    Args:
        servers: Server documents with ``path`` and ``tool_list``.
        calls: Tool calls from the audit window.
        config: Run settings.

    Returns:
        The report. It contains counts and tool names, never usernames.
    """
    episodes = split_episodes(calls, timedelta(minutes=config.episode_gap_minutes))
    logger.info(f"Split {len(calls)} tool calls into {len(episodes)} episodes")
    timestamps = [call.timestamp for call in calls]
    return DeploymentReport(
        generated_at=datetime.now(UTC),
        config=config,
        window_start=min(timestamps, default=None),
        window_end=max(timestamps, default=None),
        collisions=summarize_tool_collisions(servers, config.top_n),
        identity=summarize_identity(calls),
        searches=summarize_searches(episodes, config),
        episodes=summarize_episodes(episodes, config),
        repeats=summarize_repeats(episodes, config),
    )
