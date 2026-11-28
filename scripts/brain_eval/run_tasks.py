#!/usr/bin/env python3
"""Run the eval agent on every corpus task and score which tools it called.

This is the whole-task half of the Phase 0 baseline. The agent from
``agents/agent.py`` gets each task's text, searches the registry, and calls
tools through the gateway. The fake MCP servers log every call, and each task
scores 1 when every tool it expects was called. The report gives completion
overall, per split, per user group, per goal, and per repeat.

Before running:

1. Start the local stack and check ``http://localhost/health`` returns 200.
2. Start the fake MCP servers, logging to the file this script reads:
   ``uv run python scripts/brain_eval/fake_servers.py --log brain-eval-calls.jsonl``.
3. Load the corpus once with ``run_retrieval.py`` (it registers every server).
4. Put an admin token in ``.token`` (the "Get JWT Token" button in the UI).
5. For the default provider, pull the model: ``ollama pull qwen3:8b``.

Usage:
    # Baseline on every task with qwen3:8b on Ollama
    uv run python scripts/brain_eval/run_tasks.py --arm A --output tasks-A.json

    # Five tuning tasks, to check the setup end to end
    uv run python scripts/brain_eval/run_tasks.py --split tuning --limit 5

    # Claude confirmation pass: three repeats, because it cannot run at temperature 0
    ANTHROPIC_API_KEY=... uv run python scripts/brain_eval/run_tasks.py \\
        --provider anthropic --model claude-opus-5-5 --repeats 3 --output tasks-A-claude.json
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _SCRIPTS_DIR.parent
_AGENTS_DIR = _REPO_ROOT / "agents"
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from brain_eval.build_corpus import CORPUS_FILE, MANIFEST_FILE  # noqa: E402
from brain_eval.fake_servers import DEFAULT_LOG, load_corpus  # noqa: E402
from brain_eval.harness import HarnessError  # noqa: E402
from brain_eval.run_retrieval import (  # noqa: E402
    DEFAULT_REGISTRY_URL,
    DEFAULT_TOKEN_FILE,
    REGISTRY_URL_ENV,
    corpus_hash,
    read_token,
)
from brain_eval.task_runner import (  # noqa: E402
    DEFAULT_TIMEOUT_SECONDS,
    SPLITS,
    AgentFn,
    AgentRun,
    CallLog,
    TaskReport,
    build_report,
    run_tasks,
    select_tasks,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_PROVIDER: str = "ollama"
DEFAULT_MODEL: str = "qwen3:8b"
# LangGraph counts each model turn and each tool step; 25 is its own default.
DEFAULT_MAX_STEPS: int = 25
OLLAMA_TAGS_TIMEOUT_SECONDS: float = 5.0
SEARCH_TOOL: str = "search_registry_tools"
# The eval's own system prompt. The stock agents/system_prompt.txt tells the
# model to try its built-in tools first and carries examples for other servers,
# and with it qwen3:8b answers tasks in prose without searching. The report
# records this text so two runs can be checked for the same prompt.
EVAL_SYSTEM_PROMPT: str = """You complete requests for a user at a company. Every business system you need is an MCP server in the registry, and you can use all of them.

For each request:
1. Call search_registry_tools with a short description of the capability you need.
2. Call invoke_mcp_tool with server_name set to the result's server_path (a path such as "/example-server", never the display name), and the result's tool_name and arguments.
3. Repeat for each step the request needs, then reply with a short summary of what you did.

Use the values in the request and make reasonable assumptions for anything missing. Do not ask the user questions. The current UTC time is {current_utc_time}."""
INVOKE_TOOL: str = "invoke_mcp_tool"


def _record_tool_call(
    call: dict[str, Any],
    searches: list[str],
    invocations: list[str],
) -> None:
    """Note a search query or a requested tool call from one model tool call."""
    args = call.get("args") or {}
    if call.get("name") == SEARCH_TOOL:
        searches.append(str(args.get("query", "")))
    elif call.get("name") == INVOKE_TOOL:
        server = str(args.get("server_name", "")).strip().strip("/")
        invocations.append(f"{server}/{args.get('tool_name', '')}")


def agent_run_from_messages(
    messages: Sequence[Any],
) -> AgentRun:
    """Read the searches, requested calls and final answer from a LangGraph run.

    Args:
        messages: The run's messages. Model turns have ``type == "ai"``.

    Returns:
        The agent's account of the run.
    """
    searches: list[str] = []
    invocations: list[str] = []
    steps = 0
    final_answer = ""
    for message in messages:
        if getattr(message, "type", None) != "ai":
            continue
        steps += 1
        for call in getattr(message, "tool_calls", None) or []:
            _record_tool_call(call, searches, invocations)
        content = getattr(message, "content", "")
        if content:
            final_answer = content if isinstance(content, str) else json.dumps(content)
    return AgentRun(
        final_answer=final_answer, searches=searches, invocations=invocations, steps=steps
    )


def ollama_digest(
    base_url: str,
    model: str,
) -> str | None:
    """Return the digest of a pulled Ollama model, so the report names the exact build.

    Returns:
        The digest, or None when Ollama is unreachable or the model is not pulled.
    """
    try:
        response = httpx.get(f"{base_url}/api/tags", timeout=OLLAMA_TAGS_TIMEOUT_SECONDS)
        response.raise_for_status()
    except httpx.HTTPError as error:
        logger.warning(f"Could not read Ollama models from {base_url}: {error}")
        return None
    for entry in response.json().get("models", []):
        if model in (entry.get("name"), entry.get("model")):
            return entry.get("digest")
    logger.warning(f"{model} is not pulled in Ollama; run: ollama pull {model}")
    return None


def _load_eval_agent() -> ModuleType:
    """Import ``agents/agent.py``, which imports its neighbours by bare name."""
    if str(_AGENTS_DIR) not in sys.path:
        sys.path.insert(0, str(_AGENTS_DIR))
    import agent as eval_agent

    return eval_agent


def build_langgraph_agent(
    provider: str,
    model_id: str,
    registry_url: str,
    token: str,
    max_steps: int,
) -> AgentFn:
    """Set up the eval agent once and return a callable that runs one task.

    Each task starts a fresh conversation with only the system prompt, so no
    task sees another's history.

    Raises:
        HarnessError: The model cannot be created or the registry URL is unusable.
    """
    eval_agent = _load_eval_agent()
    eval_agent.agent_settings.auth_token = token
    eval_agent.agent_settings.registry_url = registry_url
    try:
        eval_agent._configured_registry_base_url()
    except eval_agent.AgentConfigError as error:
        raise HarnessError(str(error)) from error
    eval_agent.registry_client = eval_agent.RegistryClient(
        registry_url=registry_url, jwt_token=token
    )
    model = eval_agent._create_model(provider, model_id)
    if model is None:
        raise HarnessError(f"Could not create a {provider} model for {model_id}")
    tools = [eval_agent.calculator, eval_agent.search_registry_tools, eval_agent.invoke_mcp_tool]
    graph = eval_agent.create_react_agent(model, tools)
    system_prompt = EVAL_SYSTEM_PROMPT.format(current_utc_time=eval_agent.current_utc_time)

    async def _run(text: str) -> AgentRun:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ]
        response = await graph.ainvoke(
            {"messages": messages}, config={"recursion_limit": max_steps}
        )
        return agent_run_from_messages(response.get("messages", []))

    return _run


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the eval agent on every corpus task and score which tools it called.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  uv run python scripts/brain_eval/run_tasks.py --arm A --output tasks-A.json
  uv run python scripts/brain_eval/run_tasks.py --split tuning --limit 5
  uv run python scripts/brain_eval/run_tasks.py --task-id fin-close-month-1 --debug
  uv run python scripts/brain_eval/run_tasks.py --provider anthropic --model claude-opus-5-5 --repeats 3
""",
    )
    parser.add_argument("--arm", default="A", help="Label for the report (default: A)")
    parser.add_argument(
        "--registry-url",
        default=os.environ.get(REGISTRY_URL_ENV, DEFAULT_REGISTRY_URL),
        help=f"Registry base URL (default: ${REGISTRY_URL_ENV} or {DEFAULT_REGISTRY_URL})",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=DEFAULT_TOKEN_FILE,
        help=f"Token file (default: {DEFAULT_TOKEN_FILE})",
    )
    parser.add_argument(
        "--corpus", type=Path, default=CORPUS_FILE, help=f"Corpus file (default: {CORPUS_FILE})"
    )
    parser.add_argument(
        "--call-log",
        type=Path,
        default=Path(DEFAULT_LOG),
        help=f"The fake servers' call log, the same file as their --log (default: {DEFAULT_LOG})",
    )
    parser.add_argument(
        "--provider",
        choices=["ollama", "anthropic", "bedrock"],
        default=DEFAULT_PROVIDER,
        help=f"Model provider (default: {DEFAULT_PROVIDER})",
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL, help=f"Model id (default: {DEFAULT_MODEL})"
    )
    parser.add_argument(
        "--split", choices=SPLITS, default="all", help="Tasks to run (default: all)"
    )
    parser.add_argument(
        "--task-id", action="append", default=[], help="Run only this task; repeatable"
    )
    parser.add_argument("--limit", type=int, default=None, help="Run at most this many tasks")
    parser.add_argument(
        "--repeats", type=int, default=1, help="Runs per task, to show the spread (default: 1)"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Seconds per task (default: {DEFAULT_TIMEOUT_SECONDS:.0f})",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=DEFAULT_MAX_STEPS,
        help=f"LangGraph recursion limit per task (default: {DEFAULT_MAX_STEPS})",
    )
    parser.add_argument(
        "--output", type=Path, default=None, help="Write the JSON report here (default: stdout)"
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    return parser.parse_args()


def _log_summary(
    report: TaskReport,
) -> None:
    summary = {
        "overall": report.overall.model_dump(),
        "run_completion": report.run_completion,
        "splits": {name: s.completion for name, s in report.splits.items()},
        "groups": {name: s.completion for name, s in report.groups.items()},
        "goal_seen": {name: s.completion for name, s in report.goal_seen.items()},
    }
    logger.info(f"Arm {report.arm} whole-task results:\n{json.dumps(summary, indent=2)}")


async def _run(
    args: argparse.Namespace,
) -> TaskReport:
    """Select tasks, build the agent, run and score them."""
    corpus = load_corpus(args.corpus)
    digest, entry = corpus_hash(args.corpus, MANIFEST_FILE)
    if entry is None:
        logger.warning(f"{args.corpus} matches no entry in {MANIFEST_FILE}")
    tasks = select_tasks(corpus, args.split, args.task_id, args.limit)
    logger.warning(f"Running {len(tasks)} tasks x {args.repeats} repeats with {args.model}")
    model_digest = None
    if args.provider == "ollama":
        model_digest = ollama_digest(_load_eval_agent().ollama_base_url(), args.model)
    token = read_token(args.token_file)
    agent = build_langgraph_agent(
        args.provider, args.model, args.registry_url, token, args.max_steps
    )
    results = await run_tasks(tasks, agent, CallLog(args.call_log), args.repeats, args.timeout)
    return build_report(
        results,
        arm=args.arm,
        generated_at=datetime.now(UTC),
        registry_url=args.registry_url,
        provider=args.provider,
        model=args.model,
        model_digest=model_digest,
        corpus_sha256=digest,
        split=args.split,
        repeats=args.repeats,
        system_prompt=EVAL_SYSTEM_PROMPT,
    )


def main() -> int:
    """Run, score and report; return the process exit code."""
    args = _parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    logger.info(
        f"Configuration:\n{json.dumps({k: str(v) for k, v in vars(args).items()}, indent=2)}"
    )
    started = time.perf_counter()
    try:
        report = asyncio.run(_run(args))
    except (HarnessError, FileNotFoundError, ValueError) as error:
        logger.error(str(error))
        return 1
    _log_summary(report)
    text = report.model_dump_json(indent=2)
    if args.output is None:
        print(text)
    else:
        args.output.write_text(text + "\n", encoding="utf-8")
        logger.info(f"Wrote {args.output}")
    elapsed = time.perf_counter() - started
    logger.info(f"Finished in {int(elapsed // 60)} min {elapsed % 60:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
