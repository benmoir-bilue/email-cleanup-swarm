"""Unsubscribe header parsing and one-click behaviour.

Unsubscribing is the only irreversible action in the system, so the mechanism
selection is worth pinning precisely.
"""

from __future__ import annotations

import httpx
import pytest

from ecs.unsub.oneclick import RFC8058_BODY, post_one_click
from ecs.unsub.parse import best_target, parse_mailto, parse_targets


class TestParseTargets:
    def test_one_click_is_preferred_over_mailto(self):
        targets = parse_targets(
            "<https://acme.com/u/abc>, <mailto:stop@acme.com>",
            "List-Unsubscribe=One-Click",
        )
        assert [t.method for t in targets] == ["one_click", "mailto"]
        assert targets[0].endpoint == "https://acme.com/u/abc"

    def test_http_ranks_above_mailto_without_one_click(self):
        targets = parse_targets("<mailto:stop@acme.com>, <https://acme.com/u/abc>")
        assert [t.method for t in targets] == ["http", "mailto"]

    def test_handles_senders_that_omit_required_angle_brackets(self):
        targets = parse_targets("https://acme.com/u/abc, mailto:stop@acme.com")
        assert [t.method for t in targets] == ["http", "mailto"]

    def test_mailto_endpoint_strips_the_scheme(self):
        (target,) = parse_targets("<mailto:stop@acme.com>")
        assert target.endpoint == "stop@acme.com"

    def test_browser_is_only_needed_for_plain_http(self):
        one_click = parse_targets("<https://a.com/u>", "List-Unsubscribe=One-Click")[0]
        plain = parse_targets("<https://a.com/u>")[0]
        assert one_click.is_browser_needed is False
        assert plain.is_browser_needed is True

    @pytest.mark.parametrize("value", [None, "", "not a uri"])
    def test_unusable_headers_yield_nothing(self, value):
        assert parse_targets(value) == []
        assert best_target(value) is None


class TestParseMailto:
    def test_bare_address(self):
        assert parse_mailto("stop@acme.com") == (
            "stop@acme.com", "unsubscribe", "unsubscribe"
        )

    def test_required_token_in_the_query_is_preserved(self):
        """Dropping the sender's token means the request silently doesn't register."""
        address, subject, body = parse_mailto(
            "unsub@acme.com?subject=unsubscribe%20token-abc123"
        )
        assert address == "unsub@acme.com"
        assert subject == "unsubscribe token-abc123"

    def test_body_parameter_is_honoured(self):
        _, _, body = parse_mailto("u@acme.com?body=REMOVE%20me")
        assert body == "REMOVE me"


class TestOneClick:
    def test_refuses_plaintext_http(self):
        """The URL embeds a per-recipient token; don't leak it over cleartext."""
        result = post_one_click("http://acme.com/u/abc")
        assert result.ok is False
        assert result.status == "needs_manual"
        assert "HTTPS" in result.detail

    def test_success_posts_the_rfc_mandated_body(self, monkeypatch):
        captured = {}

        class FakeResponse:
            status_code = 200

        class FakeClient:
            def __init__(self, **kwargs):
                captured["client_kwargs"] = kwargs

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, content=None, headers=None):
                captured["url"] = url
                captured["content"] = content
                captured["headers"] = headers
                return FakeResponse()

        monkeypatch.setattr(httpx, "Client", FakeClient)
        result = post_one_click("https://acme.com/u/abc")

        assert result.ok is True
        assert result.status == "done"
        # Senders validate this body verbatim.
        assert captured["content"] == RFC8058_BODY
        assert captured["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
        assert captured["client_kwargs"]["follow_redirects"] is True

    @pytest.mark.parametrize(
        "status,expected",
        [
            (405, "needs_manual"),  # advertised one-click, didn't implement POST
            (501, "needs_manual"),
            (403, "needs_manual"),  # expired or already-used token
            (410, "needs_manual"),
            (500, "failed"),
            (502, "failed"),
        ],
    )
    def test_status_codes_route_to_the_right_outcome(self, monkeypatch, status, expected):
        class FakeResponse:
            status_code = status

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **kw):
                return FakeResponse()

        monkeypatch.setattr(httpx, "Client", FakeClient)
        result = post_one_click("https://acme.com/u/abc")
        assert result.ok is False
        assert result.status == expected

    def test_timeout_is_reported_as_failed_not_manual(self, monkeypatch):
        class FakeClient:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **kw):
                raise httpx.TimeoutException("too slow")

        monkeypatch.setattr(httpx, "Client", FakeClient)
        result = post_one_click("https://acme.com/u/abc")
        assert result.status == "failed"
        assert "timed out" in result.detail
