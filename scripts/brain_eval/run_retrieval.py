#!/usr/bin/env python3
"""Load the brain-eval corpus into a local registry and score its search.

This is the Phase 0 baseline (arm A, today's search). Phase 1 runs the same
command against the improved search and labels it arm B. The report records
per-category NDCG@10, recall@10, MRR and top-1 hits, the collision ladder, and
every query's ranked results so two arms can be compared item by item.

Before running:

1. Start the local stack (``./build_and_run.sh`` or ``docker compose up``).
2. Start the fake MCP servers so the registry's health checks and the gateway
   can reach them: ``uv run python scripts/brain_eval/fake_servers.py``.
3. Put an admin token in ``.token`` (the "Get JWT Token" button in the UI).

Usage:
    # Baseline on the core corpus, skills fetched from this repository on GitHub
    uv run python scripts/brain_eval/run_retrieval.py --arm A \\
        --skill-base-url https://github.com/OWNER/REPO/blob/BRANCH/tests/fixtures/brain_eval/skills \\
        --output baseline-core.json

    # Same, at 1,000 tools, without skills
    uv run python scripts/brain_eval/build_corpus.py --scale 1000 --padded-dir /tmp/brain-eval
    uv run python scripts/brain_eval/run_retrieval.py --arm A --skip-skills \\
        --corpus /tmp/brain-eval/corpus_1000.json --output baseline-1000.json

    # Search again without re-registering anything
    uv run python scripts/brain_eval/run_retrieval.py --arm A --skip-load --skip-skills
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _SCRIPTS_DIR.parent
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from brain_eval.build_corpus import CORPUS_FILE, MANIFEST_FILE  # noqa: E402
from brain_eval.corpus import Corpus  # noqa: E402
from brain_eval.fake_servers import load_corpus  # noqa: E402
from brain_eval.harness import (  # noqa: E402
    DEFAULT_MAX_RESULTS,
    HarnessError,
    RegistryClientLike,
    RetrievalReport,
    load_servers,
    load_skills,
    run_retrieval,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)
logger = logging.getLogger(__name__)

REGISTRY_URL_ENV: str = "REGISTRY_URL"
FAKE_BASE_URL_ENV: str = "BRAIN_EVAL_FAKE_BASE_URL"
SKILL_BASE_URL_ENV: str = "BRAIN_EVAL_SKILL_BASE_URL"
DEFAULT_REGISTRY_URL: str = "http://localhost"
# The registry runs in Docker and reaches the fake servers on the host.
DEFAULT_FAKE_BASE_URL: str = "http://host.docker.internal:9100"
DEFAULT_TOKEN_FILE: Path = _REPO_ROOT / ".token"


def read_token(
    path: Path,
) -> str:
    """Read a bearer token from a file, never from argv.

    Accepts the registry UI's download (JSON with ``tokens.access_token``), JSON
    with a top-level ``access_token``, or a file holding only the token.

    Raises:
        HarnessError: The file is missing or holds no token.
    """
    if not path.exists():
        raise HarnessError(
            f"No token file at {path}. Use the 'Get JWT Token' button in the registry UI."
        )
    text = path.read_text(encoding="utf-8").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        token = (data.get("tokens") or {}).get("access_token") or data.get("access_token")
    else:
        token = text
    if not token or not isinstance(token, str):
        raise HarnessError(f"{path} holds no access token")
    return token


def corpus_hash(
    corpus_path: Path,
    manifest_path: Path,
) -> tuple[str, str | None]:
    """Hash the corpus file and name the manifest entry it matches, if any.

    Returns:
        (sha256, "core" or the scale, or None when it matches nothing)
    """
    digest = hashlib.sha256(corpus_path.read_bytes()).hexdigest()
    if not manifest_path.exists():
        return digest, None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("core", {}).get("sha256") == digest:
        return digest, "core"
    for scale, entry in manifest.get("scales", {}).items():
        if entry.get("sha256") == digest:
            return digest, scale
    return digest, None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load the brain-eval corpus into a local registry and score its search.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  uv run python scripts/brain_eval/run_retrieval.py --arm A --skip-skills --output baseline.json
  uv run python scripts/brain_eval/run_retrieval.py --arm A \\
      --skill-base-url https://github.com/OWNER/REPO/blob/BRANCH/tests/fixtures/brain_eval/skills
  uv run python scripts/brain_eval/run_retrieval.py --arm B --skip-load --skip-skills
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
        "--fake-base-url",
        default=os.environ.get(FAKE_BASE_URL_ENV, DEFAULT_FAKE_BASE_URL),
        help=f"Where the registry reaches the fake MCP servers (default: ${FAKE_BASE_URL_ENV} or {DEFAULT_FAKE_BASE_URL})",
    )
    parser.add_argument(
        "--skill-base-url",
        default=os.environ.get(SKILL_BASE_URL_ENV),
        help=f"Public URL of tests/fixtures/brain_eval/skills (default: ${SKILL_BASE_URL_ENV})",
    )
    parser.add_argument(
        "--skill-token-env",
        default=None,
        help="Name of an environment variable holding a token for a private skill repository, "
        "such as GITHUB_TOKEN. The token is read from the environment, never argv.",
    )
    parser.add_argument(
        "--skip-skills", action="store_true", help="Do not load skills and skip skill lookups"
    )
    parser.add_argument(
        "--skip-load", action="store_true", help="Search only; the corpus is already loaded"
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=DEFAULT_MAX_RESULTS,
        help=f"Results per lookup and metric cut-off (default: {DEFAULT_MAX_RESULTS})",
    )
    parser.add_argument(
        "--output", type=Path, default=None, help="Write the JSON report here (default: stdout)"
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    return parser.parse_args()


def _load(
    client: RegistryClientLike,
    args: argparse.Namespace,
    corpus: Corpus,
) -> bool:
    """Register the corpus and return whether skills were loaded."""
    from api.registry_client import InternalServiceRegistration, SkillRegistrationRequest

    started = time.perf_counter()
    count = load_servers(client, corpus.servers, args.fake_base_url, InternalServiceRegistration)
    logger.info(f"Registered and enabled {count} servers in {time.perf_counter() - started:.1f}s")
    if args.skip_skills:
        return False
    credential = os.environ.get(args.skill_token_env) if args.skill_token_env else None
    if args.skill_token_env and not credential:
        raise HarnessError(f"${args.skill_token_env} is not set")
    count = load_skills(
        client, corpus.skills, args.skill_base_url, SkillRegistrationRequest, credential
    )
    logger.info(f"Registered {count} skills")
    return True


def _log_summary(
    report: RetrievalReport,
) -> None:
    summary = {
        "categories": {name: summary.model_dump() for name, summary in report.categories.items()},
        "ladder": [step.model_dump() for step in report.ladder],
    }
    logger.info(f"Arm {report.arm} results:\n{json.dumps(summary, indent=2)}")


def main() -> int:
    """Load, search, score and report; return the process exit code."""
    args = _parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    if not args.skip_skills and not args.skip_load and not args.skill_base_url:
        logger.error(
            f"Skills need --skill-base-url (or ${SKILL_BASE_URL_ENV}); pass --skip-skills to run without them"
        )
        return 1
    logger.info(
        f"Configuration:\n{json.dumps({k: str(v) for k, v in vars(args).items()}, indent=2)}"
    )
    started = time.perf_counter()
    try:
        corpus = load_corpus(args.corpus)
        digest, entry = corpus_hash(args.corpus, MANIFEST_FILE)
        if entry is None:
            logger.warning(
                f"{args.corpus} matches no entry in {MANIFEST_FILE}; results will not be comparable"
            )
        from api.registry_client import RegistryClient

        client = RegistryClient(args.registry_url, read_token(args.token_file))
        skills_loaded = (not args.skip_skills) if args.skip_load else _load(client, args, corpus)
        report = run_retrieval(
            client, corpus, args.arm, args.registry_url, digest, skills_loaded, args.max_results
        )
    except (HarnessError, FileNotFoundError) as error:
        logger.error(str(error))
        return 1
    _log_summary(report)
    text = report.model_dump_json(indent=2)
    if args.output is None:
        print(text)
    else:
        args.output.write_text(text + "\n", encoding="utf-8")
        logger.info(f"Wrote {args.output}")
    logger.info(f"Finished in {time.perf_counter() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
