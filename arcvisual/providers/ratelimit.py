"""A token-per-minute budget shared by every worker talking to one provider.

Free tiers meter *tokens per minute*, not requests, and Groq charges the
**requested** ``max_completion_tokens`` against that budget the moment a request
arrives — before generating anything. Three measurements shaped this module:

* A request whose budget exceeds the ceiling is refused with 413 no matter how short
  the real answer would have been. ``max_completion_tokens: 8192`` against an 8,000
  TPM key fails outright, while 6,000 succeeds and returns 589 tokens. The fix is to
  clamp the *ask*, not to shorten the reply.
* Scene generation fans out across lanes, so several workers draw on one allowance at
  once. Without a shared meter they collectively exceed it and each gets a 429,
  turning parallelism into a retry storm slower than running serially.
* The allowance refills **continuously**: with 7,971 of 8,000 tokens left, Groq
  reported ``x-ratelimit-reset-tokens: 217ms``. So this is a refilling bucket, not a
  fixed window — see :class:`TokenBudget` for why that distinction matters.
"""

from __future__ import annotations

import logging
import re
import threading
import time

log = logging.getLogger(__name__)

#: Longest a worker may sleep after a rate-limit rejection.
_MAX_BACKOFF_S = 20.0


#: Marks a 429 raised by a per-DAY quota rather than a per-minute one. Groq exposes
#: this ONLY in the error body — `x-ratelimit-remaining-tokens` reports the minute
#: allowance and reads a healthy 8000 while the daily budget is spent.
_DAILY_MARKERS = ("tokens per day", "(TPD)", "requests per day", "(RPD)")


def is_daily_limit(body: str) -> bool:
    """Whether a 429 body reports a per-day quota being exhausted.

    Worth distinguishing because the two need opposite responses. A per-minute
    rejection clears in seconds and retrying is right. A per-day one clears in
    minutes or hours — "Please try again in 15m35.28s" — and retrying against it
    burns the little that remains while reporting a confusing rate-limit error
    instead of the real problem.
    """
    return any(marker in body for marker in _DAILY_MARKERS)


def limit_resets_in(body: str) -> float | None:
    """Seconds until the quota in a 429 body clears, if it says.

    Groq's per-day window is ROLLING, not a midnight reset, so the wait scales with
    how much the request asks for: at 197,770 of 200,000 used, a 2,272-token request
    was told to wait 18 seconds while a 5,266-token one was told 22 minutes. That
    makes "daily quota" too blunt a verdict on its own — sometimes the right move is
    simply to wait a moment.
    """
    match = re.search(r"try again in ([\dhms.]+)", body)
    return _parse_duration(match.group(1).rstrip(".")) if match else None


def daily_limit_detail(body: str) -> str:
    """A short human summary of a per-day rejection, for the error message."""
    used = re.search(r"Limit (\d+), Used (\d+)", body)
    again = re.search(r"try again in ([\dhms.]+)", body)
    parts = []
    if used:
        parts.append(
            f"{int(used.group(2)):,} of {int(used.group(1)):,} daily tokens used"
        )
    if again:
        parts.append(f"resets in {again.group(1)}")
    return "; ".join(parts) or "daily quota exhausted"


def retry_after_seconds(headers: object, default: float = 2.0) -> float:
    """Seconds to wait after a 429, from the provider's own guidance.

    Groq answers with ``retry-after`` (seconds) and with reset hints shaped like
    ``7m12s``. Honouring them beats exponential backoff: guessing short hammers a
    limit that is already tripped, and guessing long idles a worker for no reason.
    """
    get = getattr(headers, "get", None)
    if get is None:
        return default
    for key in ("retry-after", "x-ratelimit-reset-tokens"):
        raw = get(key)
        if not raw:
            continue
        parsed = _parse_duration(str(raw))
        if parsed is not None:
            # Cap hard. A token-per-minute allowance refills continuously — Groq
            # reports `x-ratelimit-reset-tokens: 217ms` — so a long sleep is never
            # the right answer to a TPM rejection, and a 60s cap turned a job that
            # should take minutes into one that ran for over two hours.
            #
            # `x-ratelimit-reset-requests` is deliberately not consulted: it tracks a
            # different quota entirely, and treating it as token backoff idles every
            # lane over a limit that was not the one we hit.
            return min(parsed, _MAX_BACKOFF_S)
    return default


def _parse_duration(text: str) -> float | None:
    """Parse ``"12"``, ``"39.9s"``, ``"7m12s"``, ``"1h2m3s"``, ``"217ms"`` into seconds."""
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        pass
    match = re.fullmatch(
        r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?"
        r"(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?",
        text,
    )
    if not match or not any(match.groups()):
        return None
    h, m, s, ms = (float(g) if g else 0.0 for g in match.groups())
    return h * 3600 + m * 60 + s + ms / 1000


class TokenBudget:
    """A refilling token bucket, safe to share across threads.

    Modelled as a bucket rather than a fixed window because that is what the server
    does: with 7,971 of 8,000 tokens left, Groq reported
    ``x-ratelimit-reset-tokens: 217ms`` — an allowance topping up continuously, not
    one released in a lump every minute.

    The distinction is not academic. A local fixed window drifts out of phase with
    the server's, and when it rolls it releases every waiting scene lane at the same
    instant; they arrive together, are collectively refused, and parallelism becomes
    a retry storm slower than running serially would have been. A bucket has no edge
    to synchronise on, so lanes resume one at a time as capacity appears.

    The server's ``x-ratelimit-remaining-tokens`` still wins while it is fresh,
    because the server also counts usage this process cannot see — another session
    on the same key.
    """

    #: Held back when deciding how much to ASK for, not when deciding how much the
    #: bucket can hold. The two are different: the bucket must be able to hold what
    #: the server is willing to admit, or a request sized right up to the server's
    #: limit could never be reserved locally and would be silently under-counted.
    SAFETY_FRACTION = 0.90

    #: Multiplier on the caller's prompt estimate before reserving against the
    #: ceiling. Callers estimate at ~4 chars/token, which ran optimistic once the
    #: request also carried a JSON schema the estimate could not see: a 5,385-token
    #: estimate was counted as ~6,217 and refused against an 8,000 ceiling.
    #:
    #: 1.15 rather than something larger because the margin is not free — it comes
    #: straight out of the reply's budget, and a reply that runs short fails as hard
    #: as one that is refused.
    ESTIMATE_MARGIN = 1.15

    #: Held back for per-request overhead the caller cannot see.
    HEADROOM_TOKENS = 256

    def __init__(self, tpm: int, window_s: float = 60.0) -> None:
        self.tpm = tpm
        self.window_s = window_s
        self._lock = threading.Condition()
        # The bucket holds the server's FULL allowance. Shrinking it here was a quiet
        # accounting hole: `acquire` clamps a reservation to capacity, so a request
        # sized between 90% and 100% of the allowance reserved less than it went on to
        # ask the server for, and the meter believed it had room it had already spent.
        # The safety margin belongs in `clamp`, which decides the size of the ask.
        self.capacity = max(0, tpm)
        self._tokens = float(self.capacity)
        self._last_refill = time.monotonic()
        self._server_remaining: int | None = None
        self._server_seen_at = 0.0

    # -- internals ---------------------------------------------------------- #

    @property
    def _rate(self) -> float:
        """Tokens per second: the whole allowance refills over one window."""
        return self.tpm / self.window_s if self.window_s > 0 else 0.0

    def _refill(self, now: float) -> None:
        """Top the bucket up for elapsed time. Caller holds the lock."""
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        self._last_refill = now
        self._tokens = min(float(self.capacity), self._tokens + elapsed * self._rate)

    def _available(self, now: float) -> float:
        """Tokens believed spendable. Caller holds the lock."""
        self._refill(now)
        if self._server_remaining is not None and now - self._server_seen_at < 10.0:
            # A recent server reading is authoritative, but only briefly: it is a
            # snapshot of a bucket that has been refilling ever since it was taken.
            projected = self._server_remaining + (now - self._server_seen_at) * self._rate
            return min(self._tokens, projected)
        return self._tokens

    # -- public ------------------------------------------------------------- #

    def clamp(self, requested: int, prompt_tokens: int = 0) -> int:
        """The largest output budget that can be asked for without a certain 413.

        ``prompt_tokens`` is charged alongside the output budget rather than
        separately, so it is subtracted — with a margin, since the caller's estimate
        is a character count divided by four and the server's is a real tokeniser.
        """
        if self.tpm <= 0:
            return requested
        padded = int(prompt_tokens * self.ESTIMATE_MARGIN) + self.HEADROOM_TOKENS
        spendable = int(self.tpm * self.SAFETY_FRACTION)
        headroom = max(spendable - padded, 256)
        return max(1, min(requested, headroom))

    def acquire(self, tokens: int, timeout_s: float = 180.0) -> int:
        """Reserve ``tokens``, waiting for the bucket to refill if need be.

        Waiting is the point: sleeping until capacity exists is strictly cheaper than
        being refused and retried, because a rejection still costs a round-trip and
        still counts against the daily request quota.
        """
        if self.tpm <= 0:
            return 0
        tokens = min(tokens, self.capacity)
        deadline = time.monotonic() + timeout_s
        with self._lock:
            while True:
                now = time.monotonic()
                available = self._available(now)
                if available >= tokens:
                    self._tokens -= tokens
                    return tokens
                remaining = deadline - now
                if remaining <= 0:
                    # Out of patience: proceed and let the server arbitrate. Better a
                    # possible 429 than blocking a scene indefinitely.
                    log.debug("token budget wait timed out; proceeding unmetered")
                    self._tokens = max(0.0, self._tokens - tokens)
                    return tokens
                shortfall = tokens - available
                wait = shortfall / self._rate if self._rate > 0 else 1.0
                self._lock.wait(timeout=max(0.05, min(wait, remaining, 5.0)))

    def settle(self, reserved: int, actual_total: int) -> None:
        """Return the gap between what was reserved and what was really spent.

        Reservations are made against ``max_tokens``, an upper bound the model
        usually does not reach — one measured call reserved 6,000 and used 1,284.
        Without the refund the meter throttles several times harder than the provider
        actually does.
        """
        if self.tpm <= 0 or reserved <= 0:
            return
        with self._lock:
            refund = max(0, reserved - max(0, actual_total))
            self._tokens = min(float(self.capacity), self._tokens + refund)
            self._lock.notify_all()

    def observe(self, headers: object) -> None:
        """Adopt the server's own view of the allowance from response headers."""
        get = getattr(headers, "get", None)
        if get is None:
            return
        raw = get("x-ratelimit-remaining-tokens")
        if raw is None:
            return
        try:
            remaining = int(float(raw))
        except (TypeError, ValueError):
            return
        with self._lock:
            self._server_remaining = remaining
            self._server_seen_at = time.monotonic()
            self._lock.notify_all()

    def penalise(self, headers: object) -> None:
        """Empty the bucket after a 429, so waiters do not pile straight back in.

        Without it, every lane blocked behind a refused request wakes, sees a bucket
        that local accounting believes has capacity, and fires into the same limit.
        """
        delay = retry_after_seconds(headers, default=5.0)
        with self._lock:
            self._tokens = 0.0
            # Push the refill clock forward so the bucket stays genuinely empty for
            # as long as the server asked, rather than refilling through the penalty.
            self._last_refill = time.monotonic() + delay
            self._server_remaining = 0
            self._server_seen_at = time.monotonic()
        return None
