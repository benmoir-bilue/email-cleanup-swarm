"""Parse unsubscribe mechanisms out of RFC 2369 / RFC 8058 headers.

`List-Unsubscribe` may carry several targets: typically one `https:` URL and one
`mailto:`. `List-Unsubscribe-Post: List-Unsubscribe=One-Click` upgrades the HTTPS
target to RFC 8058 one-click, meaning a bare POST completes the unsubscribe with no
page to render and no button to find. That's the path worth taking wherever it's
offered — no browser, no scraping, and it either works or returns an error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Header value is a comma-separated list of angle-bracketed URIs.
_URI = re.compile(r"<\s*([^>]+?)\s*>")


@dataclass(frozen=True)
class UnsubTarget:
    method: str  # one_click | http | mailto
    endpoint: str

    @property
    def is_browser_needed(self) -> bool:
        return self.method == "http"


def parse_targets(
    list_unsubscribe: str | None, list_unsubscribe_post: str | None = None
) -> list[UnsubTarget]:
    """Return every usable target, best mechanism first."""
    if not list_unsubscribe:
        return []

    uris = _URI.findall(list_unsubscribe)
    if not uris:
        # Some senders omit the angle brackets the RFC requires.
        uris = [
            part.strip()
            for part in list_unsubscribe.split(",")
            if part.strip().lower().startswith(("http", "mailto:"))
        ]

    one_click = bool(
        list_unsubscribe_post and "one-click" in list_unsubscribe_post.lower()
    )

    http_targets: list[UnsubTarget] = []
    mailto_targets: list[UnsubTarget] = []

    for uri in uris:
        lowered = uri.lower()
        if lowered.startswith("https://") or lowered.startswith("http://"):
            http_targets.append(
                UnsubTarget("one_click" if one_click else "http", uri)
            )
        elif lowered.startswith("mailto:"):
            mailto_targets.append(UnsubTarget("mailto", uri[len("mailto:") :]))

    # One-click first, then any HTTPS page, then mailto as the last resort — mailto
    # is fire-and-forget with no confirmation signal.
    ordered = [t for t in http_targets if t.method == "one_click"]
    ordered += [t for t in http_targets if t.method == "http"]
    ordered += mailto_targets
    return ordered


def best_target(
    list_unsubscribe: str | None, list_unsubscribe_post: str | None = None
) -> UnsubTarget | None:
    targets = parse_targets(list_unsubscribe, list_unsubscribe_post)
    return targets[0] if targets else None


def parse_mailto(endpoint: str) -> tuple[str, str, str]:
    """Split a mailto endpoint into (address, subject, body).

    Senders often encode the required subject line in the mailto query string —
    e.g. `unsub@example.com?subject=unsubscribe%20abc123`. Losing that means the
    unsubscribe silently doesn't register, so the query is honoured.
    """
    from urllib.parse import parse_qs, unquote, urlsplit

    if "?" not in endpoint:
        return unquote(endpoint), "unsubscribe", "unsubscribe"

    address, _, query = endpoint.partition("?")
    params = parse_qs(query)
    subject = (params.get("subject") or ["unsubscribe"])[0]
    body = (params.get("body") or ["unsubscribe"])[0]
    return unquote(address), unquote(subject), unquote(body)
