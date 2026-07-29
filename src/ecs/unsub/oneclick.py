"""RFC 8058 one-click unsubscribe.

The best path by a wide margin: a single HTTPS POST with a fixed body, no page to
render, no button to locate, and a real status code telling you whether it worked.
Most bulk senders now advertise it, which is why detecting it separately in
`parse.py` is worth the effort — it keeps the browser out of the loop for the
majority of the worklist.

The POST body is mandated by the RFC as exactly `List-Unsubscribe=One-Click`, form
encoded. Senders validate it, so it isn't a parameter to improvise on.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .. import config

RFC8058_BODY = "List-Unsubscribe=One-Click"

# Identify honestly. A generic browser UA on an automated POST is more likely to be
# treated as abuse than a self-describing client.
USER_AGENT = "EmailCleanupSwarm/0.1 (personal mailbox hygiene; one-click unsubscribe)"


@dataclass
class UnsubResult:
    ok: bool
    status: str  # done | failed | needs_manual
    detail: str
    http_status: int | None = None


def post_one_click(endpoint: str, *, timeout: float | None = None) -> UnsubResult:
    """Perform an RFC 8058 one-click unsubscribe."""
    timeout = timeout or config.TUNABLES.unsub_timeout_seconds

    if not endpoint.lower().startswith("https://"):
        # Refuse to send an unsubscribe over plaintext HTTP: the URL usually embeds a
        # per-recipient token, and leaking it achieves nothing useful.
        return UnsubResult(
            False,
            "needs_manual",
            f"endpoint is not HTTPS, refusing to POST: {endpoint}",
        )

    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            response = client.post(
                endpoint,
                content=RFC8058_BODY,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except httpx.TimeoutException:
        return UnsubResult(False, "failed", f"timed out after {timeout:.0f}s")
    except httpx.HTTPError as exc:
        return UnsubResult(False, "failed", f"request error: {exc}")

    if 200 <= response.status_code < 300:
        return UnsubResult(
            True, "done", "one-click POST accepted", response.status_code
        )

    if response.status_code in (405, 501):
        # Advertised one-click but doesn't actually implement POST. Fall back to the
        # browser rather than reporting a failure.
        return UnsubResult(
            False,
            "needs_manual",
            f"POST not supported (HTTP {response.status_code}); needs browser",
            response.status_code,
        )

    if response.status_code in (401, 403, 410):
        # Token expired or already unsubscribed — the latter is common and benign.
        return UnsubResult(
            False,
            "needs_manual",
            f"endpoint rejected the request (HTTP {response.status_code}); "
            "the link may have expired or already been used",
            response.status_code,
        )

    return UnsubResult(
        False, "failed", f"HTTP {response.status_code}", response.status_code
    )
