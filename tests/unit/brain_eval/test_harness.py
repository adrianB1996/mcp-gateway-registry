"""Unit tests for scripts/brain_eval/harness.py and run_retrieval.py."""

import json
from types import SimpleNamespace

import pytest
from brain_eval import build_corpus, run_retrieval
from brain_eval.corpus import CollisionCase
from brain_eval.fake_servers import load_corpus
from brain_eval.harness import (
    HarnessError,
    ladder_hits,
    load_servers,
    load_skills,
    registration_for,
    result_ids,
)
from brain_eval.harness import run_retrieval as run_harness

from api.registry_client import InternalServiceRegistration, SkillRegistrationRequest

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def corpus():
    return load_corpus(build_corpus.CORPUS_FILE)


def _tool(server: str, tool: str):
    return SimpleNamespace(server_path=f"/{server}", tool_name=tool)


def _response(tools=(), servers=(), skills=()):
    return SimpleNamespace(
        tools=list(tools),
        servers=[SimpleNamespace(path=path) for path in servers],
        skills=[SimpleNamespace(path=path) for path in skills],
    )


class FakeClient:
    """Records calls and answers search with a fixed function of the query."""

    def __init__(self, enabled=False, answer=None, fail_skills=False):
        self.registered = []
        self.toggled = []
        self.skills = []
        self.searches = []
        self._enabled = enabled
        self._answer = answer or (lambda query, types: _response())
        self._fail_skills = fail_skills

    def register_service(self, registration):
        self.registered.append(registration)

    def get_server(self, path):
        return SimpleNamespace(is_enabled=self._enabled)

    def toggle_service(self, path):
        self.toggled.append(path)

    def register_skill(self, request):
        if self._fail_skills:
            raise RuntimeError("SSRF validation failed")
        self.skills.append(request)

    def semantic_search(self, query, entity_types=None, max_results=10):
        self.searches.append((query, tuple(entity_types or ()), max_results))
        return self._answer(query, entity_types)


class TestLoading:
    def test_routes_each_server_to_its_fake_server_with_the_full_tool_list(self, corpus):
        server = corpus.servers[0]

        registration = registration_for(
            server, "http://host.docker.internal:9100/", InternalServiceRegistration
        )

        body = registration.model_dump(by_alias=True, exclude_none=True)
        assert body["path"] == f"/{server.name}"
        assert body["proxy_pass_url"] == f"http://host.docker.internal:9100/{server.name}/"
        assert body["status"] == "active"
        assert [tool["name"] for tool in json.loads(body["tool_list_json"])] == [
            t.name for t in server.tools
        ]

    def test_switches_on_servers_that_register_disabled(self, corpus):
        client = FakeClient(enabled=False)

        count = load_servers(
            client, corpus.servers[:3], "http://fake:9100", InternalServiceRegistration
        )

        assert count == 3
        assert client.toggled == [f"/{s.name}" for s in corpus.servers[:3]]

    def test_leaves_already_enabled_servers_alone_on_a_rerun(self, corpus):
        client = FakeClient(enabled=True)

        load_servers(client, corpus.servers[:3], "http://fake:9100", InternalServiceRegistration)

        assert client.toggled == []

    def test_registers_skills_as_active_with_a_bearer_token_when_given(self, corpus):
        client = FakeClient()

        load_skills(
            client,
            corpus.skills[:1],
            "https://example.com/skills/",
            SkillRegistrationRequest,
            "YOUR_TOKEN",
        )

        request = client.skills[0]
        assert (
            request.skill_md_url == f"https://example.com/skills/{corpus.skills[0].name}/SKILL.md"
        )
        assert (request.status, request.auth_scheme) == ("active", "bearer")

    def test_explains_how_to_fix_a_skill_the_registry_cannot_fetch(self, corpus):
        client = FakeClient(fail_skills=True)

        with pytest.raises(HarnessError, match="--skip-skills"):
            load_skills(
                client, corpus.skills[:1], "http://localhost/skills", SkillRegistrationRequest, None
            )


class TestResultIds:
    def test_maps_tool_results_to_corpus_ids(self):
        assert result_ids(_response(tools=[_tool("ledgerline", "lock_period")]), "tool") == [
            "ledgerline/lock_period"
        ]

    def test_maps_server_and_skill_results_to_their_paths(self):
        response = _response(servers=["/ledgerline"], skills=["/skills/month-end-close"])

        assert (result_ids(response, "server"), result_ids(response, "skill")) == (
            ["/ledgerline"],
            ["/skills/month-end-close"],
        )


class TestLadder:
    @pytest.fixture
    def case(self):
        distractors = [f"s{index}/get_item" for index in range(60)]
        return CollisionCase(
            id="c", query="q", tool_name="get_item", target="t/get_item", distractors=distractors
        )

    def test_scores_a_hit_when_the_target_outranks_every_kept_copy(self, case):
        hits = ladder_hits(case, ["t/get_item", *case.distractors], (1, 5, 50), seed=1)

        assert hits == {1: 1.0, 5: 1.0, 50: 1.0}

    def test_a_copy_ranked_above_the_target_counts_only_once_its_level_keeps_it(self, case):
        order = list(case.distractors)
        import random

        random.Random("1-c").shuffle(order)
        ranked = [order[7], "t/get_item"]

        hits = ladder_hits(case, ranked, (1, 5, 10), seed=1)

        assert hits == {1: 1.0, 5: 1.0, 10: 0.0}

    def test_skips_levels_with_too_few_distractors(self, case):
        short = case.model_copy(update={"distractors": case.distractors[:3]})

        assert set(ladder_hits(short, ["t/get_item"], (1, 5), seed=1)) == {1}

    def test_scores_a_miss_when_the_target_is_not_returned(self, case):
        assert ladder_hits(case, case.distractors, (1,), seed=1) == {1: 0.0}


class TestRunRetrieval:
    def test_scores_lookups_by_category_and_skips_skills_when_not_loaded(self, corpus):
        expected = {lookup.query: next(iter(lookup.expected)) for lookup in corpus.lookups}

        def answer(query, types):
            item = expected.get(query, "")
            if types == ["tool"]:
                server, _, tool = item.partition("/")
                return _response(tools=[_tool(server, tool)] if tool else [])
            return _response(servers=[item] if item.startswith("/") else [])

        report = run_harness(
            FakeClient(answer=answer), corpus, "A", "http://localhost", "abc", skills_loaded=False
        )

        assert "skill" not in report.categories
        assert report.categories["tool-capability"].hit_at_1 == 1.0
        assert report.categories["server-purpose"].hit_at_1 == 1.0
        assert report.ladder[0].level == 1

    def test_report_holds_no_token(self, corpus):
        report = run_harness(
            FakeClient(), corpus, "A", "http://localhost", None, skills_loaded=False
        )

        assert "YOUR_TOKEN" not in report.model_dump_json()


class TestCli:
    @pytest.mark.parametrize(
        "content",
        [
            '{"tokens": {"access_token": "YOUR_TOKEN"}}',
            '{"access_token": "YOUR_TOKEN"}',
            "YOUR_TOKEN\n",
        ],
    )
    def test_reads_the_token_from_each_supported_file_shape(self, tmp_path, content):
        path = tmp_path / ".token"
        path.write_text(content)

        assert run_retrieval.read_token(path) == "YOUR_TOKEN"

    def test_fails_clearly_without_a_token_file(self, tmp_path):
        with pytest.raises(HarnessError, match="Get JWT Token"):
            run_retrieval.read_token(tmp_path / "missing")

    def test_recognises_the_committed_core_corpus_in_the_manifest(self):
        _, entry = run_retrieval.corpus_hash(build_corpus.CORPUS_FILE, build_corpus.MANIFEST_FILE)

        assert entry == "core"

    def test_flags_a_corpus_the_manifest_does_not_list(self, tmp_path):
        other = tmp_path / "corpus.json"
        other.write_text("{}")

        assert run_retrieval.corpus_hash(other, build_corpus.MANIFEST_FILE)[1] is None
