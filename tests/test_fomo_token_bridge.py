import subprocess
from pathlib import Path

import pytest

from app.core.config import settings
from app.main import fomo_bridge_origins

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fomo-token.sh"


# ---- the loopback CORS gate ------------------------------------------------


def test_bridge_off_allows_no_cross_origin(monkeypatch):
    monkeypatch.setattr(settings, "fomo_token_bridge", False)
    assert fomo_bridge_origins() == []


def test_bridge_on_allows_only_fomo_family(monkeypatch):
    monkeypatch.setattr(settings, "fomo_token_bridge", True)
    assert fomo_bridge_origins() == ["https://fomo.family"]


# ---- the emitted console snippet -------------------------------------------


@pytest.fixture(scope="module")
def snippet() -> str:
    out = subprocess.run(
        [str(SCRIPT), "--print", "--base", "http://127.0.0.1:8123"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


def test_snippet_targets_the_configured_api_base(snippet):
    assert "http://127.0.0.1:8123/api/fomo/session" in snippet
    assert "__BASE__" not in snippet  # placeholder fully substituted


def test_manual_snippet_does_not_rotate_privy_refresh_tokens(snippet):
    assert "privy:token" in snippet
    assert "privy:refresh_token" not in snippet
    assert "auth.privy.io" not in snippet


def test_snippet_falls_back_to_prompt_then_clipboard(snippet):
    assert "prompt(" in snippet  # still works if refresh is unavailable
    assert "clipboard.writeText(jwt)" in snippet  # push blocked -> copy for paste


def test_snippet_keeps_its_russian_strings_readable(snippet):
    # The console messages are Russian like the rest of the page; a mangled
    # heredoc would show up here before it ever reached a browser console.
    assert "Вставьте FOMO Bearer-токен" in snippet
    assert "сессия передана в localhost" in snippet


def test_clipboard_copy_pins_utf8():
    # pbcopy re-encodes per LC_CTYPE, and the default "C" locale turns those
    # Russian strings into Mac OS Roman mojibake in the clipboard.
    assert "LC_CTYPE=UTF-8 pbcopy" in SCRIPT.read_text(encoding="utf-8")


def test_snippet_never_logs_or_embeds_the_token(snippet):
    # The token is only ever posted or copied, never printed.
    assert "console.log(jwt" not in snippet
    assert "console.warn(jwt" not in snippet
    # Nothing token-shaped is baked into the script itself.
    assert "eyJ" not in snippet
