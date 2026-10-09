"""Test-wide safety rails.

**Assert on effects, not on status fields.** Four bugs in this project shipped past a
green suite because tests checked what the code *claimed* rather than what it *did*:
a scene reported ``state == PASSED`` with a valid ``content_hash`` while storing zero
bytes; a gate reported ``passed`` while its renderer had produced nothing readable.
Both assertions were true and the product was broken. Prefer
``artifact.bytes > 0`` and "a file exists under the media root" over "the field says
so".


**The important one:** the suite pins ``ARCVISUAL_PROVIDER=heuristic``. Without it, a
developer who happens to have ``ANTHROPIC_API_KEY`` exported — or ``opencode`` on their
PATH — would have ``pytest`` make real, billed model calls, and the results would stop
being deterministic. A test suite whose cost and outcome depend on the machine it runs
on is not a test suite.

Tests that need a provider construct one explicitly (see ``test_providers.py``), which
is the only way it should ever happen here.
"""

from __future__ import annotations

import os

# Before importing anything that reads configuration: a developer's .env holds real
# keys and a real ARCVISUAL_PROVIDER, and silently inheriting them would make the
# suite reach the network on their machine and not on CI.
os.environ["ARCVISUAL_NO_DOTENV"] = "1"

import pytest

from arcvisual.config import settings

#: Env vars that could make the suite reach the network or a paid API.
_MUTED = (
    "ANTHROPIC_API_KEY",
    "POOLSIDE_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "GEMINI_API_KEY",
    "MISTRAL_API_KEY",
    "DEEPSEEK_API_KEY",
    "CEREBRAS_API_KEY",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_PRESET",
    "DATABASE_URL",
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
)


@pytest.fixture(scope="session", autouse=True)
def _deterministic_environment() -> None:
    """Force the heuristic analyzer and clear credentials for the whole session."""
    for name in _MUTED:
        os.environ.pop(name, None)
    os.environ["ARCVISUAL_PROVIDER"] = "heuristic"
    os.environ.setdefault("IP_HASH_SALT", "test-salt")
    # settings() is lru_cached, so it must be rebuilt after touching the environment.
    settings.cache_clear()
    yield
    settings.cache_clear()


@pytest.fixture
def reset_settings():
    """For tests that deliberately change the environment.

    Yields a callable that applies env overrides and rebuilds the cached settings, and
    restores the original environment afterwards.
    """
    original = dict(os.environ)

    def apply(**overrides: str | None) -> None:
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        settings.cache_clear()

    yield apply

    os.environ.clear()
    os.environ.update(original)
    settings.cache_clear()
