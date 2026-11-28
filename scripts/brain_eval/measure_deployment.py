#!/usr/bin/env python3
"""Phase 0 deployment measurement for the knowledge-graph search plan.

Reads the registry's server tool lists and recent MCP audit records and reports,
as JSON:

- how many tool names more than one server exposes, and how many of those share
  a description;
- whether callers carry a user identity, or arrive on a shared token;
- what callers do after a search_registry call;
- how many episodes call tools on more than one server;
- how often a caller reuses the same set of tools, and how many tool sets
  several distinct users share.

The report holds counts and tool names, never usernames. The design that uses
these numbers is docs/design/knowledge-graph-search.md ("Measure the problem
first").

Usage:
    # Last 7 days, connection from the registry environment / .env
    uv run python scripts/brain_eval/measure_deployment.py --output brain-report.json

    # Amazon DocumentDB on ECS (password from DOCUMENTDB_PASSWORD, never argv)
    DOCUMENTDB_PASSWORD=... uv run python scripts/brain_eval/measure_deployment.py \\
        --storage-backend documentdb --host docdb.cluster-xxxx.us-east-1.docdb.amazonaws.com \\
        --username admin --tls --tls-ca-file global-bundle.pem --output brain-report.json

CONNECTION: with ``--host`` or ``MONGODB_CONNECTION_STRING`` the script connects
directly and needs no registry secrets. With neither, it uses the registry's own
client and settings, which only load where the registry environment is complete
(SECRET_KEY and the other required secrets), so run it inside a registry
container in that case.

The audit records only exist when ``AUDIT_LOG_MONGODB_ENABLED`` is true, and they
expire after ``AUDIT_LOG_MONGODB_TTL_DAYS`` (7 by default).
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Make the repo root (for ``registry``) and ``scripts/`` (for ``_mongo_conn_args``
# and ``brain_eval``) importable however the script is started.
_SCRIPT_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _SCRIPT_DIR.parent
_REPO_ROOT = _SCRIPTS_DIR.parent
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from _mongo_conn_args import (  # noqa: E402
    PASSWORD_ENV_VAR,
    add_connection_args,
    apply_connection_overrides,
    build_connection_string,
)

from brain_eval.deployment_stats import (  # noqa: E402
    MCP_ACCESS_LOG_TYPE,
    TOOLS_CALL_METHOD,
    AnalysisConfig,
    DeploymentReport,
    ToolCall,
    build_report,
    tool_call_from_audit_event,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)
logger = logging.getLogger(__name__)

# Fields read from each collection. Nothing else is fetched, so call arguments,
# headers and claims never leave the database.
SERVER_PROJECTION: dict[str, int] = {
    "server_name": 1,
    "is_enabled": 1,
    "tool_list.name": 1,
    "tool_list.description": 1,
}
AUDIT_PROJECTION: dict[str, int] = {
    "log_type": 1,
    "timestamp": 1,
    "identity.username": 1,
    "mcp_server.name": 1,
    "mcp_request.method": 1,
    "mcp_request.tool_name": 1,
    "mcp_response.status": 1,
}

# Direct-connection settings. These mirror the registry's own environment
# variables so the same values work in both connection modes.
CONNECTION_STRING_ENV: str = "MONGODB_CONNECTION_STRING"
NAMESPACE_ENV: str = "DOCUMENTDB_NAMESPACE"
DATABASE_ENV: str = "DOCUMENTDB_DATABASE"
TLS_CA_FILE_ENV: str = "DOCUMENTDB_TLS_CA_FILE"
AUDIT_TTL_ENV: str = "AUDIT_LOG_MONGODB_TTL_DAYS"
DEFAULT_NAMESPACE: str = "default"
DEFAULT_DATABASE: str = "mcp_registry"
DEFAULT_AUDIT_TTL_DAYS: int = 7
SERVER_SELECTION_TIMEOUT_MS: int = 10000

# Environment fallback for the shared-user floor, so the measurement uses the
# same value the brain will be configured with.
SHARED_MIN_USERS_ENV: str = "BRAIN_SHARED_MIN_USERS"

AUDIT_DISABLED_HINT: str = (
    "No MCP tool-call audit records were found in the window. Check that "
    "AUDIT_LOG_MONGODB_ENABLED is true on the auth server, that traffic reached the "
    "gateway in the last --days days, and that AUDIT_LOG_MONGODB_TTL_DAYS has not "
    "expired the records. See docs/design/knowledge-graph-search.md."
)


class MeasurementError(RuntimeError):
    """Raised when the deployment holds no data the measurement can use."""


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Measure tool collisions and search usage in a running registry "
        "(Phase 0 of the knowledge-graph search plan).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  # Last 7 days, connection from the registry environment, report to a file
  uv run python scripts/brain_eval/measure_deployment.py --output brain-report.json

  # Last 3 days, treat a tool set as shared once 2 users use it
  uv run python scripts/brain_eval/measure_deployment.py --days 3 --shared-min-users 2

  # Local MongoDB CE reached directly, with debug logging
  DOCUMENTDB_PASSWORD=... uv run python scripts/brain_eval/measure_deployment.py \\
      --host localhost --username admin --direct-connection --debug
""",
    )
    parser.add_argument(
        "--days", type=int, default=7, help="Days of audit records to read (default: 7)"
    )
    parser.add_argument(
        "--episode-gap-minutes",
        type=int,
        default=30,
        help="Idle minutes that end a caller's episode (default: 30)",
    )
    parser.add_argument(
        "--repeat-search-minutes",
        type=int,
        default=5,
        help="A second search within this many minutes with no call between counts as a "
        "repeat (default: 5)",
    )
    parser.add_argument(
        "--overlap-threshold",
        type=float,
        default=0.5,
        help="Jaccard overlap at which two tool sets count as the same goal (default: 0.5)",
    )
    parser.add_argument(
        "--shared-min-users",
        type=int,
        default=None,
        help=f"Distinct users before a tool set counts as shared (default: ${SHARED_MIN_USERS_ENV} or 3)",
    )
    parser.add_argument(
        "--gateway-server", default="mcpgw", help="Audit server name of mcpgw (default: mcpgw)"
    )
    parser.add_argument(
        "--search-tool",
        default="search_registry",
        help="Search tool name (default: search_registry)",
    )
    parser.add_argument(
        "--skill-tool",
        default="get_skill_content",
        help="Skill load tool name (default: get_skill_content)",
    )
    parser.add_argument(
        "--include-disabled",
        action="store_true",
        help="Count tools on disabled servers in the collision report",
    )
    parser.add_argument("--top", type=int, default=20, help="Rows in each top list (default: 20)")
    parser.add_argument(
        "--output", default=None, help="Write the JSON report here (default: stdout)"
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--namespace",
        default=None,
        help=f"Collection namespace for a direct connection (default: ${NAMESPACE_ENV} or {DEFAULT_NAMESPACE})",
    )
    parser.add_argument(
        "--tls-ca-file",
        default=None,
        help=f"CA bundle for a direct TLS connection, such as the Amazon DocumentDB bundle "
        f"(default: ${TLS_CA_FILE_ENV})",
    )
    add_connection_args(parser)
    return parser.parse_args()


def _build_config(
    args: argparse.Namespace,
) -> AnalysisConfig:
    """Build the run settings, taking the flag over the environment for the user floor."""
    shared_min_users = args.shared_min_users
    if shared_min_users is None:
        shared_min_users = int(os.environ.get(SHARED_MIN_USERS_ENV, "3"))
    return AnalysisConfig(
        window_days=args.days,
        episode_gap_minutes=args.episode_gap_minutes,
        repeat_search_minutes=args.repeat_search_minutes,
        overlap_threshold=args.overlap_threshold,
        shared_min_users=shared_min_users,
        gateway_server=args.gateway_server,
        search_tool=args.search_tool,
        skill_tool=args.skill_tool,
        top_n=args.top,
    )


async def _fetch_servers(
    db: Any,
    collection_name: str,
    include_disabled: bool,
) -> list[dict[str, Any]]:
    """Read server tool lists, logging how the enabled filter changed the count.

    Raises:
        MeasurementError: The collection holds no servers.
    """
    servers: list[dict[str, Any]] = []
    async for doc in db[collection_name].find({}, SERVER_PROJECTION):
        doc["path"] = doc.pop("_id", None)
        servers.append(doc)
    if not servers:
        raise MeasurementError(
            f"Collection '{collection_name}' holds no servers. Check the connection and "
            "DOCUMENTDB_NAMESPACE."
        )
    if include_disabled:
        logger.info(f"Servers: {len(servers)} read, disabled servers included")
        return servers
    enabled = [doc for doc in servers if doc.get("is_enabled")]
    logger.info(f"Servers: 'is_enabled=true': {len(servers)} -> {len(enabled)}")
    return enabled


async def _fetch_tool_calls(
    db: Any,
    collection_name: str,
    since: datetime,
) -> list[ToolCall]:
    """Read MCP tool calls since ``since``, logging each filter's effect on the count.

    Raises:
        MeasurementError: No usable tool-call records exist in the window.
    """
    query = {
        "log_type": MCP_ACCESS_LOG_TYPE,
        "timestamp": {"$gte": since},
        "mcp_request.method": TOOLS_CALL_METHOD,
    }
    read = 0
    calls: list[ToolCall] = []
    async for doc in db[collection_name].find(query, AUDIT_PROJECTION):
        read += 1
        call = tool_call_from_audit_event(doc)
        if call is not None:
            calls.append(call)
    logger.info(
        f"Audit: 'log_type={MCP_ACCESS_LOG_TYPE}, method={TOOLS_CALL_METHOD}, "
        f"since {since.isoformat()}': {read} records"
    )
    logger.info(f"Audit: 'status=success, tool named': {read} -> {len(calls)} tool calls")
    if not calls:
        raise MeasurementError(AUDIT_DISABLED_HINT)
    return calls


async def _open_direct(
    args: argparse.Namespace,
) -> tuple[Any, Callable[[str], str], int, Callable[[], Awaitable[None]]]:
    """Connect with motor from the CLI args or MONGODB_CONNECTION_STRING.

    Needs none of the registry's secrets. The URI may hold a password, so it is
    never logged.

    Returns:
        (database, collection-name function, audit TTL days, close function)
    """
    from motor.motor_asyncio import AsyncIOMotorClient

    if args.host:
        uri = build_connection_string(args)
        database = args.database
        logger.info(
            f"Connecting directly to {args.host}:{args.port}/{database} (password from ${PASSWORD_ENV_VAR})"
        )
    else:
        uri = os.environ[CONNECTION_STRING_ENV]
        database = os.environ.get(DATABASE_ENV, DEFAULT_DATABASE)
        logger.info(f"Connecting directly with ${CONNECTION_STRING_ENV}, database {database}")
    options: dict[str, Any] = {
        "retryWrites": False,
        "serverSelectionTimeoutMS": SERVER_SELECTION_TIMEOUT_MS,
    }
    ca_file = args.tls_ca_file or os.environ.get(TLS_CA_FILE_ENV)
    if ca_file:
        options["tlsCAFile"] = ca_file
    client = AsyncIOMotorClient(uri, **options)
    namespace = args.namespace or os.environ.get(NAMESPACE_ENV, DEFAULT_NAMESPACE)
    ttl_days = int(os.environ.get(AUDIT_TTL_ENV, str(DEFAULT_AUDIT_TTL_DAYS)))

    async def _close() -> None:
        client.close()

    # Same naming as registry.repositories.documentdb.client.get_collection_name.
    return client[database], lambda base: f"{base}_{namespace}", ttl_days, _close


async def _open_registry(
    args: argparse.Namespace,
) -> tuple[Any, Callable[[str], str], int, Callable[[], Awaitable[None]]]:
    """Connect with the registry's own client and settings.

    Returns:
        (database, collection-name function, audit TTL days, close function)
    """
    # Imported here so apply_connection_overrides() runs before the registry
    # settings singleton is created.
    apply_connection_overrides(args)
    from registry.core.config import settings
    from registry.repositories.documentdb.client import (
        close_documentdb_client,
        get_collection_name,
        get_documentdb_client,
    )

    db = await get_documentdb_client()
    return db, get_collection_name, settings.audit_log_mongodb_ttl_days, close_documentdb_client


async def _collect(
    args: argparse.Namespace,
    config: AnalysisConfig,
) -> tuple[list[dict[str, Any]], list[ToolCall]]:
    """Read server tool lists and the audit window from DocumentDB."""
    direct = bool(args.host or os.environ.get(CONNECTION_STRING_ENV))
    db, collection_name, ttl_days, close = await (
        _open_direct(args) if direct else _open_registry(args)
    )
    if config.window_days > ttl_days:
        logger.warning(
            f"--days {config.window_days} exceeds the audit TTL ({ttl_days} days); "
            f"only the last {ttl_days} days can exist"
        )
    since = datetime.now(UTC) - timedelta(days=config.window_days)
    try:
        servers = await _fetch_servers(db, collection_name("mcp_servers"), args.include_disabled)
        logger.warning(
            f"Reading every MCP tool call since {since.isoformat()}; large windows take a while"
        )
        calls = await _fetch_tool_calls(db, collection_name("audit_events"), since)
    finally:
        await close()
    return servers, calls


def _log_summary(
    report: DeploymentReport,
) -> None:
    """Log the headline numbers a reader needs to judge the stop rules."""
    summary = {
        "tools": report.collisions.tool_count,
        "shared_tool_share": report.collisions.shared_tool_share,
        "calls_without_user_share": report.identity.calls_without_user_share,
        "top_caller_share": report.identity.top_caller_share,
        "searches": report.searches.searches,
        "search_followed_by_call_share": report.searches.followed_by_backend_call_share,
        "backend_calls_after_search_share": report.searches.backend_calls_after_search_share,
        "multi_server_share": report.episodes.multi_server_share,
        "personal_repeat_share": report.repeats.personal_repeat_share,
        "shared_tool_sets": report.repeats.shared_tool_sets,
    }
    logger.info(f"Summary:\n{json.dumps(summary, indent=2)}")


def _write_report(
    report: DeploymentReport,
    output: str | None,
) -> None:
    """Write the report as JSON to a file, or to stdout when no file is given."""
    text = report.model_dump_json(indent=2)
    if output is None:
        print(text)
        return
    Path(output).write_text(text + "\n", encoding="utf-8")
    logger.info(f"Wrote report to {output}")


async def main() -> int:
    """Run the measurement and return the process exit code."""
    args = _parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    config = _build_config(args)
    logger.info(f"Configuration:\n{json.dumps(config.model_dump(), indent=2)}")

    started = time.perf_counter()
    try:
        servers, calls = await _collect(args, config)
    except MeasurementError as error:
        logger.error(str(error))
        return 1
    callers = Counter(call.caller is None for call in calls)
    logger.debug(f"Calls with a user: {callers[False]}, without: {callers[True]}")

    report = build_report(servers, calls, config)
    _log_summary(report)
    _write_report(report, args.output)
    logger.info(f"Finished in {time.perf_counter() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
