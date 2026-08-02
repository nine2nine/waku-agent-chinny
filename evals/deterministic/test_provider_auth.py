"""DETERMINISTIC EVAL — the anthropic-provider OAuth credential fallback.

get_client() now lets the "anthropic" provider run with no ANTHROPIC_API_KEY
at all, PROVIDED the anthropic SDK's own credential chain can resolve one
(ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile on disk). Every other
provider — including kimi/glm/minimax, which also speak the anthropic wire —
must keep requiring their own key; a Claude credential must never leak into
them. No network calls: default_credentials() only inspects env vars and
files, and constructing anthropic.Anthropic() makes no HTTP request.

HOME and ANTHROPIC_CONFIG_DIR are pointed at tmp_path in every test so a real
dev machine's `ant auth login` profile can never leak into the result.
"""

from __future__ import annotations

import pytest

from waku.config import Settings
from waku.loop.models import PROVIDERS, get_client


@pytest.fixture(autouse=True)
def isolated_credential_chain(monkeypatch, tmp_path):
    """No real machine credentials must ever be visible to these tests."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "anthropic-config"))
    for var in (
        "ANTHROPIC_PROFILE",
        "ANTHROPIC_IDENTITY_TOKEN",
        "ANTHROPIC_IDENTITY_TOKEN_FILE",
        "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_ORGANIZATION_ID",
        "ANTHROPIC_SERVICE_ACCOUNT_ID",
        "ANTHROPIC_WORKSPACE_ID",
        "WAKU_API_KEY",
        "WAKU_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)


def test_anthropic_no_credentials_at_all_exits_naming_both_alternatives(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    settings = Settings(provider="anthropic", model="", small_model="",
                        api_key="", base_url=None, home=tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        get_client(settings)

    message = str(exc_info.value)
    assert "ANTHROPIC_API_KEY" in message
    assert "ant auth login" in message


def test_anthropic_auth_token_resolves_without_an_api_key(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "dummy-oauth-token")
    settings = Settings(provider="anthropic", model="", small_model="",
                        api_key="", base_url=None, home=tmp_path)

    client = get_client(settings)

    # bearer path: the SDK resolved ANTHROPIC_AUTH_TOKEN itself (we never
    # passed api_key=), so auth_token is set and api_key stays None.
    assert client.auth_token == "dummy-oauth-token"
    assert client.api_key is None


def test_kimi_does_not_accept_anthropics_auth_token(monkeypatch, tmp_path):
    """A Claude OAuth credential must never leak into an anthropic-WIRE
    provider that isn't Anthropic itself — kimi still needs its own key."""
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "dummy-oauth-token")
    settings = Settings(provider="kimi", model="", small_model="",
                        api_key="", base_url=None, home=tmp_path)

    with pytest.raises(SystemExit, match="MOONSHOT_API_KEY"):
        get_client(settings)


def test_qwen_is_openai_wire_keyed_on_dashscope():
    provider = PROVIDERS["qwen"]
    assert provider.kind == "openai"
    assert provider.key_env == "DASHSCOPE_API_KEY"
