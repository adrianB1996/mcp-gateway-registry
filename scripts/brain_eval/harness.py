"""Retrieval harness: load the corpus into a registry and score its search.

The harness measures whatever registry it is pointed at, so the same code
scores arm A (today's search) and arm B (improved search from Phase 1). It
registers the corpus through the registry's own API, runs every lookup and
collision query through ``POST /api/search/semantic``, and scores the ranked
results against the corpus ground truth.

The functions that talk to the registry take a client with the methods of
``api.registry_client.RegistryClient`` (``register_service``, ``get_server``,
``toggle_service``, ``register_skill``, ``semantic_search``), so tests can pass a
fake.
"""

import json
import logging
import random
import statistics
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field

from brain_eval.corpus import CollisionCase, Corpus, Lookup
from brain_eval.corpus import Server as CorpusServer
from brain_eval.corpus import Skill as CorpusSkill
from brain_eval.metrics import RankingScores, score_ranking

logger = logging.getLogger(__name__)

DEFAULT_LADDER: tuple[int, ...] = (1, 5, 10, 25, 50)
DEFAULT_MAX_RESULTS: int = 10
# The search API caps max_results at 50, which bounds the collision ladder.
COLLISION_MAX_RESULTS: int = 50
LIFECYCLE_ACTIVE: str = "active"
# Search request scopes per lookup type. The registry keeps tools inside their
# server's document and has no standalone tool documents, so asking for "tool"
# alone matches nothing. Tool lookups ask for servers and tools together, the
# same scope agents/agent.py uses, and are scored on the tools list only.
ENTITY_TYPES: dict[str, list[str]] = {
    "tool": ["mcp_server", "tool"],
    "server": ["mcp_server"],
    "skill": ["skill"],
}


class RegistryClientLike(Protocol):
    """The RegistryClient methods the harness uses."""

    def register_service(self, registration: Any) -> Any: ...

    def get_server(self, service_path: str) -> Any: ...

    def toggle_service(self, service_path: str) -> Any: ...

    def register_skill(self, request: Any) -> Any: ...

    def semantic_search(
        self, query: str, entity_types: list[str] | None = None, max_results: int = 10
    ) -> Any: ...


class HarnessError(RuntimeError):
    """Raised when the corpus cannot be loaded or searched."""


class ItemResult(BaseModel):
    """One lookup's ranked results and scores, kept so arms can be paired later."""

    id: str
    category: str
    query: str
    expected: dict[str, int]
    ranked: list[str]
    scores: RankingScores


class CategorySummary(BaseModel):
    """Mean scores over one lookup category."""

    queries: int
    ndcg: float
    recall: float
    mrr: float
    hit_at_1: float


class LadderStep(BaseModel):
    """Top-1 accuracy among copies of a generic tool at one ladder level."""

    level: int = Field(description="Servers exposing the tool, including the right one")
    cases: int
    hit_at_1: float


class RetrievalReport(BaseModel):
    """Everything one retrieval run measured. Holds no token or credential."""

    arm: str
    generated_at: datetime
    registry_url: str
    corpus_sha256: str | None
    corpus_tools: int
    skills_loaded: bool
    categories: dict[str, CategorySummary]
    ladder: list[LadderStep]
    items: list[ItemResult]
    collision_ranks: dict[str, list[str]] = Field(description="Ranked copies per collision case")


def tool_list_entries(
    server: CorpusServer,
) -> list[dict[str, Any]]:
    """Return the server's tools in the shape the registry stores after a tools/list."""
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "parsed_description": {"main": tool.description},
            "schema": tool.input_schema,
            "inputSchema": tool.input_schema,
        }
        for tool in server.tools
    ]


def registration_for(
    server: CorpusServer,
    fake_base_url: str,
    registration_cls: type,
) -> Any:
    """Build a registration for one server, routed to the fake MCP servers.

    The gateway maps ``/<name>/mcp`` to ``proxy_pass_url`` + ``mcp``, so the
    fake server must answer at ``<fake_base_url>/<name>/mcp``.
    """
    return registration_cls(
        path=f"/{server.name}",
        name=server.title,
        description=server.description,
        proxy_pass_url=f"{fake_base_url.rstrip('/')}/{server.name}/",
        tags=server.tags,
        tool_list_json=json.dumps(tool_list_entries(server)),
        status=LIFECYCLE_ACTIVE,
        overwrite=True,
    )


def load_servers(
    client: RegistryClientLike,
    servers: Iterable[CorpusServer],
    fake_base_url: str,
    registration_cls: type,
) -> int:
    """Register and enable every server. Re-running is safe.

    New servers come back disabled, and search hides disabled servers, so each
    one is switched on unless it already is.

    Returns:
        The number of servers registered.
    """
    count = 0
    for server in servers:
        path = f"/{server.name}"
        client.register_service(registration_for(server, fake_base_url, registration_cls))
        if not client.get_server(path).is_enabled:
            client.toggle_service(path)
        count += 1
        if count % 100 == 0:
            logger.info(f"Registered {count} servers")
    return count


def load_skills(
    client: RegistryClientLike,
    skills: Iterable[CorpusSkill],
    skill_base_url: str,
    request_cls: type,
    credential: str | None,
) -> int:
    """Register every skill as active, by the URL of its SKILL.md.

    Raises:
        HarnessError: A skill failed to register. The usual cause is a SKILL.md
            URL the registry cannot fetch.
    """
    count = 0
    for skill in skills:
        request = request_cls(
            name=skill.name,
            description=skill.description,
            skill_md_url=f"{skill_base_url.rstrip('/')}/{skill.name}/SKILL.md",
            status=LIFECYCLE_ACTIVE,
            auth_scheme="bearer" if credential else None,
            auth_credential=credential,
        )
        try:
            client.register_skill(request)
        except Exception as error:
            raise HarnessError(
                f"Registering skill '{skill.name}' failed: {error}. The registry fetches "
                f"{request.skill_md_url} and refuses private addresses. Point --skill-base-url "
                "at a public copy of tests/fixtures/brain_eval/skills, pass a token for a "
                "private repository, or use --skip-skills."
            ) from error
        count += 1
    return count


def tool_result_id(
    result: Any,
) -> str:
    """Map a tool search result to a corpus tool id."""
    return f"{result.server_path.strip('/')}/{result.tool_name}"


def result_ids(
    response: Any,
    entity_type: str,
) -> list[str]:
    """Return the ranked ids of one entity type from a semantic search response."""
    if entity_type == "tool":
        return [tool_result_id(result) for result in response.tools]
    if entity_type == "server":
        return [result.path for result in response.servers]
    if entity_type == "skill":
        return [result.path for result in response.skills]
    raise HarnessError(f"Unknown entity type '{entity_type}'")


def run_lookup(
    client: RegistryClientLike,
    lookup: Lookup,
    max_results: int,
) -> ItemResult:
    """Run one lookup through search and score it."""
    response = client.semantic_search(
        lookup.query, entity_types=ENTITY_TYPES[lookup.entity_type], max_results=max_results
    )
    ranked = result_ids(response, lookup.entity_type)
    return ItemResult(
        id=lookup.id,
        category=lookup.category,
        query=lookup.query,
        expected=lookup.expected,
        ranked=ranked,
        scores=score_ranking(ranked, lookup.expected, k=max_results),
    )


def ladder_hits(
    case: CollisionCase,
    ranked_copies: list[str],
    levels: tuple[int, ...],
    seed: int,
) -> dict[int, float]:
    """Score whether the right copy of a generic tool ranks first at each level.

    Each level keeps the right server plus ``level - 1`` other servers exposing
    the same tool, chosen in a fixed shuffled order so each level contains the
    one below it. The search ranked every copy once; a level keeps only its
    servers' copies, in the same order. Search scores each document on its own,
    so this matches a registry holding only those servers.

    Returns:
        Level to 1.0 or 0.0. Levels with too few distractors are left out.
    """
    order = list(case.distractors)
    # Fixed per-case order for reproducible levels, not security. nosec B311
    random.Random(f"{seed}-{case.id}").shuffle(order)  # nosec B311
    hits: dict[int, float] = {}
    for level in levels:
        if level - 1 > len(order):
            continue
        keep = {case.target, *order[: level - 1]}
        present = [item for item in ranked_copies if item in keep]
        hits[level] = 1.0 if present and present[0] == case.target else 0.0
    return hits


def run_collision(
    client: RegistryClientLike,
    case: CollisionCase,
) -> list[str]:
    """Run a collision query and return the ranked copies of its generic tool."""
    response = client.semantic_search(
        case.query, entity_types=ENTITY_TYPES["tool"], max_results=COLLISION_MAX_RESULTS
    )
    copies = {case.target, *case.distractors}
    return [item for item in result_ids(response, "tool") if item in copies]


def summarize_categories(
    items: list[ItemResult],
) -> dict[str, CategorySummary]:
    """Average each metric within each lookup category."""
    groups: dict[str, list[RankingScores]] = defaultdict(list)
    for item in items:
        groups[item.category].append(item.scores)
    return {
        category: CategorySummary(
            queries=len(scores),
            ndcg=round(statistics.fmean(s.ndcg for s in scores), 4),
            recall=round(statistics.fmean(s.recall for s in scores), 4),
            mrr=round(statistics.fmean(s.reciprocal_rank for s in scores), 4),
            hit_at_1=round(statistics.fmean(s.hit_at_1 for s in scores), 4),
        )
        for category, scores in sorted(groups.items())
    }


def summarize_ladder(
    hits: list[dict[int, float]],
    levels: tuple[int, ...],
) -> list[LadderStep]:
    """Average top-1 accuracy per level across collision cases."""
    steps: list[LadderStep] = []
    for level in levels:
        values = [case_hits[level] for case_hits in hits if level in case_hits]
        if values:
            steps.append(
                LadderStep(
                    level=level, cases=len(values), hit_at_1=round(statistics.fmean(values), 4)
                )
            )
    return steps


def run_retrieval(
    client: RegistryClientLike,
    corpus: Corpus,
    arm: str,
    registry_url: str,
    corpus_sha256: str | None,
    skills_loaded: bool,
    max_results: int = DEFAULT_MAX_RESULTS,
    levels: tuple[int, ...] = DEFAULT_LADDER,
) -> RetrievalReport:
    """Run every lookup and collision query and build the report.

    Skill lookups are skipped when skills were not loaded, so their absence is
    visible in the report instead of scoring as zero.

    Args:
        client: A registry client.
        corpus: The corpus that was loaded.
        arm: Label for the report, such as "A".
        registry_url: Recorded in the report.
        corpus_sha256: The corpus file's hash from manifest.json.
        skills_loaded: Whether skills were registered.
        max_results: Results per lookup query; also the metric cut-off.
        levels: Collision ladder levels.

    Returns:
        The report.
    """
    lookups = [
        lookup for lookup in corpus.lookups if skills_loaded or lookup.entity_type != "skill"
    ]
    logger.warning(f"Running {len(lookups)} lookups and {len(corpus.collisions)} collision queries")
    items = [run_lookup(client, lookup, max_results) for lookup in lookups]
    collision_ranks = {case.id: run_collision(client, case) for case in corpus.collisions}
    hits = [
        ladder_hits(case, collision_ranks[case.id], levels, corpus.seed)
        for case in corpus.collisions
    ]
    return RetrievalReport(
        arm=arm,
        generated_at=datetime.now(UTC),
        registry_url=registry_url,
        corpus_sha256=corpus_sha256,
        corpus_tools=corpus.tool_count(),
        skills_loaded=skills_loaded,
        categories=summarize_categories(items),
        ladder=summarize_ladder(hits, levels),
        items=items,
        collision_ranks=collision_ranks,
    )
