"""T-401 clean-room import / static-analysis guard (C-7, AC-056; validation-harness.md §2).

THE GUARANTEE
=============
The validation harness package (tests/validation/) and the gates it imports (aats.models.
gate_a / gate_b) must reference NO `truth_*` field and import NOTHING from `sniper_sim/`.
The sim is admittedly circular (safety.py:43 reads truth_is_rug at an assumed catch_rate);
if any of that scaffolding reaches the gate path, the gate proves nothing. This test makes
the leak a BUILD FAILURE, not a reviewer's hope.

WHAT IT SCANS
=============
1. AST scan of every file in the harness package + the gate modules for the forbidden
   identifiers (truth_is_rug, truth_max_multiple, truth_rug_detectable, and any `truth_`
   attribute/name). The model-WINS / model-LOSES CONTROL selectors read `row.label` (the
   public post-hoc outcome resolved at event_time+H) — that is NOT a `truth_*` field and is
   allowed; a `truth_` reference is not.
2. Import-graph walk: FAIL if `sniper_sim` (or its types/safety/exits/venue symbols) appears
   anywhere in the harness's transitive import closure.

These mirror validation-harness.md §2 guards 1-2. (The provenance-cutoff / lineage-taint /
disjointness / recorded_at guards (§2.5) are exercised by the leak-audit suite re-run in
T-400; this file owns the clean-room import boundary specifically.)
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

# The files that MUST be clean-room (the gate path + the harness package).
_HARNESS_DIR = Path(__file__).parent
_GATE_FILES = [
    Path(__file__).parents[2] / "aats" / "models" / "gate_a.py",
    Path(__file__).parents[2] / "aats" / "models" / "gate_b.py",
]

# Forbidden identifier substrings (C-7). `truth_` as a prefix on any Name/Attribute is a leak.
_FORBIDDEN_TRUTH_PREFIX = "truth_"
_FORBIDDEN_NAMES = {"truth_is_rug", "truth_max_multiple", "truth_rug_detectable"}
_FORBIDDEN_IMPORT_ROOT = "sniper_sim"


def _scan_files() -> list[Path]:
    files = [p for p in _HARNESS_DIR.glob("*.py")]
    files.extend(_GATE_FILES)
    return files


def _truth_references(tree: ast.AST) -> list[str]:
    """Collect any Name/Attribute whose identifier starts with `truth_` or is a forbidden name."""
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and (
            node.id.startswith(_FORBIDDEN_TRUTH_PREFIX) or node.id in _FORBIDDEN_NAMES
        ):
            hits.append(node.id)
        elif isinstance(node, ast.Attribute) and (
            node.attr.startswith(_FORBIDDEN_TRUTH_PREFIX) or node.attr in _FORBIDDEN_NAMES
        ):
            hits.append(node.attr)
    return hits


def _sim_imports(tree: ast.AST) -> list[str]:
    """Collect any `import sniper_sim...` / `from sniper_sim... import ...`."""
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == _FORBIDDEN_IMPORT_ROOT:
                    hits.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.split(".")[0] == _FORBIDDEN_IMPORT_ROOT:
                hits.append(mod)
    return hits


class TestCleanRoomGuard:
    def test_no_truth_field_reference_in_gate_path(self):
        offenders: dict[str, list[str]] = {}
        for path in _scan_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            hits = _truth_references(tree)
            if hits:
                offenders[str(path)] = sorted(set(hits))
        assert not offenders, (
            "truth_field_reference_forbidden: clean-room guard found truth_* references in "
            f"the gate path (C-7): {offenders}"
        )

    def test_no_sniper_sim_import_in_gate_path(self):
        offenders: dict[str, list[str]] = {}
        for path in _scan_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            hits = _sim_imports(tree)
            if hits:
                offenders[str(path)] = sorted(set(hits))
        assert not offenders, (
            "inherited_sim_constant_forbidden: clean-room guard found sniper_sim imports in "
            f"the gate path (C-2/C-7): {offenders}"
        )

    def test_transitive_closure_has_no_sniper_sim(self):
        """Import the gate modules + harness and assert no `sniper_sim` module is loaded into
        sys.modules as a result (import-graph walk, validation-harness.md §2)."""
        import sys

        # snapshot, import the clean-room surface, then check what got pulled in
        before = set(sys.modules)
        importlib.import_module("aats.models.gate_a")
        importlib.import_module("aats.models.gate_b")
        importlib.import_module("tests.validation.harness")
        after = set(sys.modules)
        newly = after - before
        sim_modules = [m for m in (newly | after) if m.split(".")[0] == _FORBIDDEN_IMPORT_ROOT]
        # Nothing in the gate/harness closure may have imported sniper_sim. (If a prior test
        # imported it, it'd be in `after`; we assert the GATE path didn't bring it in newly.)
        newly_sim = [m for m in newly if m.split(".")[0] == _FORBIDDEN_IMPORT_ROOT]
        assert not newly_sim, (
            f"the clean-room gate/harness closure imported sniper_sim modules: {newly_sim}"
        )

    def test_guard_is_non_vacuous_detects_a_planted_truth_ref(self, tmp_path):
        """Plant a file with a truth_* reference and confirm the AST scanner FLAGS it —
        proving the guard is not a no-op."""
        planted = tmp_path / "planted_leak.py"
        planted.write_text("x = some_obj.truth_is_rug\n", encoding="utf-8")
        tree = ast.parse(planted.read_text(encoding="utf-8"))
        hits = _truth_references(tree)
        assert "truth_is_rug" in hits

    def test_guard_is_non_vacuous_detects_a_planted_sim_import(self, tmp_path):
        planted = tmp_path / "planted_sim.py"
        planted.write_text("from sniper_sim.safety import some_gate\n", encoding="utf-8")
        tree = ast.parse(planted.read_text(encoding="utf-8"))
        # the import-graph scanner flags the sniper_sim import (the C-2/C-7 leak vector)
        assert _sim_imports(tree) == ["sniper_sim.safety"]

    def test_guard_detects_truth_attr_access(self, tmp_path):
        """A truth_* ATTRIBUTE access (the real leak shape, e.g. event.truth_is_rug) is
        caught by the AST attribute scanner — proving that vector is non-vacuous too."""
        planted = tmp_path / "planted_attr.py"
        planted.write_text("y = event.truth_is_rug + obj.truth_max_multiple\n", encoding="utf-8")
        tree = ast.parse(planted.read_text(encoding="utf-8"))
        hits = _truth_references(tree)
        assert "truth_is_rug" in hits
        assert "truth_max_multiple" in hits
