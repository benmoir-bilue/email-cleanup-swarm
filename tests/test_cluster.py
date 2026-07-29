"""Clustering is deterministic and load-bearing, so it gets real coverage."""

from __future__ import annotations

import pytest

from ecs.cluster import (
    cluster_key_for,
    normalize_list_id,
    normalize_subject,
    sender_domain,
    unsub_method,
)


class TestNormalizeSubject:
    def test_strips_reply_and_forward_chains(self):
        assert normalize_subject("Re: Fwd: RE: Project update") == "project update"

    def test_template_mail_collapses_to_one_signature(self):
        """The whole point: per-send variables must not fragment a cluster."""
        a = normalize_subject("Your order #INV-2024-0918 has shipped")
        b = normalize_subject("Your order #INV-2025-1142 has shipped")
        assert a == b
        assert a == "your order has shipped"

    def test_strips_currency_dates_and_bracket_tags(self):
        got = normalize_subject(
            "Re: [Acme] Invoice #INV-2024-0918 for $1,240.00 — due Fri 12 Sep"
        )
        # Amounts, order numbers, weekday and month names all removed.
        assert "1240" not in got
        assert "sep" not in got
        assert "fri" not in got
        assert got.startswith("invoice")

    def test_strips_emoji(self):
        assert normalize_subject("🔥 Flash sale 🔥 ends tonight") == "flash sale ends tonight"

    def test_caps_length_so_long_subjects_still_match(self):
        long_a = normalize_subject("Weekly digest one two three four five six seven")
        long_b = normalize_subject("Weekly digest one two three four five NINE TEN")
        # Only the leading tokens are kept, so tails diverging doesn't split them.
        assert long_a == long_b

    @pytest.mark.parametrize("value", [None, "", "   ", "12345", "$$$"])
    def test_degenerate_input_is_empty_not_an_error(self, value):
        assert normalize_subject(value) == ""


class TestSenderDomain:
    @pytest.mark.parametrize(
        "addr,expected",
        [
            ("news@acme.com", "acme.com"),
            ("News@ACME.COM", "acme.com"),
            ("a@mail.acme.com", "acme.com"),
            # Bulk senders rotate deep subdomains; collapse to the registrable domain.
            ("bounce@bounce.mail1.sendgrid.acme.com", "acme.com"),
            # Multi-label public suffixes must survive, or every .gov.au sender
            # collapses into one cluster and the protected-domain guard stops matching.
            ("noreply@ato.gov.au", "ato.gov.au"),
            ("x@company.com.au", "company.com.au"),
            ("a@sub.company.com.au", "company.com.au"),
            ("b@bbc.co.uk", "bbc.co.uk"),
            ("c@news.bbc.co.uk", "bbc.co.uk"),
            ("d@localhost", "localhost"),
        ],
    )
    def test_extraction(self, addr, expected):
        assert sender_domain(addr) == expected

    @pytest.mark.parametrize("value", [None, "", "not-an-address"])
    def test_missing_domain_is_none(self, value):
        assert sender_domain(value) is None


class TestNormalizeListId:
    def test_extracts_bracketed_identifier(self):
        assert normalize_list_id("Acme News <news.acme.example.com>") == (
            "news.acme.example.com"
        )

    def test_bare_value_passes_through_lowercased(self):
        assert normalize_list_id("News.Acme.Example.COM") == "news.acme.example.com"

    def test_none_and_empty(self):
        assert normalize_list_id(None) is None
        assert normalize_list_id("") is None


class TestUnsubMethod:
    def test_rfc8058_one_click_is_detected(self):
        assert (
            unsub_method(
                "<https://acme.com/u/abc>", "List-Unsubscribe=One-Click"
            )
            == "one_click"
        )

    def test_http_without_post_header_is_plain_http(self):
        assert unsub_method("<https://acme.com/u/abc>", None) == "http"

    def test_one_click_requires_an_http_endpoint(self):
        """A One-Click header with only a mailto is not actually one-click."""
        assert (
            unsub_method("<mailto:stop@acme.com>", "List-Unsubscribe=One-Click")
            == "mailto"
        )

    def test_mailto_only(self):
        assert unsub_method("<mailto:unsub@acme.com>", None) == "mailto"

    def test_absent_header(self):
        assert unsub_method(None, None) == "none"


class TestClusterKeyPrecedence:
    def test_list_id_wins_over_sender(self):
        """List-Id is stable even when the envelope sender rotates per send."""
        key_a, kind = cluster_key_for(
            list_id="news.acme.com",
            from_addr="bounce-123@acme.com",
            domain="acme.com",
            subject_norm="weekly digest",
        )
        key_b, _ = cluster_key_for(
            list_id="news.acme.com",
            from_addr="bounce-999@acme.com",
            domain="acme.com",
            subject_norm="weekly digest",
        )
        assert key_a == key_b
        assert kind == "list_id"

    def test_sender_address_used_when_no_list_id(self):
        key, kind = cluster_key_for(
            list_id=None,
            from_addr="jane@example.com",
            domain="example.com",
            subject_norm="lunch",
        )
        assert key == "addr:jane@example.com"
        assert kind == "sender"

    def test_domain_plus_subject_catches_rotating_local_parts(self):
        key_a, kind = cluster_key_for(
            list_id=None, from_addr=None, domain="acme.com", subject_norm="order shipped"
        )
        key_b, _ = cluster_key_for(
            list_id=None, from_addr=None, domain="acme.com", subject_norm="order shipped"
        )
        assert key_a == key_b == "dom:acme.com|order shipped"
        assert kind == "domain_subject"

    def test_always_returns_a_key_even_with_nothing_to_go_on(self):
        key, kind = cluster_key_for(
            list_id=None, from_addr=None, domain=None, subject_norm=""
        )
        assert key == "sig:"
        assert kind == "domain_subject"
