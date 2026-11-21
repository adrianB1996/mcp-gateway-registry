#!/usr/bin/env python3
"""Serve every corpus server as a fake MCP server that records its calls.

One process serves each server in a corpus file at ``/<server-name>/mcp`` over
streamable HTTP. Each server lists its tools with the exact schemas from the
corpus, answers every call with a canned result, and appends the call to a JSON
Lines log. The eval harness reads that log to check which tools an agent called
and with what arguments.

Register the servers in a local registry with their ``proxy_pass_url`` pointing
here, so agents reach them through the gateway as they would a real backend.

Usage:
    # Serve the core corpus on localhost:9100, logging calls to calls.jsonl
    uv run python scripts/brain_eval/fake_servers.py --log calls.jsonl

    # Serve a padded corpus to a gateway running in Docker
    uv run python scripts/brain_eval/fake_servers.py \\
        --corpus /tmp/brain-eval/corpus_1000.json --host 172.17.0.1 \\
        --allowed-host 172.17.0.1:9100 --allowed-host host.docker.internal:9100

The servers hold no data and check no credentials. They exist for local eval
runs only, so they bind to 127.0.0.1 unless told otherwise.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import sys
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server.lowlevel.server import Server
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Mount

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from brain_eval.build_corpus import CORPUS_FILE  # noqa: E402
from brain_eval.corpus import Corpus  # noqa: E402
from brain_eval.corpus import Server as CorpusServer  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_HOST: str = "127.0.0.1"
DEFAULT_PORT: int = 9100
DEFAULT_LOG: str = "brain-eval-calls.jsonl"
MCP_PATH: str = "/mcp"
LIST_RESULT_SIZE: int = 3
LOCAL_HOSTS: tuple[str, ...] = ("127.0.0.1:*", "localhost:*", "[::1]:*")


class CallRecorder:
    """Append each tool call to a JSON Lines file, one object per line."""

    def __init__(
        self,
        path: Path,
    ) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def record(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        known: bool,
    ) -> None:
        """Write one call. The arguments are synthetic eval data, never real input."""
        line = json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "server": server,
                "tool": tool,
                "arguments": arguments,
                "known_tool": known,
            },
            sort_keys=True,
            default=str,
        )
        async with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


def _stable_id(
    *parts: str,
) -> str:
    """Return a short id that is the same for the same call every time."""
    # Not a security use: a deterministic label for canned results.
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:10]


def canned_result(
    server: str,
    tool: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Return a plausible, deterministic result for a call.

    List-shaped tools return a few items so an agent has something to act on
    next; everything else confirms the action with a stable id.
    """
    call_id = _stable_id(server, tool, json.dumps(arguments, sort_keys=True, default=str))
    result: dict[str, Any] = {"ok": True, "server": server, "tool": tool, "id": f"{tool}-{call_id}"}
    if tool.startswith(("list_", "search", "items_", "late_", "upcoming_", "devices_", "stale_")):
        result["items"] = [
            {"id": f"{server}-{_stable_id(call_id, str(index))}", "name": f"Example {index + 1}"}
            for index in range(LIST_RESULT_SIZE)
        ]
    else:
        result["message"] = f"{tool} completed"
        result["input"] = arguments
    return result


def _mcp_tools(
    server: CorpusServer,
) -> list[types.Tool]:
    """Convert a corpus server's tools into MCP tool definitions."""
    return [
        types.Tool(
            name=tool.name,
            description=tool.description,
            inputSchema=tool.input_schema,
            annotations=types.ToolAnnotations(readOnlyHint=tool.read_only),
        )
        for tool in server.tools
    ]


def build_server(
    server: CorpusServer,
    recorder: CallRecorder,
) -> Server:
    """Build a low-level MCP server for one corpus server.

    Args:
        server: The corpus server whose tools to expose.
        recorder: Where every call is written.

    Returns:
        An MCP server that lists the tools and answers calls with canned results.
    """
    tools = _mcp_tools(server)
    names = {tool.name for tool in tools}

    async def _list_tools(
        ctx: Any,
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def _call_tool(
        ctx: Any,
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        arguments = dict(params.arguments or {})
        known = params.name in names
        await recorder.record(server.name, params.name, arguments, known)
        if not known:
            message = f"Unknown tool '{params.name}' on {server.name}"
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=message)], isError=True
            )
        payload = canned_result(server.name, params.name, arguments)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(payload))],
            structuredContent=payload,
        )

    return Server(
        server.name,
        description=server.description,
        on_list_tools=_list_tools,
        on_call_tool=_call_tool,
    )


def _security(
    allowed_hosts: list[str],
) -> TransportSecuritySettings:
    """Keep DNS-rebinding protection on, allowing localhost plus the given hosts."""
    hosts = [*LOCAL_HOSTS, *allowed_hosts]
    origins = [f"http://{host}" for host in hosts]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
    )


def build_app(
    corpus: Corpus,
    recorder: CallRecorder,
    include_padding: bool,
    allowed_hosts: list[str],
) -> Starlette:
    """Mount one fake MCP server per corpus server under ``/<name>``.

    Args:
        corpus: The corpus to serve.
        recorder: Where every call is written.
        include_padding: Serve filler servers too. Agents only reach them by
            mistake, so the core servers are enough for most runs.
        allowed_hosts: Host headers to accept besides localhost.

    Returns:
        A Starlette app whose lifespan runs every server's session manager.
    """
    servers = [server for server in corpus.servers if include_padding or not server.padding]
    security = _security(allowed_hosts)
    mcp_servers = [build_server(server, recorder) for server in servers]
    mounts = [
        Mount(
            f"/{server.name}",
            app=mcp_server.streamable_http_app(
                streamable_http_path=MCP_PATH,
                stateless_http=True,
                json_response=True,
                transport_security=security,
            ),
        )
        for server, mcp_server in zip(servers, mcp_servers, strict=True)
    ]

    @asynccontextmanager
    async def _lifespan(app: Starlette) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            for mcp_server in mcp_servers:
                await stack.enter_async_context(mcp_server.session_manager.run())
            logger.info(f"Serving {len(mcp_servers)} fake MCP servers")
            yield

    return Starlette(routes=mounts, lifespan=_lifespan)


def load_corpus(
    path: Path,
) -> Corpus:
    """Read a corpus file written by build_corpus.py.

    Raises:
        FileNotFoundError: The file does not exist; the message says how to build it.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"No corpus at {path}. Run scripts/brain_eval/build_corpus.py first."
        )
    return Corpus.model_validate_json(path.read_text(encoding="utf-8"))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve the brain-eval corpus as fake MCP servers that record every call.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  uv run python scripts/brain_eval/fake_servers.py --log calls.jsonl
  uv run python scripts/brain_eval/fake_servers.py --corpus /tmp/brain-eval/corpus_1000.json --include-padding
  uv run python scripts/brain_eval/fake_servers.py --host 172.17.0.1 --allowed-host 172.17.0.1:9100
""",
    )
    parser.add_argument(
        "--corpus", type=Path, default=CORPUS_FILE, help=f"Corpus file (default: {CORPUS_FILE})"
    )
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help=f"Address to bind (default: {DEFAULT_HOST})"
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"Port to bind (default: {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--log", type=Path, default=Path(DEFAULT_LOG), help=f"Call log (default: {DEFAULT_LOG})"
    )
    parser.add_argument(
        "--include-padding", action="store_true", help="Also serve the filler servers"
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="Host header to accept besides localhost, such as host.docker.internal:9100; repeatable",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    return parser.parse_args()


def main() -> int:
    """Start the fake servers and return the process exit code."""
    import uvicorn

    args = _parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    try:
        corpus = load_corpus(args.corpus)
    except FileNotFoundError as error:
        logger.error(str(error))
        return 1
    if args.host != DEFAULT_HOST:
        logger.warning(f"Binding to {args.host}; these servers check no credentials")
    started = time.perf_counter()
    app = build_app(corpus, CallRecorder(args.log), args.include_padding, args.allowed_host)
    logger.info(f"Built the app in {time.perf_counter() - started:.1f}s; calls go to {args.log}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="debug" if args.debug else "info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
