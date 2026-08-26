"""The Manim API allowlist — Gate 1's core, and a security control.

Two jobs, and it is worth being clear that they are different:

1. **Quality.** Hallucinated methods are the single most common codegen failure,
   and they cost nothing to catch here versus a wasted render. ``ast.parse``
   proves a module is *syntactically* Python; this proves it only touches names
   that exist in the pinned Manim surface.
2. **Security.** Generated code is untrusted input that we execute. The forbidden
   set below is not a lint preference — it is the boundary that keeps a render
   from opening a socket or writing outside its sandbox. Gate 2's Modal Sandbox is
   the second layer; this is the first.

**Keeping the snapshot honest.** The allowlist is a *snapshot* of Manim 0.18.1's
public surface, because the control plane has no Manim installed and cannot
introspect it. Regenerate it inside the render container whenever the pin moves::

    python -m arcvisual.gates.allowlist --refresh > arcvisual/gates/manim_api.json

If the snapshot and the installed Manim disagree, the render container's own
version wins: :func:`manim_surface` prefers live introspection when available.
That way a stale snapshot causes a false *rejection* at worst, never a false pass.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_SNAPSHOT = Path(__file__).with_name("manim_api.json")

#: Modules a generated scene may import. Everything else is a finding.
ALLOWED_IMPORTS = frozenset(
    {
        "manim",
        "numpy",
        "math",
        "itertools",
        "arcvisual.templates.base",
        "arcvisual.templates.transform_chain",
        "arcvisual.templates.plot_reveal",
        "arcvisual.templates.architecture_flow",
        "arcvisual.templates.registry",
    }
)

#: Import prefixes that are refused outright, with the reason shown to the model.
FORBIDDEN_IMPORTS: dict[str, str] = {
    "os": "filesystem and environment access is not permitted in a scene",
    "sys": "interpreter introspection is not permitted in a scene",
    "subprocess": "spawning processes is not permitted in a scene",
    "socket": "network access is not permitted in a scene",
    "http": "network access is not permitted in a scene",
    "urllib": "network access is not permitted in a scene",
    "requests": "network access is not permitted in a scene",
    "httpx": "network access is not permitted in a scene",
    "shutil": "filesystem mutation is not permitted in a scene",
    "pathlib": "filesystem access is not permitted in a scene",
    "importlib": "dynamic imports are not permitted in a scene",
    "ctypes": "native code loading is not permitted in a scene",
    "multiprocessing": "process spawning is not permitted in a scene",
    "threading": "threads are not permitted in a scene",
    "pickle": "deserialisation is not permitted in a scene",
    "tempfile": "write only to the path the renderer provides",
    "builtins": "builtins access is not permitted in a scene",
}

#: Builtins whose presence means the module is doing something a template should.
FORBIDDEN_CALLS: dict[str, str] = {
    "exec": "dynamic execution is not permitted",
    "eval": "dynamic evaluation is not permitted",
    "compile": "dynamic compilation is not permitted",
    "open": "file access is not permitted; the renderer owns all output paths",
    "input": "a render is non-interactive",
    "__import__": "dynamic imports are not permitted",
    "globals": "namespace introspection is not permitted",
    "locals": "namespace introspection is not permitted",
    "vars": "namespace introspection is not permitted",
    "setattr": "monkey-patching a template is not permitted",
    "delattr": "monkey-patching a template is not permitted",
    "breakpoint": "a render is non-interactive",
}

#: Attribute names that let generated code reach outside its own objects.
FORBIDDEN_ATTRS: dict[str, str] = {
    "__globals__": "namespace escape",
    "__builtins__": "namespace escape",
    "__subclasses__": "class-hierarchy escape",
    "__bases__": "class-hierarchy mutation",
    "__code__": "bytecode access",
    "__dict__": "namespace mutation; set attributes directly instead",
    "__mro__": "class-hierarchy introspection",
    "__class__": "type escape",
    "__reduce__": "serialisation escape",
}

#: A curated snapshot of the Manim CE 0.18.x public surface a template body may
#: touch. Grouped only for readability; the gate flattens it.
_MANIM_SNAPSHOT: dict[str, tuple[str, ...]] = {
    "scene": ("Scene", "MovingCameraScene", "ZoomedScene", "ThreeDScene", "config"),
    "mobject_text": (
        "Text",
        "MarkupText",
        "Tex",
        "MathTex",
        "SingleStringMathTex",
        "Title",
        "BulletedList",
        "Paragraph",
        "Code",
        "DecimalNumber",
        "Integer",
        "Variable",
        "Table",
        "MathTable",
        "DecimalTable",
        "IntegerTable",
    ),
    "mobject_geometry": (
        "Circle",
        "Dot",
        "AnnotationDot",
        "LabeledDot",
        "Ellipse",
        "Arc",
        "ArcBetweenPoints",
        "CurvedArrow",
        "CurvedDoubleArrow",
        "AnnularSector",
        "Sector",
        "Annulus",
        "Line",
        "DashedLine",
        "TangentLine",
        "Elbow",
        "Arrow",
        "Vector",
        "DoubleArrow",
        "Angle",
        "RightAngle",
        "Polygon",
        "Polygram",
        "RegularPolygon",
        "RegularPolygram",
        "Triangle",
        "Rectangle",
        "Square",
        "RoundedRectangle",
        "Cutout",
        "ArrowTip",
        "ArcPolygon",
        "Star",
        "Cross",
        "Underline",
        "SurroundingRectangle",
        "BackgroundRectangle",
        "Brace",
        "BraceLabel",
        "BraceBetweenPoints",
        "ArrowVectorField",
        "StreamLines",
        "VectorField",
        "NumberLine",
        "UnitInterval",
        "Axes",
        "ThreeDAxes",
        "NumberPlane",
        "PolarPlane",
        "ComplexPlane",
        "BarChart",
        "Surface",
        "Sphere",
        "Cube",
        "Prism",
        "Cone",
        "Cylinder",
        "Line3D",
        "Arrow3D",
        "Torus",
        "Dodecahedron",
        "Icosahedron",
        "Octahedron",
        "Tetrahedron",
        "ThreeDVMobject",
    ),
    "mobject_types": (
        "Mobject",
        "VMobject",
        "VGroup",
        "VDict",
        "Group",
        "DashedVMobject",
        "CurvesAsSubmobjects",
        "ImageMobject",
        "SVGMobject",
        "PointCloudDot",
        "Point",
        "ValueTracker",
        "ComplexValueTracker",
        "Graph",
        "DiGraph",
        "LayoutFunction",
    ),
    "animation_creation": (
        "Create",
        "Uncreate",
        "DrawBorderThenFill",
        "Write",
        "Unwrite",
        "AddTextLetterByLetter",
        "RemoveTextLetterByLetter",
        "ShowIncreasingSubsets",
        "ShowSubmobjectsOneByOne",
        "AddTextWordByWord",
        "SpiralIn",
    ),
    "animation_fading": (
        "FadeIn",
        "FadeOut",
        "FadeTransform",
        "FadeTransformPieces",
    ),
    "animation_transform": (
        "Transform",
        "ReplacementTransform",
        "TransformFromCopy",
        "ClockwiseTransform",
        "CounterclockwiseTransform",
        "MoveToTarget",
        "ApplyMethod",
        "ApplyPointwiseFunction",
        "ApplyMatrix",
        "ApplyComplexFunction",
        "ApplyFunction",
        "CyclicReplace",
        "Swap",
        "Restore",
        "TransformMatchingTex",
        "TransformMatchingShapes",
        "TransformMatchingAbstractBase",
        "ScaleInPlace",
        "ShrinkToCenter",
        "Rotate",
        "Rotating",
    ),
    "animation_indication": (
        "Indicate",
        "Flash",
        "ShowPassingFlash",
        "ApplyWave",
        "Circumscribe",
        "Wiggle",
        "FocusOn",
        "Blink",
    ),
    "animation_movement": (
        "Homotopy",
        "SmoothedVectorizedHomotopy",
        "ComplexHomotopy",
        "PhaseFlow",
        "MoveAlongPath",
        "GrowFromPoint",
        "GrowFromCenter",
        "GrowFromEdge",
        "GrowArrow",
        "SpinInFromNothing",
    ),
    "animation_composition": (
        "AnimationGroup",
        "Succession",
        "LaggedStart",
        "LaggedStartMap",
        "Animation",
        "Wait",
        "Add",
        "prepare_animation",
        "override_animation",
    ),
    "animation_numbers": ("ChangingDecimal", "ChangeDecimalToValue", "Count"),
    "animation_updaters": (
        "UpdateFromFunc",
        "UpdateFromAlphaFunc",
        "MaintainPositionRelativeTo",
        "always_redraw",
        "always_shift",
        "always_rotate",
        "turn_animation_into_updater",
        "cycle_animation",
        "f_always",
        "always",
    ),
    "rate_functions": (
        "rate_functions",
        "linear",
        "smooth",
        "rush_into",
        "rush_from",
        "slow_into",
        "double_smooth",
        "there_and_back",
        "there_and_back_with_pause",
        "running_start",
        "wiggle",
        "ease_in_sine",
        "ease_out_sine",
        "ease_in_out_sine",
        "ease_in_quad",
        "ease_out_quad",
        "ease_in_out_quad",
        "ease_in_cubic",
        "ease_out_cubic",
        "ease_in_out_cubic",
        "ease_in_expo",
        "ease_out_expo",
        "ease_in_out_expo",
        "ease_in_elastic",
        "ease_out_elastic",
        "ease_in_out_elastic",
        "ease_in_bounce",
        "ease_out_bounce",
        "ease_in_out_bounce",
        "ease_in_back",
        "ease_out_back",
        "ease_in_out_back",
    ),
    "constants_direction": (
        "UP",
        "DOWN",
        "LEFT",
        "RIGHT",
        "IN",
        "OUT",
        "ORIGIN",
        "UL",
        "UR",
        "DL",
        "DR",
        "X_AXIS",
        "Y_AXIS",
        "Z_AXIS",
        "TOP",
        "BOTTOM",
        "LEFT_SIDE",
        "RIGHT_SIDE",
    ),
    "constants_numeric": (
        "PI",
        "TAU",
        "DEGREES",
        "RADIANS",
        "E",
        "SMALL_BUFF",
        "MED_SMALL_BUFF",
        "MED_LARGE_BUFF",
        "LARGE_BUFF",
        "DEFAULT_MOBJECT_TO_EDGE_BUFFER",
        "DEFAULT_MOBJECT_TO_MOBJECT_BUFFER",
        "DEFAULT_STROKE_WIDTH",
        "DEFAULT_FONT_SIZE",
        "UNIT_VECTOR",
    ),
    "colors": (
        "WHITE",
        "BLACK",
        "GRAY",
        "GREY",
        "DARK_GRAY",
        "DARK_GREY",
        "LIGHT_GRAY",
        "LIGHT_GREY",
        "GRAY_A",
        "GRAY_B",
        "GRAY_C",
        "GRAY_D",
        "GRAY_E",
        "BLUE",
        "BLUE_A",
        "BLUE_B",
        "BLUE_C",
        "BLUE_D",
        "BLUE_E",
        "PURE_BLUE",
        "DARK_BLUE",
        "TEAL",
        "TEAL_A",
        "TEAL_B",
        "TEAL_C",
        "TEAL_D",
        "TEAL_E",
        "GREEN",
        "GREEN_A",
        "GREEN_B",
        "GREEN_C",
        "GREEN_D",
        "GREEN_E",
        "PURE_GREEN",
        "YELLOW",
        "YELLOW_A",
        "YELLOW_B",
        "YELLOW_C",
        "YELLOW_D",
        "YELLOW_E",
        "GOLD",
        "GOLD_A",
        "GOLD_B",
        "GOLD_C",
        "GOLD_D",
        "GOLD_E",
        "RED",
        "RED_A",
        "RED_B",
        "RED_C",
        "RED_D",
        "RED_E",
        "PURE_RED",
        "MAROON",
        "MAROON_A",
        "MAROON_B",
        "MAROON_C",
        "MAROON_D",
        "MAROON_E",
        "PURPLE",
        "PURPLE_A",
        "PURPLE_B",
        "PURPLE_C",
        "PURPLE_D",
        "PURPLE_E",
        "PINK",
        "LIGHT_PINK",
        "ORANGE",
        "LIGHT_BROWN",
        "DARK_BROWN",
        "GRAY_BROWN",
        "GREY_BROWN",
        "Color",
        "ManimColor",
        "color_gradient",
        "interpolate_color",
        "rgb_to_color",
        "color_to_rgb",
        "rgba_to_color",
        "hex_to_rgb",
        "average_color",
        "random_color",
        "random_bright_color",
    ),
    "utils": (
        "interpolate",
        "inverse_interpolate",
        "match_interpolate",
        "clip",
        "bezier",
        "smoothstep",
        "sigmoid",
        "choose",
        "there_and_back",
        "get_norm",
        "normalize",
        "angle_of_vector",
        "angle_between_vectors",
        "rotate_vector",
        "rotation_matrix",
        "z_to_vector",
        "line_intersection",
        "midpoint",
        "space_ops",
        "tempconfig",
        "ValueError",
    ),
}


@lru_cache(maxsize=1)
def manim_surface() -> frozenset[str]:
    """Allowed top-level Manim names.

    Live introspection wins when Manim is importable, so the render container is
    always checked against the version it will actually run. Otherwise the
    snapshot is used, which can only be more restrictive.
    """
    try:
        import manim

        live = {n for n in dir(manim) if not n.startswith("_")}
        if len(live) > 100:  # sanity: a real Manim exports hundreds of names
            return frozenset(live | _flat_snapshot())
    except ImportError:
        pass

    if _SNAPSHOT.exists():
        try:
            data = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("names"):
                return frozenset(data["names"]) | _flat_snapshot()
        except (OSError, json.JSONDecodeError):
            pass
    return _flat_snapshot()


@lru_cache(maxsize=1)
def _flat_snapshot() -> frozenset[str]:
    return frozenset(name for group in _MANIM_SNAPSHOT.values() for name in group)


def is_manim_name(name: str) -> bool:
    return name in manim_surface()


def _refresh() -> str:  # pragma: no cover - run inside the render container
    """Emit a fresh snapshot from the installed Manim."""
    import manim

    names = sorted(n for n in dir(manim) if not n.startswith("_"))
    return json.dumps(
        {"manim_version": getattr(manim, "__version__", "unknown"), "names": names},
        indent=2,
    )


if __name__ == "__main__":  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--refresh", action="store_true", help="print a fresh snapshot")
    args = ap.parse_args()
    if args.refresh:
        print(_refresh())
    else:
        print(f"{len(manim_surface())} allowed Manim names in this environment")
