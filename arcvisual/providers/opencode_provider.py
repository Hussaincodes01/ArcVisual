"""opencode, as a local subprocess.

**opencode is an agent CLI, not an inference API** — worth being precise about, because
it changes what this provider is. It wraps whatever model you configured through
``opencode auth login`` (any provider on Models.dev), runs an agent loop with tools, and
prints an answer. So selecting ``opencode`` here does not select a *model*; it delegates
that choice to your opencode configuration.

Verified against https://opencode.ai/docs/cli/ :

* ``opencode run [message..]`` is the non-interactive form
* ``--model provider/model`` picks the model, ``--agent`` the agent
* ``--format json`` emits **raw JSON events** (a stream), ``default`` emits formatted text
* ``--attach http://localhost:4096`` reuses a running ``opencode serve``, skipping
  backend and MCP cold-start on every call
* ``--variant`` sets provider-specific reasoning effort

Two flags are easy to get wrong and are called out here because I checked: ``-f`` is
``--file``, **not** ``--format``, and ``-p`` is ``--password``, **not** print/prompt. This
provider only uses long flags.

Three consequences of it being an agent rather than an API:

* **Cost is unattributable.** Billing happens inside opencode against its own provider
  credentials. Token counts are not reliably exposed, so :class:`Usage` comes back
  ``attributed=False``.
* **The prompt goes on stdin-free argv, which has a length limit.** A whole paper body
  in a system prompt would blow the command-line limit on every platform, so the prompt
  is written to a temp file and attached with ``--file``.
* **The agent has tools and a working directory.** It could touch the filesystem. This
  provider runs it in an empty temp directory with ``--auto`` withheld by default, so
  nothing is auto-approved.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from arcvisual.config import settings
from arcvisual.providers.base import (
    Capabilities,
    ProviderError,
    ProviderNotConfigured,
    StructuredResult,
    Task,
    TextBlock,
    Usage,
    prompted_structured,
)

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class OpencodeProvider:
    name = "opencode"

    def __init__(self, runner: Any = None) -> None:
        cfg = settings().opencode
        self._cfg = cfg
        self._runner = runner  # injectable for tests
        # Resolve to a full path and keep it. On Windows the installed entry point
        # is a `.CMD` shim, and CreateProcess does not apply PATHEXT to a bare name —
        # so `shutil.which` finds it while `subprocess.run(["opencode", ...])` dies
        # with WinError 2. Passing the resolved path works on every platform.
        #: Overridden per call by the healthcheck, which must fail fast.
        self._timeout_s = cfg.timeout_s
        self._binary = shutil.which(cfg.binary) if runner is None else cfg.binary
        if runner is None and self._binary is None:
            raise ProviderNotConfigured(
                f"the {cfg.binary!r} binary is not on PATH "
                "(install from https://opencode.ai and run `opencode auth login`)"
            )

    # -- plumbing ---------------------------------------------------------- #

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name=self.name,
            native_structured_output=False,
            prompt_cache=False,
            vision=False,
            # opencode emits per-step `tokens` and `cost` in its JSON stream, so
            # cost IS attributable here — an earlier version of this file claimed
            # otherwise, before the stream format had been looked at.
            cost_attributed=True,
            note=(
                "local agent CLI; the model comes from your opencode config and "
                "billing runs through ITS credentials, but per-step cost and tokens "
                "are reported in the event stream so spend is still attributed. "
                "The agent prompt adds ~28k input tokens per call. Run "
                "`opencode serve` and set OPENCODE_ATTACH to avoid a cold start."
            ),
        )

    def model_for(self, task: Task) -> str:
        """May be empty: with no ``--model`` flag, opencode uses its own default."""
        return self._cfg.model_for(task.value)

    def healthcheck(self, timeout_s: float = 90.0):
        """Will this backend actually answer right now?

        Runs under its OWN short timeout. A probe that inherited the full call budget
        would take as long to report a dead provider as the thing it exists to avoid.
        opencode needs a generous floor even so: a trivial ping measured 39s here,
        because every call pays the agent's cold start before reaching the model.
        """
        from arcvisual.providers.base import probe

        previous = self._timeout_s
        self._timeout_s = timeout_s
        try:
            return probe(self, timeout_s)
        finally:
            self._timeout_s = previous

    # -- the call ---------------------------------------------------------- #

    def structured(
        self,
        *,
        system: list[TextBlock],
        user: str,
        output_model: type[T],
        task: Task,
        max_tokens: int = 8192,
        max_attempts: int = 2,
    ) -> StructuredResult:
        model = self.model_for(task)

        def send(system_text: str, user_text: str) -> tuple[str, Usage]:
            return self._run(model, system_text, user_text, timeout_s=self._timeout_s)

        return prompted_structured(
            send=send,
            system=system,
            user=user,
            output_model=output_model,
            provider=self.name,
            model=model or "<opencode default>",
            max_attempts=max_attempts,
        )

    def _run(
        self,
        model: str,
        system_text: str,
        user_text: str,
        *,
        timeout_s: float | None = None,
    ) -> tuple[str, Usage]:
        cfg = self._cfg
        timeout_s = timeout_s or cfg.timeout_s
        # An empty cwd: the agent has tools, and it has no business in our repo.
        with tempfile.TemporaryDirectory(prefix="arcvisual-opencode-") as tmp:
            workdir = Path(tmp)
            context = workdir / "context.md"
            # argv has a hard length limit on every platform and the paper body is
            # far past it, so the bulk goes in a file and is attached.
            context.write_text(system_text, encoding="utf-8")

            argv = [self._binary, "run", "--format", "json", "--dir", str(workdir)]
            if model:
                argv += ["--model", model]
            if cfg.agent:
                argv += ["--agent", cfg.agent]
            if cfg.variant:
                argv += ["--variant", cfg.variant]
            if cfg.attach:
                argv += ["--attach", cfg.attach]
            argv += ["--file", str(context)]
            # `--` terminates flag parsing. Without it the message is consumed as
            # another value of the variadic `--file`, and opencode fails with
            # "File not found: <the entire prompt>". It also protects any prompt that
            # happens to begin with a dash.
            argv += ["--", user_text]

            try:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                    cwd=str(workdir),
                    env={**os.environ, "NO_COLOR": "1", "CI": "1"},
                )
            except subprocess.TimeoutExpired as exc:
                raise ProviderError(
                    f"opencode produced no output in {timeout_s:.0f}s. Its route can "
                    "go quiet under throttling — no error, no exit, just silence"
                ) from exc
            except OSError as exc:
                raise ProviderError(f"could not run opencode: {exc}") from exc

        if proc.returncode != 0:
            # opencode reports most failures as JSON events on STDOUT, so a message
            # built from stderr alone reads "exited 1: " and sends whoever is
            # debugging to the wrong stream. Prefer a parsed error, then stderr,
            # then raw stdout.
            detail = (
                stream_error(proc.stdout)
                or (proc.stderr or "").strip()[-400:]
                or (proc.stdout or "").strip()[-400:]
                or "no output on either stream"
            )
            raise ProviderError(f"opencode exited {proc.returncode}: {detail}")

        # opencode reports API failures as an `error` EVENT with exit code 0. An
        # insufficient-balance 401 is indistinguishable from success to a caller that
        # only checks the return code, so the stream has to be inspected.
        if failure := stream_error(proc.stdout):
            raise ProviderError(f"opencode: {failure}")

        usage = stream_usage(proc.stdout)
        text = extract_assistant_text(proc.stdout)
        if not text.strip():
            # --format json is documented as "raw JSON events" without a fixed
            # schema, so a shape we cannot read is a real possibility. Fall back to
            # the raw stream rather than losing the answer; extract_json downstream
            # is tolerant enough to find an object inside it.
            log.info("opencode: no assistant text recognised, using raw stdout")
            text = proc.stdout
        return text, usage


#: opencode's `--format json` stream, as observed on v1.18.15. Each stdout line is
#: one event: {"type": ..., "sessionID": ..., "part": {...}}. The assistant's answer
#: arrives as `type: "text"` with the payload at `part.text` — NOT at the top level,
#: which an earlier version of this parser assumed and consequently never found.
_TEXT_EVENT_TYPES = frozenset({"text", "message", "assistant"})
_SKIP_PART_TYPES = ("tool", "reasoning", "step-start", "step-finish", "patch", "file")


def extract_assistant_text(stdout: str) -> str:
    """Recover the assistant's answer from opencode's JSON event stream.

    Tool calls, reasoning and step markers are skipped so a tool's output can never
    be mistaken for the answer. Unknown shapes fall back to a recursive text hunt
    rather than returning nothing, because the format carries no stability promise
    and losing a paid call to a renamed field would be worse than a little noise.
    """
    events = _load_events(stdout)
    if not events:
        return ""

    parts: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("type") == "error":
            continue  # surfaced by stream_error(), not concatenated into the answer
        part = event.get("part")
        if isinstance(part, dict):
            part_type = str(part.get("type", ""))
            if any(skip in part_type for skip in _SKIP_PART_TYPES):
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
            continue
        if str(event.get("type", "")) in _TEXT_EVENT_TYPES:
            text = event.get("text") or event.get("content")
            if isinstance(text, str) and text:
                parts.append(text)

    if parts:
        return "".join(parts)
    # Nothing matched the known shape. Hunt for any text-ish value rather than
    # discarding a call that was already paid for.
    return "".join(filter(None, (_deep_text(e) for e in events if isinstance(e, dict))))


def stream_error(stdout: str) -> str | None:
    """The error message from an `error` event, if the run produced one.

    opencode reports API failures as a normal event on stdout with exit code 0 — an
    insufficient-balance 401 looks exactly like a successful run to a caller that
    only checks the return code.
    """
    for event in _load_events(stdout):
        if isinstance(event, dict) and event.get("type") == "error":
            err = event.get("error") or {}
            data = err.get("data") if isinstance(err, dict) else None
            if isinstance(data, dict) and data.get("message"):
                return f"{err.get('name', 'error')}: {data['message']}"
            if isinstance(err, dict) and err.get("name"):
                return str(err["name"])
            return "opencode reported an unspecified error"
    return None


def stream_usage(stdout: str) -> Usage:
    """Tokens and cost from the `step-finish` events.

    opencode *does* report both, which is why this provider attributes cost when a
    figure is present rather than reporting it as unknowable. A free model reports
    `cost: 0`, and that zero is a real zero — distinct from the unpriced case.
    """
    total_in = total_out = 0
    cost = 0.0
    saw_cost = False
    for event in _load_events(stdout):
        if not isinstance(event, dict):
            continue
        part = event.get("part")
        if not isinstance(part, dict) or "step-finish" not in str(part.get("type", "")):
            continue
        tokens = part.get("tokens") or {}
        total_in += int(tokens.get("input") or 0)
        total_out += int(tokens.get("output") or 0)
        if part.get("cost") is not None:
            cost += float(part["cost"])
            saw_cost = True
    return Usage(
        input_tokens=total_in,
        output_tokens=total_out,
        cost_usd=round(cost, 6),
        attributed=saw_cost,
    )


def _load_events(stdout: str) -> list[Any]:
    stdout = (stdout or "").strip()
    if not stdout:
        return []
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        pass
    else:
        return parsed if isinstance(parsed, list) else [parsed]

    events: list[Any] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line[0] not in "[{":
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _deep_text(value: Any, depth: int = 0) -> str:
    """Last-resort recursive hunt for a text field in an unrecognised event."""
    if depth > 4 or not isinstance(value, dict):
        return ""
    for key in ("text", "content", "output", "delta"):
        found = value.get(key)
        if isinstance(found, str) and found:
            return found
    for nested in value.values():
        if isinstance(nested, dict):
            found = _deep_text(nested, depth + 1)
            if found:
                return found
    return ""
