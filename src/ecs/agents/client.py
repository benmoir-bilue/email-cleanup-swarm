"""Anthropic client plumbing shared by every stage of the swarm.

Each tier needs a *different* request shape, and getting one wrong is a 400 rather
than a degraded result. Centralising that here means the stage modules can express
intent ("classify this cluster") without each carrying a copy of the per-model
parameter rules:

  * **Haiku 4.5** rejects `output_config.effort` outright, and only supports the
    legacy `thinking: {type: "enabled", budget_tokens: N}` form.
  * **Opus 5** has thinking *on by default*, so `max_tokens` must cover thinking
    plus the answer or output truncates mid-JSON.
  * **Fable 5** rejects any explicit `thinking` config — the parameter must be
    omitted entirely — and requires 30-day data retention at the org level.

All three can return `stop_reason: "refusal"` as a successful HTTP 200, so every
read of `.content` goes through `extract_text`, which checks first.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import anthropic

from .. import config


class RefusalError(RuntimeError):
    """The model declined the request. Not retryable with the same input."""

    def __init__(self, model: str, category: str | None, explanation: str | None):
        self.model = model
        self.category = category
        self.explanation = explanation
        super().__init__(
            f"{model} declined the request"
            + (f" (category: {category})" if category else "")
            + (f": {explanation}" if explanation else "")
        )


_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    """Return a shared client.

    Zero-arg construction is deliberate: it resolves ANTHROPIC_API_KEY, then
    ANTHROPIC_AUTH_TOKEN, then an `ant auth login` profile. You may well have a
    profile rather than an exported key.
    """
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=4, timeout=600.0)
    return _client


# ---------------------------------------------------------------------------
# Cost accounting
# ---------------------------------------------------------------------------


@dataclass
class CostTracker:
    """Running token and dollar totals, so a run can report what it spent."""

    by_model: dict[str, dict[str, float]] = field(default_factory=dict)

    def add(self, model: str, usage: Any, *, batch: bool = False) -> None:
        rate_in, rate_out = config.PRICING_PER_MTOK.get(model, (0.0, 0.0))
        # The Batch API is half price on all token usage.
        multiplier = 0.5 if batch else 1.0

        tokens_in = getattr(usage, "input_tokens", 0) or 0
        tokens_out = getattr(usage, "output_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0

        # Cache reads bill at ~0.1x input, writes at ~1.25x.
        cost = (
            tokens_in * rate_in
            + cache_read * rate_in * 0.1
            + cache_write * rate_in * 1.25
            + tokens_out * rate_out
        ) / 1_000_000 * multiplier

        bucket = self.by_model.setdefault(
            model, {"input": 0, "output": 0, "cache_read": 0, "cost": 0.0, "calls": 0}
        )
        bucket["input"] += tokens_in
        bucket["output"] += tokens_out
        bucket["cache_read"] += cache_read
        bucket["cost"] += cost
        bucket["calls"] += 1

    @property
    def total_cost(self) -> float:
        return sum(b["cost"] for b in self.by_model.values())

    def summary(self) -> str:
        if not self.by_model:
            return "no model calls"
        lines = [
            f"  {model}: {b['calls']:,} calls, "
            f"{int(b['input']):,} in / {int(b['output']):,} out, ${b['cost']:.2f}"
            for model, b in sorted(self.by_model.items())
        ]
        lines.append(f"  total: ${self.total_cost:.2f}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-model request construction
# ---------------------------------------------------------------------------


def build_params(
    model: str,
    *,
    system: str | list[dict[str, Any]],
    messages: list[dict[str, Any]],
    max_tokens: int,
    schema: dict[str, Any] | None = None,
    effort: str | None = None,
    thinking: bool = False,
) -> dict[str, Any]:
    """Assemble a request body valid for the given model.

    This is the single place that encodes per-tier API differences.
    """
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }

    if schema is not None:
        params["output_config"] = {
            "format": {"type": "json_schema", "schema": schema}
        }

    is_haiku = model.startswith("claude-haiku")
    is_fable = model.startswith("claude-fable") or model.startswith("claude-mythos")

    if is_haiku:
        # `effort` errors on Haiku 4.5. Thinking, if wanted, uses the legacy form
        # with an explicit budget that must stay under max_tokens.
        if thinking:
            params["thinking"] = {
                "type": "enabled",
                "budget_tokens": max(1024, max_tokens // 2),
            }
    else:
        if effort:
            params.setdefault("output_config", {})["effort"] = effort
        if is_fable:
            # Fable rejects ANY explicit thinking config — omit the key entirely.
            # Thinking is always on for this model regardless.
            params.pop("thinking", None)
        elif thinking:
            params["thinking"] = {"type": "adaptive", "display": "summarized"}
        else:
            # Opus 5 thinks by default; disabling is only legal at effort <= high.
            if effort in (None, "low", "medium", "high"):
                params["thinking"] = {"type": "disabled"}

    return params


def _refusal_check(response: Any, model: str) -> None:
    if getattr(response, "stop_reason", None) != "refusal":
        return
    details = getattr(response, "stop_details", None)
    raise RefusalError(
        model,
        getattr(details, "category", None),
        getattr(details, "explanation", None),
    )


def extract_text(response: Any, model: str) -> str:
    """Pull the text content out of a response, refusing to read a refusal."""
    _refusal_check(response, model)
    for block in response.content:
        if block.type == "text":
            return block.text
    return ""


def call_json(
    model: str,
    *,
    system: str,
    user: str,
    schema: dict[str, Any],
    max_tokens: int = 16000,
    effort: str | None = None,
    thinking: bool = False,
    tracker: CostTracker | None = None,
    use_fallbacks: bool = True,
) -> dict[str, Any]:
    """One structured-output call. Streams, because thinking makes turns long."""
    client = get_client()
    params = build_params(
        model,
        system=system,
        messages=[{"role": "user", "content": user}],
        max_tokens=max_tokens,
        schema=schema,
        effort=effort,
        thinking=thinking,
    )

    # Opus 5 and Fable 5 classifiers can decline; a server-side fallback re-serves
    # the request on another model inside the same call rather than losing the
    # stage. "default" routes by refusal category so there's no model list to
    # maintain. Haiku has no fallback surface.
    extra_body: dict[str, Any] = {}
    extra_headers: dict[str, str] = {}
    if use_fallbacks and not model.startswith("claude-haiku"):
        extra_body["fallbacks"] = "default"
        extra_headers["anthropic-beta"] = "server-side-fallback-2026-07-01"

    try:
        with client.beta.messages.stream(
            **params, extra_body=extra_body or None, extra_headers=extra_headers or None
        ) as stream:
            response = stream.get_final_message()
    except anthropic.BadRequestError as exc:
        # If the beta or fallbacks parameter isn't available on this account, retry
        # once without it rather than failing the whole stage.
        if not extra_body:
            raise
        message = str(exc)
        if "fallback" not in message.lower():
            raise
        with client.messages.stream(**params) as stream:
            response = stream.get_final_message()

    if tracker is not None:
        tracker.add(response.model or model, response.usage)

    text = extract_text(response, model)
    return json.loads(text)


# ---------------------------------------------------------------------------
# Batch API — the workhorse for per-cluster and per-message classification
# ---------------------------------------------------------------------------


@dataclass
class BatchRequest:
    custom_id: str
    params: dict[str, Any]


def submit_batch(requests: list[BatchRequest]) -> str:
    """Submit a batch and return its id. 50% cheaper than serial calls."""
    client = get_client()
    batch = client.messages.batches.create(
        requests=[{"custom_id": r.custom_id, "params": r.params} for r in requests]
    )
    return batch.id


def wait_for_batch(
    batch_id: str,
    *,
    total: int | None = None,
    poll_seconds: int | None = None,
    timeout_seconds: int | None = None,
    progress: Any = None,
) -> str:
    """Block until a batch ends. Returns the terminal processing status.

    The Batch API reports `succeeded` in chunks rather than continuously, so a large
    batch sits at 0 done for a long time. Logging an identical line every poll makes
    a healthy run look hung, so output is emitted only when the counts actually change,
    plus a periodic heartbeat carrying elapsed time so it's visibly alive.
    """
    client = get_client()
    poll = poll_seconds or config.TUNABLES.batch_poll_seconds
    started = time.monotonic()
    deadline = started + (timeout_seconds or config.TUNABLES.batch_timeout_seconds)

    has_bar = hasattr(progress, "stage")
    if has_bar and total:
        progress.stage(f"batch {batch_id[-8:]} processing", total=total)

    last_counts: tuple[int, int, int] | None = None
    last_heartbeat = 0.0

    while True:
        batch = client.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        current = (counts.succeeded, counts.errored, counts.processing)
        elapsed = time.monotonic() - started

        if has_bar:
            progress.set_completed(counts.succeeded + counts.errored)

        if batch.processing_status == "ended":
            if progress is not None:
                progress(
                    f"batch ended after {elapsed / 60:.1f} min: "
                    f"{counts.succeeded:,} succeeded, {counts.errored:,} errored"
                )
            return batch.processing_status

        if time.monotonic() > deadline:
            raise TimeoutError(
                f"batch {batch_id} still {batch.processing_status} after "
                f"{elapsed / 60:.0f} min. It is still running server-side — re-run the "
                "same command to resume polling rather than resubmitting."
            )

        if progress is not None:
            changed = current != last_counts
            # Heartbeat every two minutes so a long quiet stretch still shows life.
            due = elapsed - last_heartbeat > 120
            if changed or due:
                progress(
                    f"{counts.succeeded:,}/{total or counts.processing:,} done"
                    f"{f', {counts.errored:,} errored' if counts.errored else ''}"
                    f" — {elapsed / 60:.0f} min elapsed"
                    + ("" if changed else " (no change yet; batches report in chunks)")
                )
                last_counts = current
                last_heartbeat = elapsed

        time.sleep(poll)


def collect_batch(
    batch_id: str, *, model: str, tracker: CostTracker | None = None
) -> Iterator[tuple[str, dict[str, Any] | None, str | None]]:
    """Yield (custom_id, parsed_json, error) for every result in a batch.

    Results arrive in arbitrary order, so callers must key on custom_id — never on
    position. Errors are yielded rather than raised: one bad cluster shouldn't
    discard the other 349.
    """
    client = get_client()
    for result in client.messages.batches.results(batch_id):
        custom_id = result.custom_id
        kind = result.result.type

        if kind != "succeeded":
            detail = kind
            if kind == "errored":
                err = getattr(result.result, "error", None)
                detail = f"errored: {getattr(err, 'type', 'unknown')}"
            yield custom_id, None, detail
            continue

        message = result.result.message
        if tracker is not None:
            tracker.add(model, message.usage, batch=True)

        if getattr(message, "stop_reason", None) == "refusal":
            yield custom_id, None, "refusal"
            continue

        text = next((b.text for b in message.content if b.type == "text"), "")
        try:
            yield custom_id, json.loads(text), None
        except json.JSONDecodeError as exc:
            yield custom_id, None, f"invalid json: {exc}"


def run_batch(
    requests: list[BatchRequest],
    *,
    model: str,
    tracker: CostTracker | None = None,
    progress: Any = None,
    resume_key: str | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Submit, wait, and collect. Returns (results by id, errors by id).

    `resume_key` makes this crash-safe. The batch id is persisted before waiting, so a
    run interrupted mid-poll (Ctrl-C, closed laptop, timeout) resumes polling the
    *existing* batch instead of submitting a fresh one. Without it, an interrupted
    1,851-request batch would be silently paid for twice — the first one keeps running
    server-side regardless.
    """
    if not requests:
        return {}, {}

    from .. import db

    batch_id: str | None = None
    if resume_key:
        with db.session() as conn:
            batch_id = db.kv_get(conn, resume_key)
        if batch_id:
            # Confirm it still exists before trusting it; a deleted or expired batch
            # should fall through to a fresh submit rather than hanging.
            try:
                existing = get_client().messages.batches.retrieve(batch_id)
                if progress:
                    progress(
                        f"resuming batch {batch_id[-8:]} "
                        f"({existing.processing_status}) instead of resubmitting "
                        f"{len(requests):,} requests"
                    )
            except Exception:
                if progress:
                    progress(f"stored batch {batch_id[-8:]} is gone; submitting fresh")
                batch_id = None

    if batch_id is None:
        batch_id = submit_batch(requests)
        if resume_key:
            with db.session() as conn:
                db.kv_set(conn, resume_key, batch_id)
        if progress:
            progress(f"submitted {len(requests):,} requests as batch {batch_id[-8:]}")

    wait_for_batch(batch_id, total=len(requests), progress=progress)

    results: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    for custom_id, payload, error in collect_batch(
        batch_id, model=model, tracker=tracker
    ):
        if error:
            errors[custom_id] = error
        elif payload is not None:
            results[custom_id] = payload

    # Only clear the resume point once results are safely in hand.
    if resume_key:
        with db.session() as conn:
            db.kv_delete(conn, resume_key)

    return results, errors
