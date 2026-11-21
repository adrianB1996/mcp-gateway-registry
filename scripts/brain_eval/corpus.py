"""The frozen synthetic corpus for the brain eval.

Hand-written domain files (``tests/fixtures/brain_eval/domains/*.yaml``) define
servers, tools, skills, goals and lookup queries. This module turns them into a
corpus with ground truth, adding what is better generated than written:

- three generic tools (``get_item``, ``list_items``, ``search``) with identical
  descriptions on every domain server, which is what makes tool names collide;
- one task per phrasing of each goal, split into a tuning half and a hidden
  half, with some goals kept entirely in the hidden half;
- tool-name lookups and collision cases derived from the servers;
- filler servers that pad the corpus to a target tool count.

Everything is deterministic for a given seed. No I/O happens here.
"""

import random
import re
from collections import Counter
from collections.abc import Iterable

from pydantic import BaseModel, Field, field_validator

CORPUS_VERSION: str = "1"
DEFAULT_SEED: int = 20261121

# Grade for an expected result in a lookup. Graded relevance follows
# scripts/evaluate_search.py: 3 is the answer, 1 is acceptable.
GRADE_ANSWER: int = 3
GRADE_ACCEPTABLE: int = 1

# Lookup categories. Hand-written ones come from the domain files; the
# generated ones are added by the builder.
HAND_WRITTEN_CATEGORIES: frozenset[str] = frozenset({"tool-capability", "server-purpose", "skill"})
TOOL_NAME_CATEGORY: str = "tool-name"
COLLISION_CATEGORY: str = "collision"

# Every third goal is kept out of the tuning half, so the hidden half holds
# goals the brain has never seen as well as new phrasings of goals it has.
UNSEEN_GOAL_EVERY: int = 3
TUNING_PHRASINGS_PER_SEEN_GOAL: int = 2

GENERIC_TOOLS: tuple[tuple[str, str, dict[str, str]], ...] = (
    ("get_item", "Get an item by its ID.", {"id": "string"}),
    ("list_items", "List items, newest first.", {"limit": "integer"}),
    ("search", "Search records by keyword.", {"query": "string"}),
)

_SLUG = re.compile(r"^[a-z][a-z0-9-]*$")
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_JSON_TYPES: frozenset[str] = frozenset(
    {"string", "integer", "number", "boolean", "array", "object"}
)


class CorpusError(ValueError):
    """Raised when the domain files do not form a valid corpus."""


class ToolSpec(BaseModel):
    """A hand-written tool."""

    name: str
    description: str = Field(min_length=10)
    params: dict[str, str] = Field(default_factory=dict)
    read_only: bool = True

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not _TOOL_NAME.match(value):
            raise ValueError(f"tool name '{value}' must be lower snake case")
        return value

    @field_validator("params")
    @classmethod
    def _check_params(cls, value: dict[str, str]) -> dict[str, str]:
        bad = {name: kind for name, kind in value.items() if kind not in _JSON_TYPES}
        if bad:
            raise ValueError(f"unknown parameter types {bad}; use one of {sorted(_JSON_TYPES)}")
        return value


class ServerSpec(BaseModel):
    """A hand-written MCP server."""

    name: str
    title: str
    description: str = Field(min_length=20)
    tags: list[str] = Field(default_factory=list)
    item_noun: str = Field(
        description="What get_item returns on this server, for collision queries"
    )
    group: str | None = Field(default=None, description="User group that uses this backend")
    generic_tools: bool = Field(default=True, description="Add get_item, list_items and search")
    tools: list[ToolSpec] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not _SLUG.match(value):
            raise ValueError(f"server name '{value}' must be a lower-case slug")
        return value


class SkillSpec(BaseModel):
    """A hand-written skill. ``tools`` is ground truth only; the registry never sees it."""

    name: str
    description: str = Field(min_length=20)
    body: str = Field(min_length=40)
    tools: list[str] = Field(default_factory=list)


class GoalSpec(BaseModel):
    """A goal and the ways a user might phrase it."""

    id: str
    summary: str
    tools: list[str] = Field(default_factory=list)
    tools_by_group: dict[str, list[str]] = Field(default_factory=dict)
    skills: list[str] = Field(default_factory=list)
    phrasings: list[str] = Field(min_length=3)


class LookupSpec(BaseModel):
    """A hand-written lookup query. Each expected entry is ``id`` or ``id:grade``."""

    query: str
    category: str
    expect: list[str] = Field(min_length=1)

    @field_validator("category")
    @classmethod
    def _check_category(cls, value: str) -> str:
        if value not in HAND_WRITTEN_CATEGORIES:
            raise ValueError(f"category '{value}' must be one of {sorted(HAND_WRITTEN_CATEGORIES)}")
        return value


class DomainFile(BaseModel):
    """One hand-written domain file."""

    domain: str
    servers: list[ServerSpec] = Field(default_factory=list)
    skills: list[SkillSpec] = Field(default_factory=list)
    goals: list[GoalSpec] = Field(default_factory=list)
    lookups: list[LookupSpec] = Field(default_factory=list)


class Tool(BaseModel):
    """A tool as the registry will see it."""

    id: str
    name: str
    description: str
    input_schema: dict
    read_only: bool
    generic: bool = False


class Server(BaseModel):
    """A server as the registry will see it, plus eval-only fields."""

    id: str
    name: str
    title: str
    description: str
    tags: list[str]
    domain: str
    item_noun: str
    group: str | None = None
    padding: bool = False
    tools: list[Tool]


class Skill(BaseModel):
    """A skill as the registry will see it. ``tools`` is ground truth only."""

    id: str
    name: str
    description: str
    body: str
    domain: str
    tools: list[str]


class Task(BaseModel):
    """One phrasing of a goal, with the tools a correct run calls."""

    id: str
    goal_id: str
    domain: str
    text: str
    expected_tools: list[str]
    expected_skills: list[str]
    group: str | None
    split: str = Field(pattern="^(tuning|hidden)$")
    goal_seen_in_tuning: bool


class Lookup(BaseModel):
    """A single-answer retrieval query with graded ground truth."""

    id: str
    category: str
    query: str
    entity_type: str = Field(pattern="^(tool|server|skill)$")
    expected: dict[str, int]


class CollisionCase(BaseModel):
    """A query for a generic tool where only server context names the answer."""

    id: str
    query: str
    tool_name: str
    target: str
    distractors: list[str] = Field(description="Every other server's copy of the tool")


class Corpus(BaseModel):
    """The full corpus with ground truth."""

    version: str
    seed: int
    servers: list[Server]
    skills: list[Skill]
    tasks: list[Task]
    lookups: list[Lookup]
    collisions: list[CollisionCase]

    def tool_count(self) -> int:
        """Return the number of tools across all servers."""
        return sum(len(server.tools) for server in self.servers)

    def stats(self) -> dict[str, object]:
        """Return counts a reader can check at a glance."""
        return {
            "servers": len(self.servers),
            "padding_servers": sum(server.padding for server in self.servers),
            "tools": self.tool_count(),
            "skills": len(self.skills),
            "tasks": dict(Counter(task.split for task in self.tasks)),
            "goals": len({task.goal_id for task in self.tasks}),
            "lookups": dict(Counter(lookup.category for lookup in self.lookups)),
            "collision_cases": len(self.collisions),
        }


def tool_id(
    server: str,
    tool: str,
) -> str:
    """Return the id a search result maps to: ``server/tool``, no leading slash."""
    return f"{server.strip('/')}/{tool}"


def _input_schema(
    params: dict[str, str],
) -> dict:
    """Expand ``{name: type}`` into a JSON schema with every parameter required."""
    return {
        "type": "object",
        "properties": {name: {"type": kind} for name, kind in params.items()},
        "required": sorted(params),
        "additionalProperties": False,
    }


def _build_server(
    spec: ServerSpec,
    domain: str,
) -> Server:
    """Turn a server spec into a Server, adding the generic tools when asked."""
    tools = [
        Tool(
            id=tool_id(spec.name, tool.name),
            name=tool.name,
            description=tool.description,
            input_schema=_input_schema(tool.params),
            read_only=tool.read_only,
        )
        for tool in spec.tools
    ]
    if spec.generic_tools:
        tools += [
            Tool(
                id=tool_id(spec.name, name),
                name=name,
                description=description,
                input_schema=_input_schema(params),
                read_only=True,
                generic=True,
            )
            for name, description, params in GENERIC_TOOLS
        ]
    return Server(
        id=f"/{spec.name}",
        name=spec.name,
        title=spec.title,
        description=spec.description,
        tags=spec.tags,
        domain=domain,
        item_noun=spec.item_noun,
        group=spec.group,
        tools=tools,
    )


def _goal_tasks(
    goal: GoalSpec,
    domain: str,
    ordinal: int,
) -> list[Task]:
    """Expand a goal into one task per phrasing (and per group, for group goals)."""
    seen = ordinal % UNSEEN_GOAL_EVERY != 0
    variants = goal.tools_by_group.items() if goal.tools_by_group else [(None, goal.tools)]
    tasks: list[Task] = []
    for index, text in enumerate(goal.phrasings):
        split = "tuning" if seen and index < TUNING_PHRASINGS_PER_SEEN_GOAL else "hidden"
        for group, tools in variants:
            suffix = f"-{group}" if group else ""
            tasks.append(
                Task(
                    id=f"{goal.id}-{index + 1}{suffix}",
                    goal_id=goal.id,
                    domain=domain,
                    text=text,
                    expected_tools=list(tools),
                    expected_skills=list(goal.skills),
                    group=group,
                    split=split,
                    goal_seen_in_tuning=seen,
                )
            )
    return tasks


def _parse_expected(
    entries: list[str],
) -> dict[str, int]:
    """Parse ``id`` or ``id:grade`` entries; the first bare id is the answer."""
    expected: dict[str, int] = {}
    for entry in entries:
        item, separator, grade = entry.rpartition(":")
        if separator and grade.isdigit():
            expected[item] = int(grade)
        else:
            expected[entry] = GRADE_ANSWER
    return expected


def _entity_type(
    item: str,
) -> str:
    """Return the entity type of a ground-truth id."""
    if item.startswith("/skills/"):
        return "skill"
    if item.startswith("/"):
        return "server"
    return "tool"


def _hand_written_lookups(
    domains: list[DomainFile],
) -> list[Lookup]:
    """Turn every domain's lookup specs into Lookups with stable ids."""
    lookups: list[Lookup] = []
    for domain in domains:
        for index, spec in enumerate(domain.lookups, start=1):
            expected = _parse_expected(spec.expect)
            lookups.append(
                Lookup(
                    id=f"{domain.domain}-{spec.category}-{index}",
                    category=spec.category,
                    query=spec.query,
                    entity_type=_entity_type(next(iter(expected))),
                    expected=expected,
                )
            )
    return lookups


def _tool_name_lookups(
    servers: list[Server],
) -> list[Lookup]:
    """Add one lookup per non-generic tool whose name no other server uses."""
    counts = Counter(tool.name for server in servers for tool in server.tools)
    return [
        Lookup(
            id=f"name-{tool.id.replace('/', '-')}",
            category=TOOL_NAME_CATEGORY,
            query=tool.name,
            entity_type="tool",
            expected={tool.id: GRADE_ANSWER},
        )
        for server in servers
        for tool in server.tools
        if not tool.generic and counts[tool.name] == 1
    ]


def _collision_cases(
    servers: list[Server],
) -> list[CollisionCase]:
    """Add a get_item and a search query per domain server, named only by its noun."""
    holders: dict[str, list[str]] = {}
    for server in servers:
        for tool in server.tools:
            if tool.generic:
                holders.setdefault(tool.name, []).append(tool.id)
    cases: list[CollisionCase] = []
    for server in servers:
        if server.padding or not any(tool.generic for tool in server.tools):
            continue
        for name, query in (
            ("get_item", f"Get the {server.item_noun} with ID 4471"),
            ("search", f"Search {server.item_noun} records for the keyword acme"),
        ):
            target = tool_id(server.name, name)
            cases.append(
                CollisionCase(
                    id=f"collision-{server.name}-{name}",
                    query=query,
                    tool_name=name,
                    target=target,
                    distractors=[item for item in holders.get(name, []) if item != target],
                )
            )
    return cases


def _known_ids(
    servers: list[Server],
    skills: list[Skill],
) -> set[str]:
    return (
        {tool.id for server in servers for tool in server.tools}
        | {server.id for server in servers}
        | {skill.id for skill in skills}
    )


def validate_corpus(
    corpus: Corpus,
) -> list[str]:
    """Return every problem with the corpus; an empty list means it is valid.

    Checks that ids are unique, that every ground-truth id exists, that every
    task expects at least one tool, and that both splits are populated.
    """
    problems: list[str] = []
    ids = [tool.id for server in corpus.servers for tool in server.tools]
    ids += [server.id for server in corpus.servers] + [skill.id for skill in corpus.skills]
    problems += [f"duplicate id {item}" for item, count in Counter(ids).items() if count > 1]
    known = set(ids)
    for task in corpus.tasks:
        if not task.expected_tools:
            problems.append(f"task {task.id} expects no tools")
        problems += [
            f"task {task.id} expects unknown {item}"
            for item in task.expected_tools + task.expected_skills
            if item not in known
        ]
    for lookup in corpus.lookups:
        problems += [
            f"lookup {lookup.id} expects unknown {item}"
            for item in lookup.expected
            if item not in known
        ]
    for skill in corpus.skills:
        problems += [
            f"skill {skill.id} uses unknown {item}" for item in skill.tools if item not in known
        ]
    splits = Counter(task.split for task in corpus.tasks)
    if corpus.tasks and (not splits["tuning"] or not splits["hidden"]):
        problems.append(f"both splits need tasks, got {dict(splits)}")
    return problems


def _normalize_ids(
    domains: list[DomainFile],
    known: set[str],
) -> None:
    """Accept ``skill-name`` for skills in goals and lookups and expand it to ``/skills/name``."""
    skill_names = {item.removeprefix("/skills/") for item in known if item.startswith("/skills/")}
    for domain in domains:
        for goal in domain.goals:
            goal.skills = [
                f"/skills/{name}" if name in skill_names else name for name in goal.skills
            ]
        for lookup in domain.lookups:
            lookup.expect = [
                f"/skills/{entry}" if entry.split(":")[0] in skill_names else entry
                for entry in lookup.expect
            ]


def build_core(
    domains: Iterable[DomainFile],
    seed: int = DEFAULT_SEED,
) -> Corpus:
    """Build the hand-written corpus with its generated tools, tasks and lookups.

    Args:
        domains: Parsed domain files.
        seed: Recorded in the corpus; padding uses it.

    Returns:
        The corpus without padding.

    Raises:
        CorpusError: A reference points at nothing, an id repeats, or a split
            is empty. The message lists every problem.
    """
    domains = list(domains)
    servers = [_build_server(spec, domain.domain) for domain in domains for spec in domain.servers]
    skills = [
        Skill(
            id=f"/skills/{spec.name}",
            name=spec.name,
            description=spec.description,
            body=spec.body,
            domain=domain.domain,
            tools=spec.tools,
        )
        for domain in domains
        for spec in domain.skills
    ]
    _normalize_ids(domains, _known_ids(servers, skills))
    goals = [(goal, domain.domain) for domain in domains for goal in domain.goals]
    tasks = [
        task
        for ordinal, (goal, domain) in enumerate(goals)
        for task in _goal_tasks(goal, domain, ordinal)
    ]
    corpus = Corpus(
        version=CORPUS_VERSION,
        seed=seed,
        servers=servers,
        skills=skills,
        tasks=tasks,
        lookups=_hand_written_lookups(domains) + _tool_name_lookups(servers),
        collisions=[],
    )
    corpus.collisions = _collision_cases(corpus.servers)
    problems = validate_corpus(corpus)
    if problems:
        raise CorpusError(
            "Corpus is invalid:\n" + "\n".join(f"- {problem}" for problem in problems)
        )
    return corpus


# Word lists for filler servers. Their text is plausible enough to compete in
# search, and no task or lookup ever expects a filler tool.
_PAD_AREAS: tuple[str, ...] = (
    "fleet",
    "facilities",
    "procurement",
    "travel",
    "payroll",
    "training",
    "telemetry",
    "survey",
    "badge",
    "parking",
    "catering",
    "warranty",
    "grants",
    "research",
    "lab",
    "printing",
    "mailroom",
    "events",
    "donations",
    "membership",
    "loyalty",
    "booking",
    "energy",
    "waste",
    "safety",
    "quality",
    "audit",
    "translation",
    "media",
    "archive",
)
_PAD_KINDS: tuple[str, ...] = (
    "tracker",
    "manager",
    "portal",
    "hub",
    "desk",
    "registry",
    "planner",
    "console",
    "service",
    "center",
)
_PAD_NOUNS: tuple[str, ...] = (
    "record",
    "request",
    "booking",
    "order",
    "report",
    "schedule",
    "asset",
    "vendor",
    "contact",
    "note",
    "entry",
    "batch",
    "claim",
    "permit",
    "visit",
    "reading",
)
_PAD_VERBS: tuple[tuple[str, str], ...] = (
    ("get", "Fetch one {noun} from the {area} {kind} by its identifier."),
    ("create", "Create a new {noun} in the {area} {kind}."),
    ("update", "Update fields on an existing {area} {noun}."),
    ("cancel", "Cancel a pending {area} {noun}."),
    ("list", "List {area} {noun}s matching a status filter."),
    ("export", "Export {area} {noun}s to a CSV file."),
    ("approve", "Approve a {area} {noun} that is waiting for sign-off."),
    ("summarize", "Summarize {area} {noun}s for a date range."),
)
PAD_TOOLS_PER_SERVER: int = 8
# Share of filler servers that also expose the generic tools, so collisions
# reach the 50-server level of the ladder.
PAD_GENERIC_SHARE: float = 0.3


def _padding_server(
    index: int,
    rng: random.Random,
) -> Server:
    """Build one filler server from the word lists."""
    area = _PAD_AREAS[index % len(_PAD_AREAS)]
    kind = _PAD_KINDS[(index // len(_PAD_AREAS)) % len(_PAD_KINDS)]
    name = f"pad-{index:05d}-{area}-{kind}"
    generic = rng.random() < PAD_GENERIC_SHARE  # nosec B311 - corpus generation, not security
    own = PAD_TOOLS_PER_SERVER - (len(GENERIC_TOOLS) if generic else 0)
    tools: list[Tool] = []
    for verb, template in rng.sample(_PAD_VERBS, own):  # nosec B311
        noun = rng.choice(_PAD_NOUNS)  # nosec B311
        tool_name = f"{verb}_{noun}"
        if any(tool.name == tool_name for tool in tools):
            tool_name = f"{verb}_{area}_{noun}"
        tools.append(
            Tool(
                id=tool_id(name, tool_name),
                name=tool_name,
                description=template.format(noun=noun, area=area, kind=kind),
                input_schema=_input_schema(
                    {"id": "string"}
                    if verb in {"get", "cancel", "approve"}
                    else {"filter": "string"}
                ),
                read_only=verb in {"get", "list", "export", "summarize"},
            )
        )
    if generic:
        tools += [
            Tool(
                id=tool_id(name, gname),
                name=gname,
                description=gdesc,
                input_schema=_input_schema(params),
                read_only=True,
                generic=True,
            )
            for gname, gdesc, params in GENERIC_TOOLS
        ]
    return Server(
        id=f"/{name}",
        name=name,
        title=f"{area.title()} {kind.title()}",
        description=f"Internal {area} {kind} for managing {area} {rng.choice(_PAD_NOUNS)}s.",  # nosec B311
        tags=[area, kind],
        domain="padding",
        item_noun=f"{area} record",
        padding=True,
        tools=tools,
    )


def pad_corpus(
    core: Corpus,
    target_tools: int,
) -> Corpus:
    """Return a copy of the corpus padded with filler servers to ``target_tools``.

    The same core and target always give the same corpus. Collision cases are
    rebuilt so their distractor lists include filler servers' generic tools.

    Raises:
        CorpusError: The target is smaller than the core corpus.
    """
    if target_tools < core.tool_count():
        raise CorpusError(
            f"target {target_tools} tools is below the core corpus's {core.tool_count()}"
        )
    rng = random.Random(f"{core.seed}-{target_tools}")  # nosec B311 - deterministic corpus generation
    servers = list(core.servers)
    total = core.tool_count()
    index = 0
    while total < target_tools:
        server = _padding_server(index, rng)
        servers.append(server)
        total += len(server.tools)
        index += 1
    padded = core.model_copy(update={"servers": servers})
    padded.collisions = _collision_cases(servers)
    return padded
