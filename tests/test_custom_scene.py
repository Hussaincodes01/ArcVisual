"""The model-authored scene path.

These tests exist because the thing they cover is the product's whole point. With
only three templates implemented, every paper was drawn as a chain, a plot or a
box diagram — so two different papers produced the same three animations with
different labels. A custom scene is the escape from that, and the machinery that
makes it *safe* (Gate 1's allowlist, Gate 2's sandbox, Gate 3's spatial trace) is
only meaningful if generated code actually reaches it.
"""

from __future__ import annotations

import hashlib

import pytest

from arcvisual.gates import g1_static
from arcvisual.generate.custom_codegen import (
    CustomSceneOut,
    build_prompt,
    estimate_duration,
    render_custom_module,
)
from arcvisual.storyboard import Archetype, Beat, SceneSpec, SourceSpan

QUOTE = "The Transformer uses multi-head self-attention."

GOOD_BODY = """
q = m.Rectangle(width=1.1, height=2.2, color=m.BLUE)
k = m.Rectangle(width=2.2, height=1.1, color=m.GREEN)
row = m.VGroup(q, k).arrange(m.RIGHT, buff=0.5)
scene.fit_inside(row, width_frac=0.7)
scene.play(m.FadeIn(row))
scene.hold(1.0)
"""


def _spec(duration: float = 14.0) -> SceneSpec:
    span = SourceSpan(
        section_id="s001",
        start=0,
        end=len(QUOTE),
        quote_sha256=hashlib.sha256(QUOTE.encode()).hexdigest(),
    )
    return SceneSpec(
        id="o001",
        concept_id="c001",
        archetype=Archetype.CUSTOM_SCENE,
        claim="Attention scores are a scaled dot product of queries and keys.",
        span=span,
        params={},
        beats=[Beat(t=0.0, dur=duration, caption="x", kind="reveal")],
        duration_s=duration,
    )


# -- coercion --------------------------------------------------------------- #


def test_a_fenced_body_wrapped_in_def_is_unwrapped_not_rejected() -> None:
    """Models emit markdown fences and a ``def build`` wrapper constantly despite
    instructions. Both are mechanical to undo, and rejecting them would spend a
    repair attempt on a formatting habit rather than on the animation."""
    wrapped = "```python\ndef build(scene, params):\n" + GOOD_BODY.replace("\n", "\n    ")
    out = CustomSceneOut(body=wrapped, captions=["a"])
    assert not out.body.startswith("```")
    assert "def build" not in out.body
    assert out.body.lstrip().startswith("q = m.Rectangle")


def test_captions_are_clamped_per_item() -> None:
    """``clamp_text`` takes a string, so on a list field it silently does nothing —
    and an over-long caption is not harmless: ``scene.caption`` fits it by shrinking,
    so it becomes unreadable and fails Gate 3 rather than simply being trimmed."""
    out = CustomSceneOut(body=GOOD_BODY, captions=["x" * 400, "short"])
    assert len(out.captions[0]) <= 90
    assert out.captions[1] == "short"


# -- the generated module --------------------------------------------------- #


def test_a_generated_module_passes_gate_1() -> None:
    """Gate 1 validates arbitrary source when given ``archetype=None``, which is what
    makes free-form generation safe to attempt at all."""
    out = CustomSceneOut(plan="Two boxes.", body=GOOD_BODY, captions=["one", "two"])
    source = render_custom_module(_spec(), out)
    result, _ = g1_static.run(source, archetype=None)
    assert result.passed, result.findings


def test_latex_survives_into_the_module_byte_exact() -> None:
    """A custom scene's value is showing the paper's OWN notation. If a backslash is
    lost between the model's JSON and the emitted module, the formula silently
    becomes a different formula."""
    latex = (
        'eq = m.MathTex(r"'
        + r"\text{softmax}\left(\frac{QK^{T}}{\sqrt{d_k}}\right)V"
        + '")'
    )
    out = CustomSceneOut(body=GOOD_BODY + "\n" + latex, captions=["x"])
    source = render_custom_module(_spec(), out)
    assert r"\text{softmax}" in source
    assert r"\frac{QK^{T}}{\sqrt{d_k}}" in source


def test_a_body_that_smuggles_an_import_is_caught() -> None:
    """The model writes a function BODY, not a module, so imports are not in reach —
    but if one appears anyway the allowlist must still catch it, or the boundary is
    decorative."""
    body = "import os\nos.system('echo pwned')\nscene.play(m.FadeIn(m.Dot()))"
    out = CustomSceneOut(body=body, captions=["x"])
    source = render_custom_module(_spec(), out)
    result, _ = g1_static.run(source, archetype=None)
    assert not result.passed
    joined = " ".join(result.findings).lower()
    assert "import" in joined or "os" in joined, result.findings


def test_an_empty_body_still_produces_a_parsable_module() -> None:
    """A SyntaxError the repair loop cannot read is worse than a gate finding it can
    act on."""
    out = CustomSceneOut(body="x = 1  # nothing is drawn here at all, deliberately")
    source = render_custom_module(_spec(), out)
    result, _ = g1_static.run(source, archetype=None)
    assert "does not parse" not in " ".join(result.findings)


# -- duration --------------------------------------------------------------- #


def test_duration_is_read_off_the_body_not_the_target() -> None:
    """Gate 2 fails a scene whose measured runtime drifts >20% from its beats.

    Templates answer this with ``estimate_duration(params)``; model-written code has
    no such function, so without reading the body every custom scene inherits the
    opportunity's TARGET duration and trips the check on arrival. One rendered
    perfectly and failed for exactly this: 10.2s measured against 14.0s declared.
    """
    body = (
        "scene.play(m.FadeIn(m.Dot()), run_time=2.0)\n"
        "scene.hold(1.5)\n"
        "scene.play(m.FadeOut(m.Dot()))\n"
    )
    # 2.0 + 1.5 + 1.0 (manim's default run_time)
    assert estimate_duration(body) == pytest.approx(4.5)


def test_the_pacing_floor_is_reflected_in_the_estimate() -> None:
    """``scene.play`` raises anything below MIN_BEAT_S, so an estimate that took the
    model's number at face value would read low for a body of short animations."""
    body = "scene.play(m.FadeIn(m.Dot()), run_time=0.1)\n" * 4
    assert estimate_duration(body) == pytest.approx(0.8 * 4)


def test_an_unparsable_body_declines_to_guess() -> None:
    assert estimate_duration("this is not python (((") == 0.0


# -- the prompt ------------------------------------------------------------- #


def test_the_prompt_forbids_mathtex_when_there_is_no_latex_toolchain() -> None:
    """Without ``latex`` and ``dvisvgm``, MathTex raises before the first animation
    and Gate 2 reports an environment problem no repair can fix. Telling the model up
    front costs nothing; discovering it costs the scene its whole repair budget."""
    system, _ = build_prompt(_spec(), title="T", concept="c", quote=QUOTE, latex=False)
    assert "NO LaTeX toolchain" in system
    assert "Unicode" in system

    system_ok, _ = build_prompt(_spec(), title="T", concept="c", quote=QUOTE, latex=True)
    assert "NO LaTeX toolchain" not in system_ok


def test_equations_reach_the_prompt_verbatim() -> None:
    """The single most valuable thing in the prompt: it is what lets the animation
    show the paper's own notation instead of a paraphrase of it."""
    equation = r"\text{Attention}(Q,K,V)=\text{softmax}(QK^T/\sqrt{d_k})V"
    _, user = build_prompt(
        _spec(),
        title="Attention Is All You Need",
        concept="Scaled dot-product attention",
        quote=QUOTE,
        equations=[equation],
        latex=True,
    )
    assert r"\sqrt{d_k}" in user
    assert QUOTE in user


def test_custom_scene_is_offered_to_the_analyzer() -> None:
    """Registration is the difference between this feature existing and running.

    Unregistered, ``custom_scene`` never appears in the archetype enum the analyzer
    chooses from, every paper is forced into one of three fixed shapes, and none of
    the code above is ever reached.
    """
    from arcvisual.analyze.prompts import _available_archetypes, archetype_help

    assert "custom_scene" in _available_archetypes()
    assert "custom_scene" in archetype_help()


# -- coercions found by running a real model -------------------------------- #


def test_a_mixed_indent_body_is_normalised() -> None:
    """A real model wrote its body as if already inside ``def build``, with a
    comment at column 0 and the code at four spaces.

    ``textwrap.dedent`` takes the longest common prefix over non-blank lines, and a
    comment is a non-blank line — so the prefix was "" and nothing was removed. The
    emitter then added four more spaces to every line and the module failed to parse
    with "unexpected indent". A whole scene lost to whitespace.
    """
    import ast

    body = (
        "# Setup\n"
        "    hw, hh = scene.safe_frame()\n"
        "\n"
        "    # Moment 1\n"
        '    title = m.Text("Attention", font_size=36)\n'
        "    scene.play(m.Write(title))\n"
    )
    out = CustomSceneOut(body=body, captions=["x"])
    ast.parse(out.body)  # raises if the normalisation failed
    assert out.body.startswith("# Setup")


def test_redundant_preamble_is_stripped_but_forbidden_imports_are_not() -> None:
    """Models reliably restate the preamble they were told not to write.

    Observed on consecutive real replies: ``import math``, shadowing the module-level
    import (F811 plus F401 for the now-unused original), then ``m = manim_mod()``,
    shadowing the binding ``build`` opens with (F811 again). Neither says anything
    about the animation.

    The safety half matters more than the convenience half: only what we actually
    provide is stripped, so a forbidden import is still there for Gate 1 to refuse.
    Laundering ``import os`` into a passing scene would make the whole allowlist
    decorative.
    """
    body = (
        "import math\n"
        "m = manim_mod()\n"
        "import os\n"
        "ys = [math.exp(-x) for x in [0.0, 1.0]]\n"
        "scene.play(m.FadeIn(m.Dot()))\n"
    )
    out = CustomSceneOut(body=body, captions=["x"])
    assert "import math" not in out.body
    assert "manim_mod()" not in out.body
    assert "import os" in out.body, "a forbidden import must survive to reach Gate 1"

    source = render_custom_module(_spec(), out)
    result, _ = g1_static.run(source, archetype=None)
    assert not result.passed, "Gate 1 must still refuse the smuggled import"


def test_math_is_available_to_a_generated_body() -> None:
    """Manim is a drawing library with no maths functions: a real scene plotting a
    softmax curve reached for ``m.exp`` and raised at render time, after four
    animations had already been drawn. The emitted module imports ``math`` so the
    body has somewhere legitimate to get it."""
    out = CustomSceneOut(
        body="ys = [math.exp(-x) for x in [0.0, 1.0]]\nscene.play(m.FadeIn(m.Dot(ys)))",
        captions=["x"],
    )
    source = render_custom_module(_spec(), out)
    assert "import math" in source
    result, _ = g1_static.run(source, archetype=None)
    assert result.passed, result.findings


def test_the_prompt_spells_out_the_m_prefix_on_constants() -> None:
    """A real model wrote bare ``ORIGIN`` and Gate 1 refused the scene for an
    undefined name. The surface listed constants without their prefix, which invited
    exactly that — the most common way a generated body fails."""
    system, _ = build_prompt(_spec(), title="T", concept="c", quote=QUOTE, latex=True)
    assert "m.ORIGIN" in system
    assert "math.exp" in system, "the maths surface must be advertised too"
