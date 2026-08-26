"""Content addressing. These tests exist because the plan's hash tuple was wrong.

The plan hashed ``(template_id, params, manim_version, plugin_versions)``.
``template_id`` is a name, not content: fix a layout bug inside
``transform_chain.py`` and every previously rendered scene of that archetype keeps
serving the buggy video forever, because its hash never moved.
:func:`test_template_edit_invalidates_the_hash` is what stops that from coming back.
"""

from __future__ import annotations

from arcvisual.cache.hashing import (
    RenderEnv,
    canonical,
    canonical_json,
    content_hash,
    r2_keys,
)
from arcvisual.storyboard import Archetype, Beat, SceneSpec, SourceSpan, quote_hash
from arcvisual.templates import registry


def _spec(**overrides) -> SceneSpec:
    base = dict(
        id="o000",
        archetype=Archetype.TRANSFORM_CHAIN,
        claim="a claim long enough to pass validation",
        concept_id="c000",
        span=SourceSpan(
            section_id="s000", start=0, end=6, quote_sha256=quote_hash("hello!")
        ),
        params={"steps": ["a=b", "b=c"], "captions": ["one", "two"]},
        beats=(Beat(t=0.0, dur=2.0, caption="one"), Beat(t=2.0, dur=2.0, caption="two")),
    )
    base.update(overrides)
    return SceneSpec(**base)


def _env(**overrides) -> RenderEnv:
    base = dict(
        manim_version="0.18.1",
        plugin_lock_sha="abc123",
        template_source_sha="tpl-sha-1",
        arcscene_base_sha="base-sha-1",
        quality="final",
    )
    base.update(overrides)
    return RenderEnv(**base)


# -- canonicalisation ------------------------------------------------------- #


def test_key_order_does_not_change_the_hash() -> None:
    """Otherwise the cache silently loses much of its value."""
    a = canonical_json({"n": 2, "color": "#FFF"})
    b = canonical_json({"color": "#FFF", "n": 2})
    assert a == b


def test_integral_float_and_int_agree() -> None:
    assert canonical(2.0) == canonical(2)
    assert canonical_json({"n": 2.0}) == canonical_json({"n": 2})


def test_floats_are_rounded_consistently() -> None:
    assert canonical(1.0000001) == canonical(1.00000009)


def test_bools_are_not_treated_as_ints() -> None:
    """bool is an int subclass; conflating them would make True == 1 in a hash."""
    assert canonical(True) is True
    assert canonical_json({"x": True}) != canonical_json({"x": 1})


# -- the hash tuple --------------------------------------------------------- #


def test_same_inputs_give_the_same_hash() -> None:
    assert content_hash(_spec(), _env()) == content_hash(_spec(), _env())


def test_param_change_invalidates() -> None:
    other = _spec(params={"steps": ["a=b", "b=d"], "captions": ["one", "two"]})
    assert content_hash(_spec(), _env()) != content_hash(other, _env())


def test_beat_change_invalidates() -> None:
    other = _spec(
        beats=(Beat(t=0.0, dur=3.0, caption="one"), Beat(t=3.0, dur=2.0, caption="two"))
    )
    assert content_hash(_spec(), _env()) != content_hash(other, _env())


def test_template_edit_invalidates_the_hash() -> None:
    """THE regression test for the plan's hash bug. If this ever passes with the
    two hashes equal, edited templates will serve stale video indefinitely."""
    before = content_hash(_spec(), _env(template_source_sha="tpl-sha-1"))
    after = content_hash(_spec(), _env(template_source_sha="tpl-sha-2"))
    assert before != after


def test_base_class_edit_invalidates_the_hash() -> None:
    """ArcSceneMixin owns pacing, margins and instrumentation, so editing it
    changes every rendered scene."""
    before = content_hash(_spec(), _env(arcscene_base_sha="base-sha-1"))
    after = content_hash(_spec(), _env(arcscene_base_sha="base-sha-2"))
    assert before != after


def test_quality_is_part_of_the_key() -> None:
    draft = content_hash(_spec(), _env(quality="draft"))
    final = content_hash(_spec(), _env(quality="final"))
    assert draft != final


def test_manim_version_is_part_of_the_key() -> None:
    assert content_hash(_spec(), _env(manim_version="0.18.1")) != content_hash(
        _spec(), _env(manim_version="0.19.0")
    )


def test_field_boundaries_cannot_collide() -> None:
    """A unit separator between fields stops ("ab","c") hashing like ("a","bc")."""
    from arcvisual.cache.hashing import sha256_of

    assert sha256_of("ab", "c") != sha256_of("a", "bc")


# -- real templates --------------------------------------------------------- #


def test_render_env_reads_real_template_source() -> None:
    env = RenderEnv.for_template(registry.get(Archetype.PLOT_REVEAL).module)
    assert len(env.template_source_sha) == 16
    assert len(env.arcscene_base_sha) == 16
    other = RenderEnv.for_template(registry.get(Archetype.TRANSFORM_CHAIN).module)
    assert env.template_source_sha != other.template_source_sha


def test_r2_keys_are_sharded_and_consistent() -> None:
    keys = r2_keys("abcdef" + "0" * 58)
    assert keys["mp4_key"].startswith("scenes/ab/cd/")
    assert all(("abcdef" + "0" * 58) in v for v in keys.values())
    assert len(set(keys.values())) == 4
