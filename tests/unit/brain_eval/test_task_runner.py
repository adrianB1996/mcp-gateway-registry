"""Unit tests for scripts/brain_eval/task_runner.py and run_tasks.py."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from brain_eval import build_corpus, run_tasks
from brain_eval.fake_servers import load_corpus
from brain_eval.task_runner import (
    AgentRun,
    CallLog,
    LoggedCall,
    build_report,
    score_task,
    select_tasks,
)
from brain_eval.task_runner import run_tasks as run_all

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def corpus():
    return load_corpus(build_corpus.CORPUS_FILE)


@pytest.fixture
def task(corpus):
    return next(t for t in corpus.tasks if len(t.expected_tools) >= 2)


def _write_calls(path, tool_ids, known=True):
    with path.open("a", encoding="utf-8") as handle:
        for tool_id in tool_ids:
            server, tool = tool_id.split("/")
            line = {"server": server, "tool": tool, "known_tool": known, "arguments": {}}
            handle.write(json.dumps(line) + "\n")


def _calls(tool_ids, known=True):
    return [
        LoggedCall(server=t.split("/")[0], tool=t.split("/")[1], known_tool=known) for t in tool_ids
    ]


class TestSelectTasks:
    def test_all_split_keeps_every_task(self, corpus):
        assert len(select_tasks(corpus)) == len(corpus.tasks)

    def test_split_filters(self, corpus):
        tasks = select_tasks(corpus, "hidden")
        assert tasks and all(t.split == "hidden" for t in tasks)

    def test_task_ids_and_limit(self, corpus):
        ids = [t.id for t in corpus.tasks[:3]]
        assert [t.id for t in select_tasks(corpus, task_ids=ids, limit=2)] == ids[:2]

    def test_unknown_task_id_is_refused(self, corpus):
        with pytest.raises(ValueError, match="Not in the corpus: nope"):
            select_tasks(corpus, task_ids=["nope"])

    def test_unknown_split_is_refused(self, corpus):
        with pytest.raises(ValueError, match="Unknown split"):
            select_tasks(corpus, "train")

    def test_empty_selection_is_refused(self, corpus):
        hidden = next(t for t in corpus.tasks if t.split == "hidden")
        with pytest.raises(ValueError, match="No tasks match"):
            select_tasks(corpus, "tuning", task_ids=[hidden.id])


class TestCallLog:
    def test_missing_log_reads_as_empty(self, tmp_path):
        log = CallLog(tmp_path / "calls.jsonl")
        assert log.offset() == 0
        assert log.read_since(0) == []

    def test_reads_only_lines_after_offset(self, tmp_path):
        path = tmp_path / "calls.jsonl"
        _write_calls(path, ["a/one"])
        log = CallLog(path)
        offset = log.offset()
        _write_calls(path, ["b/two", "c/three"])

        assert [c.tool_id for c in log.read_since(offset)] == ["b/two", "c/three"]


class TestScoreTask:
    def test_all_expected_tools_called_completes(self, task):
        calls = _calls([*task.expected_tools, "other/extra"])
        result = score_task(task, calls, AgentRun(searches=["q"]), run=0, elapsed_seconds=1.234)

        assert result.completed == 1.0
        assert result.missing == []
        assert result.extra_calls == 1
        assert result.elapsed_seconds == 1.23

    def test_missing_tool_fails_and_is_named(self, task):
        result = score_task(task, _calls(task.expected_tools[:-1]), AgentRun(), 0, 0.0)

        assert result.completed == 0.0
        assert result.missing == [task.expected_tools[-1]]

    def test_unknown_tools_are_counted_not_credited(self, task):
        calls = _calls(task.expected_tools, known=False)
        result = score_task(task, calls, AgentRun(), 0, 0.0)

        assert result.completed == 0.0
        assert result.unknown_calls == len(task.expected_tools)


class TestRunTasks:
    async def test_each_task_scores_only_its_own_calls(self, corpus, tmp_path):
        tasks = corpus.tasks[:2]
        path = tmp_path / "calls.jsonl"
        by_text = {t.text: t for t in tasks}

        async def agent(text):
            _write_calls(path, by_text[text].expected_tools)
            return AgentRun(final_answer="done", steps=2)

        results = await run_all(tasks, agent, CallLog(path))

        assert [r.completed for r in results] == [1.0, 1.0]
        assert [r.called for r in results] == [t.expected_tools for t in tasks]

    async def test_crash_after_calls_still_scores_the_calls(self, task, tmp_path):
        path = tmp_path / "calls.jsonl"

        async def agent(text):
            _write_calls(path, task.expected_tools)
            raise RuntimeError("model fell over")

        [result] = await run_all([task], agent, CallLog(path))

        assert result.completed == 1.0
        assert result.error == "RuntimeError: model fell over"

    async def test_timeout_is_an_error_result(self, task, tmp_path):
        async def agent(text):
            await asyncio.sleep(5)
            return AgentRun()

        [result] = await run_all([task], agent, CallLog(tmp_path / "c.jsonl"), timeout_seconds=0.01)

        assert result.completed == 0.0
        assert result.error.startswith("timed out")

    async def test_repeats_run_every_task_again(self, corpus, tmp_path):
        async def agent(text):
            return AgentRun()

        results = await run_all(corpus.tasks[:2], agent, CallLog(tmp_path / "c.jsonl"), repeats=3)

        assert [r.run for r in results] == [0, 0, 1, 1, 2, 2]

    async def test_repeats_below_one_are_refused(self, task, tmp_path):
        with pytest.raises(ValueError, match="repeats"):
            await run_all([task], None, CallLog(tmp_path / "c.jsonl"), repeats=0)


class TestBuildReport:
    def test_summaries_split_by_split_group_goal_and_repeat(self, corpus):
        hidden = next(t for t in corpus.tasks if t.split == "hidden" and t.group)
        tuning = next(t for t in corpus.tasks if t.split == "tuning" and not t.group)
        results = [
            score_task(hidden, _calls(hidden.expected_tools), AgentRun(), 0, 0.0),
            score_task(tuning, [], AgentRun(error="boom"), 0, 0.0),
            score_task(hidden, [], AgentRun(), 1, 0.0),
            score_task(tuning, _calls(tuning.expected_tools), AgentRun(), 1, 0.0),
        ]

        report = build_report(
            results,
            arm="A",
            generated_at="2026-11-28T00:00:00Z",
            registry_url="http://localhost",
            provider="ollama",
            model="qwen3:8b",
            model_digest=None,
            corpus_sha256=None,
            split="all",
            repeats=2,
            system_prompt="prompt",
        )

        assert report.overall.completion == 0.5
        assert report.overall.errors == 1
        assert report.run_completion == [0.5, 0.5]
        assert report.splits["hidden"].tasks == 2
        assert set(report.groups) == {hidden.group, "none"}
        seen = "seen" if hidden.goal_seen_in_tuning else "unseen"
        assert list(report.goal_seen) == [seen]
        assert report.goals[tuning.goal_id].completion == 0.5


class TestAgentRunFromMessages:
    def test_reads_searches_invocations_steps_and_answer(self):
        messages = [
            SimpleNamespace(type="system", content="prompt"),
            SimpleNamespace(type="human", content="refund it"),
            SimpleNamespace(
                type="ai",
                content="",
                tool_calls=[{"name": "search_registry_tools", "args": {"query": "refund"}}],
            ),
            SimpleNamespace(type="tool", content="{}"),
            SimpleNamespace(
                type="ai",
                content="",
                tool_calls=[
                    {
                        "name": "invoke_mcp_tool",
                        "args": {"server_name": "/paystream", "tool_name": "refund_payment"},
                    }
                ],
            ),
            SimpleNamespace(type="ai", content="Refunded.", tool_calls=[]),
        ]

        run = run_tasks.agent_run_from_messages(messages)

        assert run.searches == ["refund"]
        assert run.invocations == ["paystream/refund_payment"]
        assert run.steps == 3
        assert run.final_answer == "Refunded."
