from __future__ import annotations

import base64

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from gg_forge_kit.github import GitHubAuth, GitHubAuthError, normalize_private_key


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return pem, key.public_key()


class FakeGitHub:
    def __init__(self, public_key, clock=None):
        self.public_key = public_key
        self.clock = clock
        self.calls = []
        self.minted = 0

    async def __call__(self, method, url, headers, body):
        claims = jwt.decode(headers["Authorization"].split()[1], self.public_key, algorithms=["RS256"],
                            options={"verify_exp": False, "verify_iat": False})  # reloj simulado
        assert claims["iss"] == "123" and claims["exp"] - claims["iat"] <= 600
        self.calls.append((method, url, body))
        if url.endswith("/orgs/GG-Forge-Dev/installation"):
            return 200, {"id": 42}
        if url.endswith("/app/installations/42/access_tokens"):
            self.minted += 1
            from datetime import datetime, timezone
            expires = datetime.fromtimestamp(self.clock() + 3600, timezone.utc).isoformat().replace("+00:00", "Z")
            return 201, {"token": f"ghs_{self.minted}", "expires_at": expires}  # como GitHub: dura 1 h
        return 404, {"message": "Not Found"}


def epoch(iso):
    from datetime import datetime
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


async def test_app_token_is_scoped_cached_and_renewed_before_expiry(keypair):
    pem, public = keypair
    now = [epoch("2026-10-02T12:00:00Z")]
    gh = FakeGitHub(public, clock=lambda: now[0])
    auth = GitHubAuth(app_id="123", private_key=pem, repositories=["GG-Forge-Dev/gg-forge-servidor"],
                      permissions={"contents": "write"}, request=gh, clock=lambda: now[0])
    assert auth.kind == "app"
    assert await auth.token() == "ghs_1"
    assert gh.calls[1] == ("POST", "https://api.github.com/app/installations/42/access_tokens",
                           {"repositories": ["gg-forge-servidor"], "permissions": {"contents": "write"}})
    now[0] += 50 * 60
    assert await auth.token() == "ghs_1"  # quedan 10 min: sirve
    now[0] += 6 * 60
    assert await auth.token() == "ghs_2"  # quedan 4 min: se renueva
    assert (await auth.headers())["Authorization"] == "Bearer ghs_2"
    assert auth.secrets() == ["ghs_2"]


async def test_not_installed_is_a_clear_error(keypair):
    pem, public = keypair

    async def nothing(method, url, headers, body):
        return 404, {"message": "Not Found"}

    with pytest.raises(GitHubAuthError, match="no está instalada en GG-Forge-Dev"):
        await GitHubAuth(app_id="123", private_key=pem, request=nothing).token()


async def test_static_token_fallback_and_from_env(monkeypatch, keypair):
    for name in ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert GitHubAuth.from_env() is None
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_x")
    auth = GitHubAuth.from_env()
    assert auth.kind == "token" and await auth.token() == "github_pat_x"
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", keypair[0])
    assert GitHubAuth.from_env().kind == "app"  # la App manda sobre el token fijo


def test_private_key_formats(keypair):
    pem = keypair[0]
    assert normalize_private_key(pem) == pem.strip() + "\n"
    assert normalize_private_key(pem.strip().replace("\n", "\n")) == pem.strip() + "\n"
    assert normalize_private_key(base64.b64encode(pem.encode()).decode()) == pem.strip() + "\n"
    with pytest.raises(GitHubAuthError):
        normalize_private_key("no-es-una-clave")
