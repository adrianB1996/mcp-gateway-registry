"""Whole-task runner: run an agent on each corpus task and score its tool calls.

Each task is a request written the way a user would say it, with the tools that
complete it. The runner hands the text to an agent, which searches the registry
and calls tools through the gateway. The fake MCP servers append every call they
receive to a JSON Lines log, and the runner reads the lines each task added and
scores them with ``task_completed``: the task counts as done when every expected
tool was called. Extra calls never fail a task; they are reported separately.

The log, not the agent's own account, is the source of truth, because a call
only counts if it reached the server through the gateway. The agent's search
queries and requested calls are kept beside it to explain failures.

Tasks run one at a time, so every line a task adds to the log belongs to it.

The agent is any async callable from task text to ``AgentRun``, so tests pass a
fake and ``run_tasks.py`` passes the LangGraph agent from ``agents/agent.py``.
"""

import asyncio
import json
import logging
import statistics
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from brain_eval.corpus import Corpus, Task
from brain_eval.metrics import task_completed

logger = logging.getLogger(__name__)

SPLITS: tuple[str, ...] = ("tuning", "hidden", "all")
NO_GROUP: str = "none"
DEFAULT_TIMEOUT_SECONDS: float = 300.0


class AgentRun(BaseModel):
    """What the agent did on one task, as the agent reports it."""

    final_answer: str = ""
    searches: list[str] = Field(default_factory=list, description="Search queries, in order")
    invocations: list[str] = Field(
        default_factory=list, description="Tools the agent asked to call, as server/tool"
    )
    steps: int = Field(default=0, description="Model turns")
    error: str | None = None


AgentFn = Callable[[str], Awaitable[AgentRun]]


class LoggedCall(BaseModel):
    """One line of the fake servers' call log."""

    server: str
    tool: str
    known_tool: bool = True

    @property
    def tool_id(self) -> str:
        """The corpus tool id, ``server/tool``."""
        return f"{self.server}/{self.tool}"


class CallLog:
    """Read the lines the fake servers appended since a byte offset."""

    def __init__(
        self,
        path: Path,
    ) -> None:
        self.path = path

    def offset(self) -> int:
        """Return the current end of the log, or 0 if it does not exist yet."""
        return self.path.stat().st_size if self.path.exists() else 0

    def read_since(
        self,
        offset: int,
    ) -> list[LoggedCall]:
        """Return every call written after ``offset``, in order."""
        if not self.path.exists():
            return []
        with self.path.open("rb") as handle:
            handle.seek(offset)
            text = handle.read().decode("utf-8")
        return [LoggedCall.model_validate(json.loads(line)) for line in text.splitlines() if line]


class TaskResult(BaseModel):
    """One task's score and everything needed to explain it."""

    task_id: str
    run: int = Field(description="Repeat number, from 0")
    goal_id: str
    domain: str
    split: str
    group: str | None
    goal_seen_in_tuning: bool
    text: str
    expected_tools: list[str]
    called: list[str] = Field(description="Tools the servers saw called, in order")
    completed: float
    missing: list[str]
    extra_calls: int = Field(description="Calls to tools the task does not expect")
    unknown_calls: int = Field(description="Calls naming a tool the server does not have")
    searches: list[str]
    invocations: list[str]
    steps: int
    final_answer: str
    error: str | None
    elapsed_seconds: float


class Summary(BaseModel):
    """Mean scores over a set of task results."""

    tasks: int
    completion: float
    mean_extra_calls: float
    mean_searches: float
    errors: int


class TaskReport(BaseModel):
    """Everything one whole-task run measured. Holds no token or credential."""

    arm: str
    generated_at: datetime
    registry_url: str
    provider: str
    model: str
    model_digest: str | None = Field(description="Ollama digest, so a run names its exact model")
    corpus_sha256: str | None
    split: str
    repeats: int
    system_prompt: str = Field(description="The eval system prompt, before the time is filled in")
    overall: Summary
    run_completion: list[float] = Field(description="Completion of each repeat, for the spread")
    splits: dict[str, Summary]
    groups: dict[str, Summary]
    goal_seen: dict[str, Summary] = Field(description="Hidden tasks by whether tuning saw the goal")
    goals: dict[str, Summary]
    tasks: list[TaskResult]


def select_tasks(
    corpus: Corpus,
    split: str = "all",
    task_ids: Sequence[str] = (),
    limit: int | None = None,
) -> list[Task]:
    """Pick the tasks to run, in corpus order.

    Args:
        corpus: The corpus.
        split: "tuning", "hidden" or "all".
        task_ids: Run only these tasks. Empty means every task in the split.
        limit: Keep at most this many after filtering. None or 0 keeps all.

    Returns:
        The selected tasks.

    Raises:
        ValueError: The split is unknown, a task id is not in the corpus, or
            nothing is left to run.
    """
    if split not in SPLITS:
        raise ValueError(f"Unknown split '{split}'. Use one of: {', '.join(SPLITS)}")
    tasks = [task for task in corpus.tasks if split == "all" or task.split == split]
    if task_ids:
        unknown = set(task_ids) - {task.id for task in corpus.tasks}
        if unknown:
            raise ValueError(f"Not in the corpus: {', '.join(sorted(unknown))}")
        wanted = set(task_ids)
        tasks = [task for task in tasks if task.id in wanted]
    if limit:
        tasks = tasks[:limit]
    if not tasks:
        raise ValueError(f"No tasks match split={split} and the given task ids")
    return tasks


def score_task(
    task: Task,
    calls: Sequence[LoggedCall],
    agent_run: AgentRun,
    run: int,
    elapsed_seconds: float,
) -> TaskResult:
    """Score one task from the calls its run added to the log.

    Args:
        task: The task.
        calls: The calls the servers recorded during the task.
        agent_run: The agent's own account, kept to explain the score.
        run: Repeat number.
        elapsed_seconds: Wall time of the task.

    Returns:
        The scored result.
    """
    called = [call.tool_id for call in calls if call.known_tool]
    expected = set(task.expected_tools)
    return TaskResult(
        task_id=task.id,
        run=run,
        goal_id=task.goal_id,
        domain=task.domain,
        split=task.split,
        group=task.group,
        goal_seen_in_tuning=task.goal_seen_in_tuning,
        text=task.text,
        expected_tools=task.expected_tools,
        called=called,
        completed=task_completed(called, task.expected_tools),
        missing=[tool for tool in task.expected_tools if tool not in called],
        extra_calls=sum(1 for tool in called if tool not in expected),
        unknown_calls=sum(1 for call in calls if not call.known_tool),
        searches=agent_run.searches,
        invocations=agent_run.invocations,
        steps=agent_run.steps,
        final_answer=agent_run.final_answer,
        error=agent_run.error,
        elapsed_seconds=round(elapsed_seconds, 2),
    )


async def _run_agent(
    agent: AgentFn,
    text: str,
    timeout_seconds: float,
) -> AgentRun:
    """Run the agent on one task, turning a timeout or crash into an error result.

    The calls a failed run made are still in the log and still scored, so a run
    that called every right tool and then crashed counts as complete.
    """
    try:
        return await asyncio.wait_for(agent(text), timeout=timeout_seconds)
    except TimeoutError:
        return AgentRun(error=f"timed out after {timeout_seconds:.0f}s")
    except Exception as error:
        logger.exception("Agent failed on a task")
        return AgentRun(error=f"{type(error).__name__}: {error}")


async def run_tasks(
    tasks: Sequence[Task],
    agent: AgentFn,
    call_log: CallLog,
    repeats: int = 1,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[TaskResult]:
    """Run every task ``repeats`` times, one at a time, and score each run.

    Args:
        tasks: The tasks to run.
        agent: Async callable from task text to ``AgentRun``.
        call_log: The fake servers' call log.
        repeats: Runs per task. Models that cannot run at temperature 0 need
            several to show the spread.
        timeout_seconds: Limit per task.

    Returns:
        One result per task per repeat.

    Raises:
        ValueError: repeats is below 1.
    """
    if repeats < 1:
        raise ValueError(f"repeats must be at least 1, got {repeats}")
    results: list[TaskResult] = []
    total = len(tasks) * repeats
    for run in range(repeats):
        for task in tasks:
            offset = call_log.offset()
            started = time.perf_counter()
            agent_run = await _run_agent(agent, task.text, timeout_seconds)
            elapsed = time.perf_counter() - started
            result = score_task(task, call_log.read_since(offset), agent_run, run, elapsed)
            results.append(result)
            logger.info(
                f"[{len(results)}/{total}] {task.id} run {run}: "
                f"completed={result.completed:.0f} called={result.called} "
                f"missing={result.missing} error={result.error} ({elapsed:.1f}s)"
            )
    return results


def summarize(
    results: Sequence[TaskResult],
) -> Summary:
    """Average the scores over a set of results.

    Raises:
        ValueError: There are no results.
    """
    if not results:
        raise ValueError("Cannot summarize an empty set of results")
    return Summary(
        tasks=len(results),
        completion=round(statistics.fmean(r.completed for r in results), 4),
        mean_extra_calls=round(statistics.fmean(r.extra_calls for r in results), 4),
        mean_searches=round(statistics.fmean(len(r.searches) for r in results), 4),
        errors=sum(1 for r in results if r.error),
    )


def summarize_by(
    results: Sequence[TaskResult],
    key: Callable[[TaskResult], str],
) -> dict[str, Summary]:
    """Group results by a key and summarize each group, sorted by key."""
    groups: dict[str, list[TaskResult]] = defaultdict(list)
    for result in results:
        groups[key(result)].append(result)
    return {name: summarize(group) for name, group in sorted(groups.items())}


def run_completion(
    results: Sequence[TaskResult],
) -> list[float]:
    """Return each repeat's completion rate, in repeat order."""
    by_run = summarize_by(results, lambda r: f"{r.run:06d}")
    return [summary.completion for summary in by_run.values()]


def build_report(
    results: Sequence[TaskResult],
    **fields: object,
) -> TaskReport:
    """Summarize the results overall, per split, group, goal and repeat.

    Args:
        results: Every scored run.
        **fields: The report's run details (arm, generated_at, registry_url,
            provider, model, model_digest, corpus_sha256, split, repeats,
            system_prompt).

    Returns:
        The report.
    """
    hidden = [r for r in results if r.split == "hidden"]
    return TaskReport(
        overall=summarize(results),
        run_completion=run_completion(results),
        splits=summarize_by(results, lambda r: r.split),
        groups=summarize_by(results, lambda r: r.group or NO_GROUP),
        goal_seen=summarize_by(hidden, lambda r: "seen" if r.goal_seen_in_tuning else "unseen"),
        goals=summarize_by(results, lambda r: r.goal_id),
        tasks=list(results),
        **fields,
    )
