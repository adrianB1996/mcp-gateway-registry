"""Unit tests for scripts/brain_eval/corpus.py and the committed corpus."""

import json

import pytest
from brain_eval import build_corpus
from brain_eval.corpus import (
    CorpusError,
    DomainFile,
    build_core,
    pad_corpus,
)

pytestmark = pytest.mark.unit


def _server(name: str, *tools: str, generic: bool = True, group: str | None = None) -> dict:
    return {
        "name": name,
        "title": name.title(),
        "description": f"The {name} server for testing the corpus builder.",
        "item_noun": f"{name} record",
        "generic_tools": generic,
        "group": group,
        "tools": [{"name": tool, "description": f"Does {tool} for testing."} for tool in tools],
    }


def _goal(goal_id: str, tools: list[str], **extra) -> dict:
    return {
        "id": goal_id,
        "summary": goal_id,
        "tools": tools,
        "phrasings": [f"{goal_id} one", f"{goal_id} two", f"{goal_id} three"],
        **extra,
    }


def _domain(**fields) -> DomainFile:
    return DomainFile.model_validate({"domain": "test", **fields})


@pytest.fixture
def two_goal_domain() -> DomainFile:
    return _domain(
        servers=[_server("alpha", "do_thing"), _server("files", "write_xlsx", generic=False)],
        goals=[_goal("g-first", ["alpha/do_thing"]), _goal("g-second", ["files/write_xlsx"])],
    )


class TestBuildCore:
    def test_adds_generic_tools_to_domain_servers_only(self, two_goal_domain):
        corpus = build_core([two_goal_domain])

        names = {server.name: {tool.name for tool in server.tools} for server in corpus.servers}
        assert names["alpha"] == {"do_thing", "get_item", "list_items", "search"}
        assert names["files"] == {"write_xlsx"}

    def test_keeps_every_phrasing_of_an_unseen_goal_in_the_hidden_half(self, two_goal_domain):
        corpus = build_core([two_goal_domain])

        first = [task for task in corpus.tasks if task.goal_id == "g-first"]
        assert {task.split for task in first} == {"hidden"}
        assert not first[0].goal_seen_in_tuning

    def test_puts_two_phrasings_of_a_seen_goal_in_tuning(self, two_goal_domain):
        corpus = build_core([two_goal_domain])

        second = [task.split for task in corpus.tasks if task.goal_id == "g-second"]
        assert second == ["tuning", "tuning", "hidden"]

    def test_expands_a_group_goal_into_one_task_per_group(self):
        domain = _domain(
            servers=[
                _server("east", "refund", group="emea"),
                _server("west", "refund", group="amer"),
            ],
            goals=[
                _goal(
                    "g-refund",
                    [],
                    tools_by_group={"emea": ["east/refund"], "amer": ["west/refund"]},
                ),
                _goal("g-other", ["east/refund"]),
            ],
        )

        corpus = build_core([domain])

        first = {
            task.id: task.expected_tools for task in corpus.tasks if task.text == "g-refund one"
        }
        assert first == {"g-refund-1-emea": ["east/refund"], "g-refund-1-amer": ["west/refund"]}

    def test_rejects_a_goal_that_names_an_unknown_tool(self):
        domain = _domain(
            servers=[_server("alpha", "do_thing")],
            goals=[_goal("g", ["alpha/missing"]), _goal("h", ["alpha/do_thing"])],
        )

        with pytest.raises(CorpusError, match="alpha/missing"):
            build_core([domain])

    def test_rejects_two_servers_with_the_same_name(self):
        domain = _domain(
            servers=[_server("alpha", "a"), _server("alpha", "b")],
            goals=[_goal("g", ["alpha/a"]), _goal("h", ["alpha/b"])],
        )

        with pytest.raises(CorpusError, match="duplicate id"):
            build_core([domain])

    def test_expands_skill_names_and_grades_in_lookups(self):
        domain = _domain(
            servers=[_server("alpha", "do_thing")],
            skills=[
                {
                    "name": "how-to",
                    "description": "How to do the thing well.",
                    "body": "A body long enough to pass the length check.",
                }
            ],
            goals=[_goal("g", ["alpha/do_thing"]), _goal("h", ["alpha/do_thing"])],
            lookups=[
                {"query": "how", "category": "skill", "expect": ["how-to", "alpha/do_thing:1"]}
            ],
        )

        corpus = build_core([domain])

        skill_lookup = next(lookup for lookup in corpus.lookups if lookup.category == "skill")
        assert skill_lookup.expected == {"/skills/how-to": 3, "alpha/do_thing": 1}
        assert skill_lookup.entity_type == "skill"

    def test_skips_tool_name_lookups_for_names_two_servers_share(self):
        domain = _domain(
            servers=[_server("east", "refund", "only_east"), _server("west", "refund")],
            goals=[_goal("g", ["east/refund"]), _goal("h", ["west/refund"])],
        )

        corpus = build_core([domain])

        queries = {lookup.query for lookup in corpus.lookups if lookup.category == "tool-name"}
        assert queries == {"only_east"}

    def test_lists_every_other_copy_of_a_generic_tool_as_a_distractor(self, two_goal_domain):
        domain = two_goal_domain.model_copy(
            update={
                "servers": [
                    *two_goal_domain.servers,
                    *_domain(servers=[_server("beta", "x")]).servers,
                ]
            }
        )

        corpus = build_core([domain])

        case = next(case for case in corpus.collisions if case.id == "collision-alpha-get_item")
        assert case.distractors == ["beta/get_item"]


class TestPadCorpus:
    def test_reaches_the_target_and_marks_filler_servers(self, two_goal_domain):
        core = build_core([two_goal_domain])

        padded = pad_corpus(core, 200)

        assert padded.tool_count() >= 200
        assert all(server.padding for server in padded.servers[len(core.servers) :])

    def test_gives_the_same_corpus_every_time(self, two_goal_domain):
        core = build_core([two_goal_domain])

        assert pad_corpus(core, 300) == pad_corpus(core, 300)

    def test_rejects_a_target_below_the_core_size(self, two_goal_domain):
        core = build_core([two_goal_domain])

        with pytest.raises(CorpusError, match="below the core corpus"):
            pad_corpus(core, 1)


class TestCommittedCorpus:
    @pytest.fixture(scope="class")
    def core(self):
        return build_core(build_corpus.load_domains(build_corpus.DOMAINS_DIR))

    def test_committed_corpus_matches_the_domain_files(self, core):
        assert build_corpus.CORPUS_FILE.read_text(encoding="utf-8") == build_corpus.corpus_json(
            core
        ), "Run scripts/brain_eval/build_corpus.py and commit corpus.json"

    def test_committed_manifest_matches_the_padded_corpora(self, core):
        manifest = build_corpus.build_manifest(core, build_corpus.DEFAULT_SCALES, padded_dir=None)

        assert json.loads(build_corpus.MANIFEST_FILE.read_text(encoding="utf-8")) == manifest

    def test_no_task_names_the_tool_it_expects(self, core):
        offenders = [
            task.id
            for task in core.tasks
            for tool in task.expected_tools
            if tool.split("/", 1)[1] in task.text.lower()
        ]

        assert offenders == []

    def test_every_lookup_category_has_at_least_thirty_queries(self, core):
        counts = core.stats()["lookups"]

        assert min(counts.values()) >= 30

    def test_the_ladder_reaches_fifty_servers_at_one_thousand_tools(self, core):
        padded = pad_corpus(core, 1000)

        get_item = [case for case in padded.collisions if case.tool_name == "get_item"]
        assert min(len(case.distractors) for case in get_item) >= 49
