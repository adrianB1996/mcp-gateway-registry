#!/usr/bin/env python3
"""Build the frozen brain-eval corpus from the hand-written domain files.

Reads ``tests/fixtures/brain_eval/domains/*.yaml``, adds the generated tools,
tasks, lookups and collision cases, and writes ``corpus.json`` next to the
domain directory. With ``--scale``, it also writes padded corpora (for example
1,000 and 10,000 tools) and records each file's SHA-256 in ``manifest.json``, so
anyone can check that their padded corpus matches the one the baseline used.

Usage:
    # Rebuild corpus.json and the manifest after editing a domain file
    uv run python scripts/brain_eval/build_corpus.py

    # Also write padded corpora to a scratch directory
    uv run python scripts/brain_eval/build_corpus.py --scale 1000 --scale 10000 \\
        --padded-dir /tmp/brain-eval

    # Check that the committed corpus.json is up to date (exits 1 if not)
    uv run python scripts/brain_eval/build_corpus.py --check
"""

import argparse
import hashlib
import json
import logging
import sys
import time
from pathlib import Path

import yaml

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _SCRIPTS_DIR.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from brain_eval.corpus import (  # noqa: E402
    DEFAULT_SEED,
    Corpus,
    CorpusError,
    DomainFile,
    build_core,
    pad_corpus,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)
logger = logging.getLogger(__name__)

FIXTURE_DIR: Path = _REPO_ROOT / "tests" / "fixtures" / "brain_eval"
DOMAINS_DIR: Path = FIXTURE_DIR / "domains"
CORPUS_FILE: Path = FIXTURE_DIR / "corpus.json"
MANIFEST_FILE: Path = FIXTURE_DIR / "manifest.json"
DEFAULT_SCALES: tuple[int, ...] = (1000, 10000)


def load_domains(
    directory: Path,
) -> list[DomainFile]:
    """Parse every domain file in a directory, in file-name order.

    Raises:
        CorpusError: The directory has no domain files, or one fails to parse.
    """
    paths = sorted(directory.glob("*.yaml"))
    if not paths:
        raise CorpusError(f"No domain files (*.yaml) in {directory}")
    domains: list[DomainFile] = []
    for path in paths:
        try:
            domains.append(
                DomainFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
            )
        except (yaml.YAMLError, ValueError) as error:
            raise CorpusError(f"{path.name}: {error}") from error
    return domains


def corpus_json(
    corpus: Corpus,
) -> str:
    """Serialize a corpus the same way every time, so its hash is stable."""
    return json.dumps(corpus.model_dump(mode="json"), indent=1, sort_keys=True) + "\n"


def _sha256(
    text: str,
) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_manifest(
    core: Corpus,
    scales: tuple[int, ...],
    padded_dir: Path | None,
) -> dict:
    """Pad the corpus to each scale, optionally write it, and return the manifest."""
    manifest: dict = {
        "seed": core.seed,
        "core": {"sha256": _sha256(corpus_json(core)), **core.stats()},
        "scales": {},
    }
    for scale in scales:
        padded = pad_corpus(core, scale)
        text = corpus_json(padded)
        manifest["scales"][str(scale)] = {
            "sha256": _sha256(text),
            "tools": padded.tool_count(),
            "servers": len(padded.servers),
        }
        if padded_dir is not None:
            padded_dir.mkdir(parents=True, exist_ok=True)
            path = padded_dir / f"corpus_{scale}.json"
            path.write_text(text, encoding="utf-8")
            logger.info(f"Wrote {path} ({padded.tool_count()} tools)")
    return manifest


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the frozen brain-eval corpus from the domain files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  uv run python scripts/brain_eval/build_corpus.py
  uv run python scripts/brain_eval/build_corpus.py --scale 1000 --padded-dir /tmp/brain-eval
  uv run python scripts/brain_eval/build_corpus.py --check
""",
    )
    parser.add_argument(
        "--domains", type=Path, default=DOMAINS_DIR, help=f"Domain files (default: {DOMAINS_DIR})"
    )
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED, help=f"Padding seed (default: {DEFAULT_SEED})"
    )
    parser.add_argument(
        "--scale",
        type=int,
        action="append",
        default=None,
        help="Tool count to pad to; repeatable (default: 1000 and 10000)",
    )
    parser.add_argument("--padded-dir", type=Path, default=None, help="Write padded corpora here")
    parser.add_argument(
        "--check", action="store_true", help="Fail if corpus.json or manifest.json is out of date"
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    return parser.parse_args()


def main() -> int:
    """Build or check the corpus and return the process exit code."""
    args = _parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    scales = tuple(args.scale) if args.scale else DEFAULT_SCALES
    started = time.perf_counter()
    try:
        core = build_core(load_domains(args.domains), seed=args.seed)
    except CorpusError as error:
        logger.error(str(error))
        return 1
    logger.info(f"Core corpus:\n{json.dumps(core.stats(), indent=2)}")
    text = corpus_json(core)
    manifest = (
        json.dumps(build_manifest(core, scales, args.padded_dir), indent=2, sort_keys=True) + "\n"
    )

    if args.check:
        stale = [
            path.name
            for path, fresh in ((CORPUS_FILE, text), (MANIFEST_FILE, manifest))
            if not path.exists() or path.read_text(encoding="utf-8") != fresh
        ]
        if stale:
            logger.error(
                f"Out of date: {', '.join(stale)}. Run scripts/brain_eval/build_corpus.py and commit the result."
            )
            return 1
        logger.info("corpus.json and manifest.json are up to date")
        return 0

    CORPUS_FILE.write_text(text, encoding="utf-8")
    MANIFEST_FILE.write_text(manifest, encoding="utf-8")
    logger.info(f"Wrote {CORPUS_FILE} and {MANIFEST_FILE} in {time.perf_counter() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
