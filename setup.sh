#!/usr/bin/env bash
# One-shot setup for email-cleanup-swarm. Safe to re-run.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
info() { printf '  %s\n' "$1"; }
warn() { printf '\033[33m  %s\033[0m\n' "$1"; }
ok()   { printf '\033[32m✓ %s\033[0m\n' "$1"; }

bold "email-cleanup-swarm setup"
echo

# --- uv -----------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
    warn "uv is not installed."
    info "Install it, then re-run this script:"
    info "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi
ok "uv found ($(uv --version))"

# --- Python dependencies --------------------------------------------------
bold "Installing dependencies..."
uv sync
ok "Core dependencies installed"

echo
read -r -p "Install the browser-unsubscribe extra (Playwright + Chromium, ~200MB)? [y/N] " reply
if [[ "$reply" =~ ^[Yy]$ ]]; then
    uv sync --extra browser
    uv run playwright install chromium
    ok "Browser unsubscribe support installed"
else
    info "Skipped — you can add it later with:"
    info "  uv sync --extra browser && uv run playwright install chromium"
fi

# --- .env -----------------------------------------------------------------
echo
if [[ -f .env ]]; then
    ok ".env already exists — leaving it alone"
else
    cp .env.example .env
    ok "Created .env from .env.example"
fi

# --- filing-rules.toml ------------------------------------------------------
if [[ -f filing-rules.toml ]]; then
    ok "filing-rules.toml already exists — leaving it alone"
else
    info "No filing-rules.toml yet — this is optional, skipping."
    info "See filing-rules.example.toml if you want to add your own rules later."
fi

# --- Next steps -------------------------------------------------------------
echo
bold "Next steps"
cat <<'EOF'

1. Create a Google OAuth client (Google Cloud Console):
   - console.cloud.google.com -> new/select project -> "APIs & Services"
   - Enable the "Gmail API"
   - Configure the OAuth consent screen (External, Testing mode is fine for
     personal use — add your own Google account as a test user)
   - Credentials -> Create Credentials -> OAuth client ID -> type "Desktop app"
   - Download the client secret JSON

2. Edit .env:
   - Set ECS_CLIENT_SECRET to the path of the JSON file you just downloaded
   - Set ANTHROPIC_API_KEY (console.anthropic.com/settings/keys), or leave it
     unset if you already run `claude auth login` / `ant auth login` locally

3. Authorize and bind your mailbox:
     uv run ecs auth --expect you@gmail.com

4. See README.md "Usage" for the full pipeline (index -> cluster -> guards ->
   analyze -> plan -> review -> apply).

EOF
ok "Setup complete"
