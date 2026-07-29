"""OAuth for a personal Gmail account.

Installed-app flow over a loopback redirect: `ecs auth` opens a browser, you grant
consent, and a refresh token lands in the config dir at mode 0600.

The requested scopes are deliberately narrow. `gmail.modify` can label, archive,
and trash; `gmail.settings.basic` can create filters. Neither can permanently
delete — that needs `https://mail.google.com/`, which this app never asks for. So
even a total logic failure cannot destroy mail beyond Trash's 30-day window.
"""

from __future__ import annotations

import json
import os
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build

from .. import config


def _load_stored() -> Credentials | None:
    """Return stored credentials, or None if absent or scope-insufficient."""
    if not config.TOKEN_PATH.is_file():
        return None
    try:
        creds = Credentials.from_authorized_user_file(
            str(config.TOKEN_PATH), config.SCOPES
        )
    except (ValueError, json.JSONDecodeError):
        return None

    # A token minted before a scope was added will authenticate fine but 403 on
    # first use. Catch that here rather than mid-run.
    granted = set(creds.scopes or [])
    if not set(config.SCOPES).issubset(granted):
        missing = sorted(set(config.SCOPES) - granted)
        print(f"Stored token is missing scopes {missing}; re-running consent flow.")
        return None
    return creds


def _persist(creds: Credentials) -> None:
    config.ensure_dirs()
    config.TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(config.TOKEN_PATH, 0o600)


def authorise(*, force: bool = False) -> Credentials:
    """Return usable credentials, refreshing or running consent as needed."""
    creds = None if force else _load_stored()

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _persist(creds)
            return creds
        except Exception as exc:  # refresh tokens do eventually hard-expire
            print(f"Token refresh failed ({exc}); re-running consent flow.")

    flow = InstalledAppFlow.from_client_secrets_file(
        str(config.client_secret_path()), config.SCOPES
    )
    # port=0 lets the OS pick a free port for the loopback redirect.
    creds = flow.run_local_server(
        port=0,
        prompt="consent",  # forces a refresh_token to be issued
        authorization_prompt_message=(
            "Opening a browser to authorise Email Cleanup Swarm.\n"
            "Grant access to the PERSONAL Gmail account you want cleaned up.\n"
            "If the browser doesn't open, visit:\n{url}"
        ),
        success_message=(
            "Authorised. You can close this tab and return to the terminal."
        ),
    )
    _persist(creds)
    return creds


def service(creds: Credentials | None = None) -> Resource:
    """Build a Gmail API client.

    cache_discovery=False avoids the noisy oauth2client cache warning and a stale
    discovery doc on disk.
    """
    creds = creds or authorise()
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def whoami(creds: Credentials | None = None) -> dict[str, Any]:
    """Fetch the authenticated profile — the round-trip check for `ecs auth`."""
    return service(creds).users().getProfile(userId="me").execute()


# ---------------------------------------------------------------------------
# Account binding
#
# This tool is pointed at a *personal* mailbox, but a Google consent screen will
# happily hand over whichever account the browser happens to be signed into. On a
# machine with both a personal and a work account that is a genuinely dangerous
# ambiguity: authorise the wrong one and the next `ecs apply` files and trashes
# thousands of work emails instead.
#
# So the first successful `ecs auth` records the address it authorised, and every
# command that touches the mailbox refuses to run if the live token resolves to a
# different account. Changing accounts then has to be deliberate (`--rebind`)
# rather than accidental.
# ---------------------------------------------------------------------------

ACCOUNT_KEY = "account.email"


class WrongAccountError(RuntimeError):
    """The authorised mailbox is not the one this run is bound to."""


def bound_account() -> str | None:
    """The address this working directory is bound to, if any."""
    from .. import db

    with db.session() as conn:
        return db.kv_get(conn, ACCOUNT_KEY)


def bind_account(email: str) -> None:
    from .. import db

    with db.session() as conn:
        db.kv_set(conn, ACCOUNT_KEY, email)


def unbind_account() -> None:
    from .. import db

    with db.session() as conn:
        db.kv_delete(conn, ACCOUNT_KEY)


def assert_account() -> str:
    """Verify the live token matches the bound account. Call before any mailbox work.

    Returns the confirmed address. Raises `WrongAccountError` on mismatch, which the
    CLI turns into a hard stop rather than a warning — a mismatch here means we are
    about to operate on the wrong mailbox.
    """
    expected = bound_account()
    actual = whoami().get("emailAddress", "")

    if expected is None:
        # Never bound (e.g. `ecs auth` predates this check). Bind now rather than
        # leaving the run unguarded.
        bind_account(actual)
        return actual

    if actual.lower() != expected.lower():
        raise WrongAccountError(
            f"This working directory is bound to {expected}, but the stored token "
            f"authorises {actual}.\n\n"
            "Refusing to touch the wrong mailbox. Either:\n"
            "  • run `ecs auth --reauth` and sign in as "
            f"{expected}, or\n"
            "  • if you genuinely want to switch accounts, start a clean working "
            "directory (the index and journal belong to the old account), or\n"
            "  • run `ecs auth --reauth --rebind` to deliberately re-point this "
            "directory at a different mailbox."
        )
    return actual
