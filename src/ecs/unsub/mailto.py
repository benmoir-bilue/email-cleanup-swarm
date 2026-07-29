"""Unsubscribe-by-email (RFC 2369 `mailto:` targets).

Fire-and-forget: the send either succeeds or it doesn't, and there is no confirmation
signal afterwards. So it's the last resort, and it reports `done` meaning "the request
was sent" rather than "the sender honoured it".

The subject and body encoded in the mailto query string matter — senders routinely
require a specific token there (`?subject=unsubscribe%20abc123`), and dropping it means
the request silently doesn't register.
"""

from __future__ import annotations

from .oneclick import UnsubResult
from .parse import parse_mailto


def send_mailto_unsubscribe(endpoint: str) -> UnsubResult:
    """Send the unsubscribe email described by a mailto endpoint."""
    from ..gmail.auth import service
    from ..gmail.mutate import send_plain_message

    address, subject, body = parse_mailto(endpoint)

    if "@" not in address:
        return UnsubResult(False, "needs_manual", f"unusable mailto target: {endpoint}")

    try:
        svc = service()
        message_id = send_plain_message(svc, to=address, subject=subject, body=body)
    except Exception as exc:
        return UnsubResult(False, "failed", f"send failed: {exc}")

    return UnsubResult(
        True,
        "done",
        f"unsubscribe email sent to {address} (subject: {subject!r}, id {message_id})",
    )
