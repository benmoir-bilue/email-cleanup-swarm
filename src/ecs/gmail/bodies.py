"""Selective message-body fetch.

Bodies are the expensive resource in this system — in Gmail quota, in tokens, and in
privacy surface. So they're fetched only for the small subset of messages that reach
escalation, stored in their own table, and truncated to the amount actually needed to
classify a message rather than the whole thing.
"""

from __future__ import annotations

import base64
import binascii
import re
from datetime import UTC, datetime

from .. import config, db

# Enough to classify. A promotional email's first two paragraphs settle it; a receipt
# shows its vendor and amount up top. Full bodies would multiply token cost for very
# little classification gain.
MAX_BODY_CHARS = 2500

_SCRIPT_STYLE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_TAG = re.compile(r"<[^>]+>")
_ENTITY = re.compile(r"&(?:nbsp|amp|lt|gt|quot|#39|apos);")
_ENTITIES = {
    "&nbsp;": " ",
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&apos;": "'",
}
_BLANKS = re.compile(r"\n{3,}")
_SPACES = re.compile(r"[ \t]{2,}")


def _decode(data: str | None) -> str:
    """Decode Gmail's base64url body payload."""
    if not data:
        return ""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return ""
    return raw.decode("utf-8", errors="replace")


def html_to_text(html: str) -> str:
    """Crude but adequate HTML flattening.

    A real parser would be better, but marketing HTML is deliberately hostile and
    this only needs to yield enough readable text to classify the message.
    """
    text = _SCRIPT_STYLE.sub(" ", html)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|tr|h[1-6]|li)>", "\n", text, flags=re.IGNORECASE)
    text = _TAG.sub(" ", text)
    text = _ENTITY.sub(lambda m: _ENTITIES.get(m.group(0), " "), text)
    text = _SPACES.sub(" ", text)
    return _BLANKS.sub("\n\n", text).strip()


def extract_body(payload: dict) -> str:
    """Pull the best available text from a Gmail payload tree.

    Prefers text/plain; falls back to flattened text/html. Skips attachments — their
    content isn't reachable from a `format=full` response anyway.
    """
    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict) -> None:
        mime = (part.get("mimeType") or "").lower()
        body = part.get("body") or {}
        if part.get("filename"):
            return  # attachment part; nothing inline to read
        if mime == "text/plain":
            plain.append(_decode(body.get("data")))
        elif mime == "text/html":
            html.append(_decode(body.get("data")))
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)

    if any(p.strip() for p in plain):
        text = "\n".join(plain)
    else:
        text = html_to_text("\n".join(html))

    text = _BLANKS.sub("\n\n", text).strip()
    return text[:MAX_BODY_CHARS]


def fetch_bodies(message_ids: list[str], *, progress=None) -> dict[str, int]:
    """Fetch and store bodies for the given ids, skipping any already stored."""
    from .auth import service
    from .index import _with_backoff

    emit = progress or (lambda _: None)

    with db.session() as conn:
        have = {
            r["message_id"]
            for r in conn.execute("SELECT message_id FROM bodies").fetchall()
        }
    todo = [m for m in message_ids if m not in have]
    if not todo:
        emit("all bodies already cached")
        return {"fetched": 0, "cached": len(message_ids), "failed": 0}

    svc = service()
    size = config.METADATA_BATCH_SIZE
    fetched = 0
    failed = 0

    for start in range(0, len(todo), size):
        chunk = todo[start : start + size]
        rows: list[tuple[str, str, str]] = []
        errors: list[str] = []
        now = datetime.now(UTC).isoformat()

        def callback(request_id: str, response: dict, exception: Exception | None) -> None:
            if exception is not None or not response:
                errors.append(request_id)
                return
            try:
                text = extract_body(response.get("payload", {}) or {})
            except Exception:
                errors.append(request_id)
                return
            rows.append((request_id, text, now))

        batch = svc.new_batch_http_request(callback=callback)
        for mid in chunk:
            batch.add(
                svc.users().messages().get(userId="me", id=mid, format="full"),
                request_id=mid,
            )
        _with_backoff(batch.execute)

        with db.session() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO bodies(message_id, text, fetched_at) "
                "VALUES(?,?,?)",
                rows,
            )
        fetched += len(rows)
        failed += len(errors)
        emit(f"bodies {fetched:,}/{len(todo):,}")

    return {"fetched": fetched, "cached": len(have), "failed": failed}
