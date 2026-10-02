"""Credenciales de GitHub para los bots: una GitHub App (tokens de 1 hora que se renuevan solos) o, mientras
se migra, un token fijo.

Con `GITHUB_APP_ID` y `GITHUB_APP_PRIVATE_KEY` el bot firma un JWT con la clave de la App y pide a GitHub un
token de instalación que caduca en una hora; se renueva solo unos minutos antes. Cada bot pide **solo sus
repos y permisos** (`repositories`, `permissions`), aunque la App tenga más: si un token se filtra, sirve
para poco y durante poco. Sin esas variables usa `GITHUB_TOKEN` tal cual, para no romper nada en el cambio.

Requiere el extra `[github]` (PyJWT con criptografía).
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Mapping, Sequence
from typing import Any, Awaitable, Callable

import aiohttp

from gg_forge_kit.config import optional_env

API = "https://api.github.com"
DEFAULT_OWNER = "GG-Forge-Dev"
# Se renueva con margen: una petición larga no debe empezar con un token a punto de caducar.
REFRESH_MARGIN_S = 5 * 60
_TIMEOUT = aiohttp.ClientTimeout(total=20)

# (método, url, headers, json) -> (status, cuerpo JSON). Se inyecta en los tests.
RequestFn = Callable[[str, str, dict[str, str], Any], Awaitable[tuple[int, Any]]]


class GitHubAuthError(RuntimeError):
    pass


async def _request(method: str, url: str, headers: dict[str, str], body: Any = None) -> tuple[int, Any]:
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async with session.request(method, url, headers=headers, json=body) as resp:
            text = await resp.text()
            try:
                return resp.status, json.loads(text) if text else None
            except ValueError:
                return resp.status, text


def normalize_private_key(raw: str) -> str:
    """Acepta el .pem tal cual, con los saltos de línea escritos como «\\n» (variables de una sola línea) o
    en base64."""
    key = raw.strip()
    if "\\n" in key and "\n" not in key:
        key = key.replace("\\n", "\n")
    if not key.startswith("-----BEGIN"):
        try:
            key = base64.b64decode(key, validate=True).decode("utf-8").strip()
        except (ValueError, UnicodeDecodeError):
            raise GitHubAuthError("GITHUB_APP_PRIVATE_KEY no es un .pem ni un .pem en base64") from None
    return key + "\n"


class GitHubAuth:
    """`await auth.token()` devuelve un token válido; `await auth.headers()` las cabeceras para la API."""

    def __init__(
        self,
        *,
        app_id: str | None = None,
        private_key: str | None = None,
        installation_id: str | None = None,
        owner: str = DEFAULT_OWNER,
        repositories: Sequence[str] | None = None,
        permissions: Mapping[str, str] | None = None,
        static_token: str | None = None,
        request: RequestFn = _request,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not static_token and not (app_id and private_key):
            raise GitHubAuthError("Hace falta GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY, o GITHUB_TOKEN")
        self.app_id = app_id
        self._key = normalize_private_key(private_key) if private_key else None
        self.installation_id = installation_id
        self.owner = owner
        self.repositories = list(repositories) if repositories else None
        self.permissions = dict(permissions) if permissions else None
        self._static = static_token
        self._request = request
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    @classmethod
    def from_env(cls, *, repositories: Sequence[str] | None = None,
                 permissions: Mapping[str, str] | None = None) -> GitHubAuth | None:
        """La App si está configurada; si no, `GITHUB_TOKEN`; si no hay nada, None."""
        app_id, key = optional_env("GITHUB_APP_ID"), optional_env("GITHUB_APP_PRIVATE_KEY")
        if app_id and key:
            return cls(app_id=app_id, private_key=key, installation_id=optional_env("GITHUB_APP_INSTALLATION_ID"),
                       owner=optional_env("GITHUB_APP_OWNER", DEFAULT_OWNER) or DEFAULT_OWNER,
                       repositories=repositories, permissions=permissions)
        token = optional_env("GITHUB_TOKEN")
        return cls(static_token=token) if token else None

    @property
    def kind(self) -> str:
        """«app» o «token» (fijo), para mostrarlo en los estados de los bots."""
        return "token" if self._static else "app"

    def secrets(self) -> list[str]:
        """Lo que no debe aparecer nunca en un log ni en Discord (para los sanitizadores)."""
        return [s for s in (self._static, self._token) if s]

    async def token(self) -> str:
        if self._static:
            return self._static
        async with self._lock:
            if self._token is None or self._clock() >= self._expires_at - REFRESH_MARGIN_S:
                await self._mint()
            assert self._token is not None
            return self._token

    async def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self.token()}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    # ─── Internos ─────────────────────────────────────────────────────────────

    def _jwt(self) -> str:
        import jwt  # PyJWT, del extra [github]

        now = int(self._clock())
        # 60 s hacia atrás por si el reloj de GitHub va por delante; GitHub acepta como mucho 10 min.
        return jwt.encode({"iat": now - 60, "exp": now + 9 * 60, "iss": str(self.app_id)}, self._key, algorithm="RS256")

    def _app_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._jwt()}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    async def _find_installation(self) -> str:
        status, body = await self._request("GET", f"{API}/orgs/{self.owner}/installation", self._app_headers(), None)
        if status == 404:  # no es una organización: puede ser una cuenta personal
            status, body = await self._request("GET", f"{API}/users/{self.owner}/installation", self._app_headers(), None)
        if status >= 400 or not isinstance(body, dict) or "id" not in body:
            raise GitHubAuthError(f"La GitHub App no está instalada en {self.owner} ({status})")
        return str(body["id"])

    async def _mint(self) -> None:
        if not self.installation_id:
            self.installation_id = await self._find_installation()
        payload: dict[str, Any] = {}
        if self.repositories:
            payload["repositories"] = [r.split("/", 1)[-1] for r in self.repositories]
        if self.permissions:
            payload["permissions"] = self.permissions
        status, body = await self._request(
            "POST", f"{API}/app/installations/{self.installation_id}/access_tokens", self._app_headers(), payload or None)
        if status >= 400 or not isinstance(body, dict) or "token" not in body:
            message = body.get("message") if isinstance(body, dict) else body
            raise GitHubAuthError(f"GitHub no dio un token de instalación ({status}): {message}")
        self._token = body["token"]
        expires = body.get("expires_at", "")
        try:
            from datetime import datetime

            self._expires_at = datetime.fromisoformat(expires.replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            self._expires_at = self._clock() + 3600
