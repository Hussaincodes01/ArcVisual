"""Gate 1 — syntactic and contract checks. Static, ~50ms, no render.

The cheapest gate and the highest-leverage one. Order inside it is also
cheapest-first: parse, then the security walk, then the allowlist, then ruff
(which costs a subprocess), and parameter validation is folded in because a
hallucinated parameter name should fail here rather than surface as a confusing
``TypeError`` deep inside a template.

Findings are written as instructions to the model, not as tracebacks, because
they are fed verbatim into the repair prompt. "``manim.FadeInFrom`` does not
exist in Manim 0.18.1" is actionable; a ``AttributeError`` line number is not.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arcvisual.gates.allowlist import (
    FORBIDDEN_ATTRS,
    FORBIDDEN_CALLS,
    FORBIDDEN_IMPORTS,
    is_manim_name,
)
from arcvisual.storyboard import Archetype, GateResult
from arcvisual.templates import registry

#: Lint codes that cannot make a render wrong, so they never cost an attempt.
_HARMLESS_LINT = frozenset({"F401", "F841"})


@dataclass
class StaticFindings:
    security: list[str] = field(default_factory=list)
    api: list[str] = field(default_factory=list)
    params: list[str] = field(default_factory=list)
    lint: list[str] = field(default_factory=list)

    def all(self) -> list[str]:
        return [*self.security, *self.api, *self.params, *self.lint]

    @property
    def blocking(self) -> bool:
        """Security, API and parameter findings always block.

        Lint blocks selectively. Only ``F``/``E9`` rules are collected at all
        (style opinions cannot make a render wrong, and spending a repair attempt
        on import ordering spends money for nothing), but within those, an
        undefined name will crash the render and must block. The two harmless
        exceptions are unused imports and unused variables.
        """
        return bool(
            self.security
            or self.api
            or self.params
            or [f for f in self.lint if not _HARMLESS_LINT.intersection(f.split())]
        )


def run(
    source: str,
    archetype: Archetype | None = None,
    raw_params: dict[str, Any] | None = None,
    *,
    run_ruff: bool = True,
) -> tuple[GateResult, Any | None]:
    """Check generated module source. Returns ``(result, validated_params)``."""
    t0 = time.perf_counter()
    findings = StaticFindings()
    params_obj = None

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return (
            GateResult(
                gate=1,
                passed=False,
                duration_ms=int((time.perf_counter() - t0) * 1000),
                findings=[f"the module does not parse: {exc.msg} at line {exc.lineno}"],
                payload={"stage": "parse", "lineno": exc.lineno},
            ),
            None,
        )

    visitor = _Walker()
    visitor.visit(tree)
    findings.security.extend(visitor.security)
    findings.api.extend(visitor.api)

    if archetype is not None:
        params_obj, param_findings = registry.validate_or_error(
            archetype, raw_params or {}
        )
        findings.params.extend(param_findings)

    if run_ruff:
        findings.lint.extend(_ruff(source))

    duration = int((time.perf_counter() - t0) * 1000)
    return (
        GateResult(
            gate=1,
            passed=not findings.blocking,
            duration_ms=duration,
            findings=findings.all(),
            payload={
                "security": findings.security,
                "api": findings.api,
                "params": findings.params,
                "lint": findings.lint,
            },
        ),
        params_obj,
    )


class _Walker(ast.NodeVisitor):
    """One pass collecting security and API-surface findings."""

    def __init__(self) -> None:
        self.security: list[str] = []
        self.api: list[str] = []
        #: Names bound to the manim module, e.g. ``import manim as m``.
        self._manim_aliases: set[str] = {"manim"}
        #: Names imported directly from manim, e.g. ``from manim import Text``.
        self._from_manim: set[str] = set()

    # -- imports ----------------------------------------------------------- #

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            if reason := FORBIDDEN_IMPORTS.get(root):
                self.security.append(f"import of {alias.name!r} is refused: {reason}")
            elif alias.name == "manim":
                self._manim_aliases.add(alias.asname or "manim")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        root = module.split(".")[0]
        if reason := FORBIDDEN_IMPORTS.get(root):
            self.security.append(f"import from {module!r} is refused: {reason}")
        elif module == "manim":
            for alias in node.names:
                if alias.name == "*":
                    self.api.append(
                        "`from manim import *` is not permitted; import the names "
                        "you use explicitly so they can be checked"
                    )
                    continue
                if not is_manim_name(alias.name):
                    self.api.append(
                        f"manim has no name {alias.name!r} in the pinned version"
                    )
                self._from_manim.add(alias.asname or alias.name)
        self.generic_visit(node)

    # -- calls and attributes ---------------------------------------------- #

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name) and (reason := FORBIDDEN_CALLS.get(func.id)):
            self.security.append(f"call to {func.id}() is refused: {reason}")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if reason := FORBIDDEN_ATTRS.get(node.attr):
            self.security.append(f"access to {node.attr!r} is refused: {reason}")
        # manim.Foo -> Foo must exist in the pinned surface.
        if (
            isinstance(node.value, ast.Name)
            and node.value.id in self._manim_aliases
            and not is_manim_name(node.attr)
        ):
            self.api.append(
                f"manim.{node.attr} does not exist in the pinned Manim "
                "version; use a documented class or animation"
            )
        self.generic_visit(node)

    # -- unbounded work ---------------------------------------------------- #

    def visit_While(self, node: ast.While) -> None:
        if isinstance(node.test, ast.Constant) and node.test.value:
            self.security.append(
                "`while True` is refused: a scene must have a bounded runtime"
            )
        else:
            self.security.append(
                "`while` loops are refused in a scene; iterate over a bounded "
                "sequence with `for` instead"
            )
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # Direct self-recursion is the cheap case to catch and the common one.
        for child in ast.walk(node):
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == node.name
            ):
                self.security.append(
                    f"function {node.name!r} calls itself; unbounded recursion is "
                    "refused in a scene"
                )
                break
        self.generic_visit(node)


def _ruff(source: str) -> list[str]:
    """Run ruff, selecting only ``F`` (pyflakes) and ``E9`` (syntax) rules."""
    ruff = _ruff_binary()
    if ruff is None:  # pragma: no cover - depends on environment
        return []
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "scene.py"
        path.write_text(source, encoding="utf-8")
        try:
            proc = subprocess.run(
                [
                    *ruff,
                    "check",
                    "--select",
                    "F,E9",
                    "--output-format",
                    "json",
                    "--no-cache",
                    "--isolated",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (subprocess.TimeoutExpired, OSError):  # pragma: no cover
            return []
    try:
        issues = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:  # pragma: no cover
        return []
    return [
        f"ruff {i.get('code', '?')} line {(i.get('location') or {}).get('row', '?')}: "
        f"{i.get('message', '')}"
        for i in issues
    ]


def _ruff_binary() -> list[str] | None:
    import shutil

    if found := shutil.which("ruff"):
        return [found]
    try:  # ruff installed as a library dependency
        import ruff  # noqa: F401

        return [sys.executable, "-m", "ruff"]
    except ImportError:
        return None
