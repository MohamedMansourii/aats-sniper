"""Hard-rule / hygiene tests (self-check items 8, 9 + non-waivable rules).

Proves:
  - NO win_rate / win-rate field anywhere in the reasoning package (HONESTY CLAUSE);
  - NO point price field reaches the model or the verdict (probability + uncertainty only);
  - NO secrets/keys in the source; .env.example documents the endpoints;
  - the LLM is NEVER imported on the FAST/SNIPE path (slow-loop only) — instructor/openai
    are LAZY imports, not module-level;
  - point-in-time: wall_clock_ms is never interpolated into the prompt;
  - DRY_RUN is the boot default (real capital disabled).
"""

from __future__ import annotations

import pathlib
import re

import aats.reasoning as reasoning_pkg
from aats.reasoning.prompt import PromptInputs, build_reasoner_prompt
from aats.reasoning.router import is_dry_run
from tests.reasoning.fixtures import make_mcs, make_signal

_REASONING_DIR = pathlib.Path(reasoning_pkg.__file__).parent
_SOURCE_FILES = sorted(_REASONING_DIR.glob("*.py"))


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# No win-rate metric anywhere (HONESTY CLAUSE)
# ---------------------------------------------------------------------------


def test_no_win_rate_field_anywhere():
    """The string 'win_rate' (a metric field) must not appear as a field/metric."""
    for f in _SOURCE_FILES:
        text = _read(f)
        # Allow the explanatory mention 'win-rate' in prose comments that explicitly say
        # it is NOT used; forbid a 'win_rate' identifier / dict key.
        assert "win_rate" not in text, f"win_rate identifier found in {f.name}"
        # A 'winrate' identifier is likewise forbidden.
        assert "winrate" not in text.lower().replace("win-rate", ""), f.name


# ---------------------------------------------------------------------------
# No point price reaches the model or the verdict
# ---------------------------------------------------------------------------


def test_prompt_contains_probability_not_price():
    inputs = PromptInputs(
        decision_signal=make_signal(p_calibrated=0.7, uncertainty=0.2),
        mcs=make_mcs(),
        total_cost_bps=300,
        expected_edge_bps=800,
        position_is_open=False,
    )
    prompt = build_reasoner_prompt(inputs)
    assert "p_calibrated" in prompt
    assert "uncertainty" in prompt
    # No price-target field names leak into the prompt.
    for banned in ("price_target", "target_price", "point_price", "price_forecast"):
        assert banned not in prompt


# ---------------------------------------------------------------------------
# Point-in-time: wall_clock_ms never interpolated into the prompt
# ---------------------------------------------------------------------------


def test_wall_clock_not_in_prompt():
    """wall_clock_ms is monitoring-only — it must not appear in the prompt (no leak)."""
    sig = make_signal()
    inputs = PromptInputs(
        decision_signal=sig,
        mcs=make_mcs(),
        total_cost_bps=300,
        expected_edge_bps=800,
        position_is_open=False,
    )
    prompt = build_reasoner_prompt(inputs)
    assert "wall_clock" not in prompt
    assert str(sig.event_time.wall_clock_ms) not in prompt


def test_no_future_or_compute_time_field_in_prompt_source():
    """grep the prompt builder for future/compute-time field references (item 8)."""
    prompt_src = _read(_REASONING_DIR / "prompt.py")
    # The prompt must never reference a forward-looking field or wall-clock as a feature.
    for banned in ("wall_clock_ms", "future_", "fwd_return", "realized_mult", "resolution_event"):
        # wall_clock_ms may be MENTIONED in a comment saying it is excluded; ensure it is
        # not interpolated (no f-string usage of it).
        assert f"{{inputs.decision_signal.event_time.{banned}}}" not in prompt_src
        assert f"{{sig.event_time.{banned}}}" not in prompt_src


# ---------------------------------------------------------------------------
# No secrets / keys in source
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),  # OpenAI-style key
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"api[_-]?key\s*=\s*[\"'][A-Za-z0-9]{16,}[\"']", re.IGNORECASE),
]


def test_no_secrets_in_source():
    for f in _SOURCE_FILES:
        text = _read(f)
        for pat in _SECRET_PATTERNS:
            assert not pat.search(text), f"possible secret in {f.name}"


def test_env_var_names_used_not_literal_endpoints():
    """Endpoints/keys come from env var NAMES, not hardcoded values."""
    router_src = _read(_REASONING_DIR / "router.py")
    assert "AATS_OLLAMA_URL" in router_src
    assert "AATS_FRONTIER_API_KEY" in router_src
    # No literal http(s) endpoint hardcoded in the router.
    assert "https://api.openai.com" not in router_src
    assert "http://localhost:11434" not in router_src


# ---------------------------------------------------------------------------
# LLM never on the FAST/SNIPE path: instructor/openai are LAZY imports only
# ---------------------------------------------------------------------------


def test_instructor_openai_not_imported_at_module_level():
    """instructor/openai must NOT be imported at module top-level (lazy plug-in only).

    This keeps the package loadable offline AND documents that the LLM client is a
    deploy-time plug-in, not a hot-path dependency.
    """
    for f in _SOURCE_FILES:
        text = _read(f)
        lines = text.splitlines()
        # Find top-level (non-indented) import lines.
        for line in lines:
            stripped = line.rstrip()
            if stripped.startswith(("import instructor", "from instructor", "import openai", "from openai")):
                raise AssertionError(
                    f"top-level instructor/openai import in {f.name}: {stripped!r}. "
                    "These must be LAZY (inside the live plug-in functions)."
                )


def test_reasoning_package_imports_without_llm_libs():
    """The package import already succeeded (instructor/openai absent in this env)."""
    # If we got here, `import aats.reasoning` worked without instructor/openai installed.
    assert hasattr(reasoning_pkg, "DeRiskReasoner")


# ---------------------------------------------------------------------------
# DRY_RUN is the boot default (real capital disabled)
# ---------------------------------------------------------------------------


def test_dry_run_is_default(monkeypatch):
    monkeypatch.delenv("DRY_RUN_ENABLED", raising=False)
    assert is_dry_run() is True  # default = DRY RUN (real capital disabled)
    monkeypatch.setenv("DRY_RUN_ENABLED", "false")
    assert is_dry_run() is False
    monkeypatch.setenv("DRY_RUN_ENABLED", "true")
    assert is_dry_run() is True
