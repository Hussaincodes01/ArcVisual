"""The template registry — the single place that knows which archetypes exist.

Analyze must only ever propose an archetype that can actually be rendered, so
the triage prompt is built from :func:`available_archetypes` rather than from the
full taxonomy. Declaring an unimplemented archetype in the enum is free;
promising one to the model is not.

Registering a template also pins its ``source_sha`` into the L2 cache key, which
is what makes a template edit invalidate exactly its own scenes.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from types import ModuleType
from typing import Any

from pydantic import ValidationError

from arcvisual.cache.hashing import source_sha
from arcvisual.storyboard import Archetype
from arcvisual.templates.base import BuildFn, TemplateParams

#: Import paths of the templates this build ships. Phase 1: three.
_TEMPLATE_MODULES = (
    "arcvisual.templates.transform_chain",
    "arcvisual.templates.plot_reveal",
    "arcvisual.templates.architecture_flow",
)


@dataclass(frozen=True)
class Template:
    template_id: str
    archetype: Archetype
    params_model: type[TemplateParams]
    build: BuildFn
    module: ModuleType
    simplify: Callable[[Any], Any | None] | None
    #: Runtime the template will actually play, from its own timeline.
    estimate_duration: Callable[[Any], float] | None

    @property
    def source_sha(self) -> str:
        """Digest of the template module's own source. Feeds the L2 cache key."""
        return source_sha(self.module)

    @property
    def import_path(self) -> str:
        return self.module.__name__

    def validate_params(self, raw: dict[str, Any]) -> TemplateParams:
        """Parse model-supplied params. Raises ``ValidationError`` for Gate 1."""
        return self.params_model.model_validate(raw)

    def param_schema(self) -> dict[str, Any]:
        """JSON Schema handed to the model as a tool-use input schema.

        The descriptions on each field are load-bearing — they are the entire
        instruction set the model gets for this archetype.
        """
        return self.params_model.model_json_schema()


class UnknownArchetype(KeyError):
    """An archetype with no registered template in this build."""


@lru_cache(maxsize=1)
def _registry() -> dict[Archetype, Template]:
    out: dict[Archetype, Template] = {}
    for path in _TEMPLATE_MODULES:
        mod = importlib.import_module(path)
        missing = [
            attr
            for attr in (
                "TEMPLATE_ID",
                "ARCHETYPE",
                "Params",
                "build",
                "estimate_duration",
            )
            if not hasattr(mod, attr)
        ]
        if missing:
            raise RuntimeError(f"template {path} is missing {missing}")
        archetype: Archetype = mod.ARCHETYPE
        if archetype in out:
            raise RuntimeError(f"two templates claim archetype {archetype.value!r}")
        out[archetype] = Template(
            template_id=mod.TEMPLATE_ID,
            archetype=archetype,
            params_model=mod.Params,
            build=mod.build,
            module=mod,
            simplify=getattr(mod, "simplify", None),
            estimate_duration=getattr(mod, "estimate_duration", None),
        )
    return out


def get(archetype: Archetype | str) -> Template:
    key = Archetype(archetype) if isinstance(archetype, str) else archetype
    try:
        return _registry()[key]
    except KeyError as exc:
        raise UnknownArchetype(
            f"{key.value!r} has no template in this build; available: "
            f"{sorted(a.value for a in available_archetypes())}"
        ) from exc


def available_archetypes() -> list[Archetype]:
    return sorted(_registry(), key=lambda a: a.value)


def all_templates() -> list[Template]:
    return [_registry()[a] for a in available_archetypes()]


def is_available(archetype: Archetype | str) -> bool:
    try:
        get(archetype)
    except (UnknownArchetype, ValueError):
        return False
    return True


def validate_or_error(archetype: Archetype, raw: dict[str, Any]) -> tuple[Any, list[str]]:
    """Validate params, returning ``(params_or_None, findings)`` for Gate 1.

    Findings are phrased as instructions rather than as tracebacks, because they
    are fed verbatim back into the repair prompt.
    """
    try:
        tpl = get(archetype)
    except UnknownArchetype as exc:
        # KeyError.__str__ re-quotes its argument; take the message directly.
        return None, [str(exc.args[0])]
    try:
        return tpl.validate_params(raw), []
    except ValidationError as exc:
        findings = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "<root>"
            findings.append(f"parameter {loc}: {err['msg']}")
        return None, findings
