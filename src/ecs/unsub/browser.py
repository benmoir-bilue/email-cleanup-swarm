"""Browser-driven unsubscribe, for senders that only offer a web page.

Runs **headed** by default so you can watch it work and take over when a page does
something unexpected — a CAPTCHA, a login wall, a multi-step preference centre. An
unsubscribe flow that fails silently in a headless browser is worse than one that
visibly stops and asks for help.

Safety choices worth noting:

  * Only URLs from `List-Unsubscribe` headers are visited, never links scraped from
    message bodies. Header targets are what the sending infrastructure declares; body
    links are attacker-controllable content.
  * A fresh, isolated browser context per target: no shared cookies, no access to
    existing sessions, downloads refused.
  * A screenshot of the final state is saved for every attempt, so the outcome is
    auditable rather than a bare "done".
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from .. import config
from .oneclick import UnsubResult

# Ordered by how strongly the text implies "this completes the unsubscribe".
_CONFIRM_PATTERNS = [
    re.compile(r"^\s*(?:confirm|yes,?\s*unsubscribe|unsubscribe)\s*$", re.IGNORECASE),
    re.compile(r"unsubscribe\s+(?:me|all|from\s+all)", re.IGNORECASE),
    re.compile(r"\b(?:confirm|yes)\b.*\b(?:unsubscribe|opt[-\s]?out|remove)\b", re.IGNORECASE),
    re.compile(r"\b(?:opt[-\s]?out|remove\s+me)\b", re.IGNORECASE),
    re.compile(r"\bunsubscribe\b", re.IGNORECASE),
]

# Text implying the unsubscribe already succeeded without interaction. Many one-click
# links land straight on a confirmation page.
_SUCCESS_PATTERNS = re.compile(
    r"(?:you\s+(?:have\s+been|are)\s+(?:now\s+)?unsubscribed"
    r"|successfully\s+unsubscribed"
    r"|unsubscribe\s+(?:successful|complete|confirmed)"
    r"|you'?re\s+unsubscribed"
    r"|removed\s+from\s+(?:our|the)\s+(?:list|mailing)"
    r"|no\s+longer\s+receive)",
    re.IGNORECASE,
)

# Signals a human has to intervene; clicking blindly won't help.
_BLOCKED_PATTERNS = re.compile(
    r"(?:captcha|recaptcha|are\s+you\s+a\s+robot"
    r"|sign\s+in|log\s?in\s+to|enter\s+your\s+password"
    r"|verify\s+your\s+identity)",
    re.IGNORECASE,
)


def _evidence_path(cluster_key: str, suffix: str = "png") -> Path:
    config.ensure_dirs()
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", cluster_key)[:80]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return config.UNSUB_EVIDENCE_DIR / f"{stamp}_{safe}.{suffix}"


class BrowserUnsubscriber:
    """Reuses one browser across many targets; launching per target is slow."""

    def __init__(self, *, headed: bool = True, timeout_ms: int = 20000):
        self.headed = headed
        self.timeout_ms = timeout_ms
        self._playwright = None
        self._browser = None

    def __enter__(self) -> BrowserUnsubscriber:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # optional dependency
            raise RuntimeError(
                "Playwright is not installed. Run:\n"
                "  uv sync --extra browser\n"
                "  uv run playwright install chromium"
            ) from exc

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=not self.headed)
        return self

    def __exit__(self, *exc_info) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()

    def unsubscribe(self, cluster_key: str, endpoint: str) -> UnsubResult:
        """Visit an unsubscribe page and try to complete the flow."""
        if self._browser is None:
            raise RuntimeError("use BrowserUnsubscriber as a context manager")

        # Fresh context per target: no cookie sharing between senders, and no access
        # to any existing browser session.
        context = self._browser.new_context(
            accept_downloads=False,
            java_script_enabled=True,
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        page.set_default_timeout(self.timeout_ms)
        shot = _evidence_path(cluster_key)

        try:
            try:
                page.goto(endpoint, wait_until="domcontentloaded")
            except Exception as exc:
                return UnsubResult(False, "failed", f"could not load page: {exc}")

            body_text = self._safe_text(page)

            if _BLOCKED_PATTERNS.search(body_text):
                page.screenshot(path=str(shot), full_page=True)
                return UnsubResult(
                    False,
                    "needs_manual",
                    "page requires sign-in or CAPTCHA — finish this one by hand",
                )

            # Many links complete on load; no click needed.
            if _SUCCESS_PATTERNS.search(body_text):
                page.screenshot(path=str(shot), full_page=True)
                return UnsubResult(
                    True, "done", f"already confirmed on load (evidence: {shot.name})"
                )

            clicked = self._click_confirm(page)
            if not clicked:
                page.screenshot(path=str(shot), full_page=True)
                return UnsubResult(
                    False,
                    "needs_manual",
                    f"no unsubscribe control found (evidence: {shot.name})",
                )

            # Give the page a moment to settle, then check for confirmation.
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass  # single-page apps may never go idle; the text check still works

            after_text = self._safe_text(page)
            page.screenshot(path=str(shot), full_page=True)

            if _SUCCESS_PATTERNS.search(after_text):
                return UnsubResult(
                    True, "done", f"confirmed after click (evidence: {shot.name})"
                )

            # Clicked something, but the page never said it worked. Report honestly
            # rather than claiming success.
            return UnsubResult(
                False,
                "needs_manual",
                f"clicked the control but saw no confirmation (evidence: {shot.name})",
            )
        finally:
            context.close()

    @staticmethod
    def _safe_text(page) -> str:
        try:
            return page.inner_text("body")[:20000]
        except Exception:
            return ""

    def _click_confirm(self, page) -> bool:
        """Find and click the most plausible confirmation control."""
        selectors = [
            "button",
            "input[type=submit]",
            "input[type=button]",
            "a[href]",
            "[role=button]",
        ]
        for pattern in _CONFIRM_PATTERNS:
            for selector in selectors:
                try:
                    elements = page.query_selector_all(selector)
                except Exception:
                    continue
                for element in elements:
                    label = self._element_label(element)
                    if not label or not pattern.search(label):
                        continue
                    try:
                        if not element.is_visible():
                            continue
                        element.click(timeout=5000)
                        return True
                    except Exception:
                        continue
        return False

    @staticmethod
    def _element_label(element) -> str:
        try:
            text = (element.inner_text() or "").strip()
            if text:
                return text
            for attr in ("value", "aria-label", "title"):
                value = element.get_attribute(attr)
                if value:
                    return value.strip()
        except Exception:
            return ""
        return ""
