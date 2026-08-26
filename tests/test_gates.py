"""Gate 1 must catch every failure mode it claims to, and pass clean code.

The allowlist is both a quality check and a security control, so the security cases
here are not optional coverage — a regression that lets `import socket` through
means we execute network-capable model-authored code.
"""

from __future__ import annotations

import pytest

from arcvisual.gates.allowlist import is_manim_name, manim_surface
from arcvisual.gates.g1_static import run
from arcvisual.gates.g2_runtime import scrape_log
from arcvisual.storyboard import Archetype

CLEAN = """
import manim as m
from manim import Text, FadeIn, TransformMatchingTex
from arcvisual.templates.transform_chain import Params, build
from arcvisual.templates.base import make_scene

PARAMS = Params(steps=["a=b", "b=c"], captions=["one", "two"])
Scene_x = make_scene("x", "transform_chain", build, PARAMS)
"""


def test_clean_module_passes() -> None:
    result, _ = run(CLEAN)
    assert result.passed, result.findings


def test_allowlist_is_populated() -> None:
    assert len(manim_surface()) > 200
    assert is_manim_name("TransformMatchingTex")
    assert is_manim_name("Axes")
    assert not is_manim_name("FadeInFrom")  # removed from Manim CE


# -- API surface ------------------------------------------------------------ #


def test_hallucinated_attribute_is_caught() -> None:
    result, _ = run("import manim as m\nx = m.FadeInFrom\n")
    assert not result.passed
    assert any("FadeInFrom" in f for f in result.findings)


def test_hallucinated_from_import_is_caught() -> None:
    result, _ = run("from manim import ShowCreationThenDestruction\n")
    assert not result.passed
    assert any("ShowCreationThenDestruction" in f for f in result.findings)


def test_star_import_is_refused() -> None:
    """A star import makes the allowlist unenforceable, so it cannot be allowed."""
    result, _ = run("from manim import *\n")
    assert not result.passed
    assert any("import the names you use explicitly" in f for f in result.findings)


# -- security --------------------------------------------------------------- #


@pytest.mark.parametrize(
    "source,needle",
    [
        ("import socket\n", "network access"),
        ("import requests\n", "network access"),
        ("import subprocess\n", "spawning processes"),
        ("import os\n", "filesystem and environment"),
        ("from pathlib import Path\n", "filesystem access"),
        ("import importlib\n", "dynamic imports"),
        ("x = open('/etc/passwd')\n", "file access"),
        ("exec('print(1)')\n", "dynamic execution"),
        ("eval('1+1')\n", "dynamic evaluation"),
        ("x = ().__class__.__bases__\n", "class-hierarchy"),
        ("import manim as m\nx = m.Scene.__subclasses__()\n", "class-hierarchy escape"),
    ],
)
def test_forbidden_constructs_are_refused(source: str, needle: str) -> None:
    result, _ = run(source)
    assert not result.passed, source
    assert any(needle in f for f in result.findings), result.findings


def test_unbounded_loop_is_refused() -> None:
    result, _ = run("while True:\n    pass\n")
    assert not result.passed
    assert any("bounded runtime" in f for f in result.findings)


def test_self_recursion_is_refused() -> None:
    result, _ = run("def f(n):\n    return f(n + 1)\n")
    assert not result.passed
    assert any("calls itself" in f for f in result.findings)


# -- lint triage ------------------------------------------------------------ #


def test_undefined_name_blocks() -> None:
    result, _ = run("import manim as m\nx = m.Create(missing_thing)\n")
    assert not result.passed
    assert any("F821" in f for f in result.findings)


def test_unused_import_does_not_block() -> None:
    """An unused import cannot make a render wrong, so it must not cost an
    attempt from the repair budget."""
    result, _ = run("import manim as m\n")
    assert result.passed
    assert any("F401" in f for f in result.findings)  # reported, not blocking


def test_syntax_error_short_circuits() -> None:
    result, _ = run("def f(:\n  pass")
    assert not result.passed
    assert result.duration_ms < 50  # no ruff subprocess was spawned
    assert "does not parse" in result.findings[0]


# -- parameter validation --------------------------------------------------- #


def test_parameter_findings_are_phrased_as_instructions() -> None:
    result, params = run(
        "import manim as m\n", Archetype.TRANSFORM_CHAIN, {"steps": ["only-one"]}
    )
    assert not result.passed
    assert params is None
    assert any(f.startswith("parameter steps") for f in result.findings)


def test_hallucinated_parameter_name_is_caught() -> None:
    """extra='forbid' on TemplateParams: a wrong name must fail loudly rather
    than be silently ignored and produce a subtly wrong animation."""
    result, _ = run(
        "import manim as m\n",
        Archetype.TRANSFORM_CHAIN,
        {"steps": ["a=b", "b=c"], "titel": "typo"},
    )
    assert not result.passed
    assert any("titel" in f for f in result.findings)


def test_unimplemented_archetype_is_caught() -> None:
    result, _ = run("import manim as m\n", Archetype.VECTOR_FIELD, {})
    assert not result.passed
    assert any("no template in this build" in f for f in result.findings)


# -- Gate 2 log scraping ---------------------------------------------------- #


def test_latex_errors_are_surfaced() -> None:
    findings = scrape_log("! LaTeX Error: File `foo.sty' not found.\n")
    assert any("LaTeX failed to compile" in f for f in findings)


def test_missing_math_mode_is_surfaced() -> None:
    findings = scrape_log("! Missing $ inserted.\n")
    assert any("math-mode" in f for f in findings)


def test_clean_log_yields_no_findings() -> None:
    assert scrape_log("Rendered SomeScene\nFile ready at scene.mp4\n") == []
