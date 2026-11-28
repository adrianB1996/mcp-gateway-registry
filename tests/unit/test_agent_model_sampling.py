"""Unit tests for how agents/agent.py configures sampling per model."""

import importlib.util
import sys
from pathlib import Path

import pytest

# agents/ is a standalone script directory; see test_safe_eval_arithmetic.py for
# why registry_client is pre-loaded from agents/.
_AGENTS_DIR = str(Path(__file__).resolve().parents[2] / "agents")
if _AGENTS_DIR not in sys.path:
    sys.path.insert(0, _AGENTS_DIR)
if "registry_client" not in sys.modules:
    _path = Path(__file__).resolve().parents[2] / "agents" / "registry_client.py"
    _spec = importlib.util.spec_from_file_location("registry_client", _path)
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["registry_client"] = _module
    _spec.loader.exec_module(_module)

from agents import agent  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "model_id",
    [
        "claude-opus-5-5",
        "claude-fable-5-2",
        "claude-sonnet-5",
        "claude-opus-4-7",
        "claude-opus-4-8",
        "us.anthropic.claude-opus-4-7-20260301-v1:0",
        "anthropic.claude-opus-5-5",
    ],
)
def test_newer_claude_models_reject_sampling(model_id):
    assert agent._accepts_sampling(model_id) is False


@pytest.mark.parametrize(
    "model_id",
    [
        "us.anthropic.claude-3-7-sonnet-20250219-v1:0",
        "claude-sonnet-4-6",
        "claude-opus-4-6",
        "claude-haiku-4-5",
        "claude-sonnet-4-5-20250929",
    ],
)
def test_older_claude_models_keep_temperature(model_id):
    assert agent._accepts_sampling(model_id) is True


def test_anthropic_model_drops_temperature_and_sets_effort_for_opus_5_5(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "YOUR_TEST_KEY")

    model = agent._create_model("anthropic", "claude-opus-5-5")

    assert model.temperature is None
    assert model.effort == "high"


def test_anthropic_model_keeps_temperature_zero_for_older_models(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "YOUR_TEST_KEY")

    model = agent._create_model("anthropic", "claude-sonnet-4-6")

    assert model.temperature == 0.0
    assert model.effort is None


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        (None, "http://127.0.0.1:11434"),
        ("", "http://127.0.0.1:11434"),
        ("0.0.0.0:11434", "http://0.0.0.0:11434"),
        ("http://gpu-box:11434/", "http://gpu-box:11434"),
    ],
)
def test_ollama_base_url_reads_ollama_host(monkeypatch, env_value, expected):
    if env_value is None:
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
    else:
        monkeypatch.setenv("OLLAMA_HOST", env_value)

    assert agent.ollama_base_url() == expected


def test_ollama_model_runs_at_temperature_zero_with_fixed_context(monkeypatch):
    pytest.importorskip("langchain_ollama")
    monkeypatch.delenv("OLLAMA_HOST", raising=False)

    model = agent._create_model("ollama", "qwen3:8b")

    assert model.model == "qwen3:8b"
    assert model.temperature == 0.0
    assert model.seed == 0
    assert model.num_ctx == 8192
    assert model.reasoning is False
    assert model.base_url == "http://127.0.0.1:11434"
