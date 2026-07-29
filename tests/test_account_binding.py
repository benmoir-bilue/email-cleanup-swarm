"""Account binding.

This tool is aimed at a personal mailbox, but a Google consent screen hands over
whichever account the browser is signed into. On a machine with both a personal and a
work Google account, authorising the wrong one and then running `ecs apply` would file
and trash thousands of work emails. These tests pin the guard that prevents it.
"""

from __future__ import annotations

import pytest

from ecs import config, db
from ecs.gmail import auth


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "bind.db")
    monkeypatch.setattr(config, "TOKEN_PATH", tmp_path / "token.json")
    return tmp_path


def fake_profile(monkeypatch, email: str) -> None:
    monkeypatch.setattr(auth, "whoami", lambda creds=None: {"emailAddress": email})


class TestBinding:
    def test_unbound_by_default(self, isolated):
        assert auth.bound_account() is None

    def test_bind_then_read_back(self, isolated):
        auth.bind_account("me@personal.example.com")
        assert auth.bound_account() == "me@personal.example.com"

    def test_unbind(self, isolated):
        auth.bind_account("me@personal.example.com")
        auth.unbind_account()
        assert auth.bound_account() is None


class TestAssertAccount:
    def test_binds_on_first_use_when_unbound(self, isolated, monkeypatch):
        """A directory created before this guard existed shouldn't stay unguarded."""
        fake_profile(monkeypatch, "me@personal.example.com")
        assert auth.assert_account() == "me@personal.example.com"
        assert auth.bound_account() == "me@personal.example.com"

    def test_passes_when_the_account_matches(self, isolated, monkeypatch):
        auth.bind_account("me@personal.example.com")
        fake_profile(monkeypatch, "me@personal.example.com")
        assert auth.assert_account() == "me@personal.example.com"

    def test_match_is_case_insensitive(self, isolated, monkeypatch):
        auth.bind_account("Me@Personal.Example.com")
        fake_profile(monkeypatch, "me@personal.example.com")
        assert auth.assert_account()

    def test_raises_on_the_work_account(self, isolated, monkeypatch):
        """The failure this whole mechanism exists to prevent."""
        auth.bind_account("me@personal.example.com")
        fake_profile(monkeypatch, "me@work.example.com")

        with pytest.raises(auth.WrongAccountError) as excinfo:
            auth.assert_account()

        message = str(excinfo.value)
        # The error has to name both addresses, or it's not actionable.
        assert "me@personal.example.com" in message
        assert "me@work.example.com" in message
        assert "Refusing" in message

    def test_a_mismatch_does_not_silently_rebind(self, isolated, monkeypatch):
        auth.bind_account("me@personal.example.com")
        fake_profile(monkeypatch, "other@work.example.com")
        with pytest.raises(auth.WrongAccountError):
            auth.assert_account()
        # Still bound to the original; switching must be deliberate.
        assert auth.bound_account() == "me@personal.example.com"


class TestTokenIsolation:
    def test_token_path_is_this_app_only(self):
        """Never read or write the gmail-mcp server's credentials."""
        token = str(config.TOKEN_PATH)
        assert "email-cleanup-swarm" in token
        assert ".gmail-mcp" not in token

    def test_no_module_references_the_gmail_mcp_directory(self):
        import pathlib

        src = pathlib.Path(__file__).resolve().parents[1] / "src" / "ecs"
        offenders = [
            path.name
            for path in src.rglob("*.py")
            if "gmail-mcp" in path.read_text(encoding="utf-8")
        ]
        assert offenders == []
