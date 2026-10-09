"""IA con respaldo: Claude primero; si falla, Gemini; si falla, OpenAI.

Mismo patrón que `im-tired-boss/BE/src/ai/fallback.ts` y el Cartógrafo: se prueba cada proveedor
en orden y se devuelve la primera respuesta válida. Cualquier fallo (HTTP, timeout, respuesta vacía,
rechazo) pasa al siguiente. No se reintenta dentro de un mismo proveedor (el SDK de cada uno ya
reintenta errores transitorios), para no multiplicar reintentos por proveedores.

Solo se usan los proveedores con API key en el entorno. Instalar con `gg-forge-kit[ia]`. Cada SDK se
importa la primera vez que su proveedor responde, no al arrancar: juntos ocupan ~50 MB de RAM, y el de
un proveedor de respaldo no llega a cargarse mientras el primero funcione.

Variables de entorno:
    ANTHROPIC_API_KEY   · IA_MODELO_CLAUDE  (por defecto claude-opus-5-5)
    GEMINI_API_KEY      · IA_MODELO_GEMINI  (por defecto gemini-3.6-flash)
    OPENAI_API_KEY      · IA_MODELO_OPENAI  (obligatorio para usar OpenAI)
    IA_ORDEN            (por defecto «claude,gemini,openai»)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from gg_forge_kit.config import optional_env

log = logging.getLogger(__name__)

# Opus 5.5: más barato que Opus 5 ($4/$20 frente a $5/$25 por millón de tokens) y con la misma llamada.
DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"
DEFAULT_ORDER = ("claude", "gemini", "openai")
DEFAULT_MAX_TOKENS = 16000  # con el pensamiento adaptativo de Claude, quedarse corto trunca la respuesta
CLAUDE_FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Tiempo máximo por proveedor. Sin límite, el SDK de Anthropic espera hasta 10 min y reintenta: una
# pregunta de Discord (cuyo token de respuesta caduca a los 15 min) se quedaba «pensando» para siempre.
DEFAULT_TIMEOUT = 120.0


@dataclass(frozen=True)
class Respuesta:
    texto: str
    proveedor: str
    modelo: str
    tokens_entrada: int
    tokens_salida: int


class ProveedorError(RuntimeError):
    """Un proveedor no dio una respuesta utilizable (error, vacío o rechazo)."""


class SinRespuestaError(RuntimeError):
    """Ningún proveedor respondió. `errores` tiene el motivo de cada uno."""

    def __init__(self, errores: dict[str, str]) -> None:
        detalle = "; ".join(f"{p}: {e}" for p, e in errores.items()) or "no hay proveedores configurados"
        super().__init__(f"Ningún proveedor de IA respondió ({detalle})")
        self.errores = errores


class Proveedor(Protocol):
    nombre: str
    modelo: str

    async def completar(self, sistema: str, mensaje: str, max_tokens: int) -> Respuesta: ...


class ClaudeProveedor:
    """Claude vía el SDK oficial de Anthropic.

    Usa `fallbacks="default"` (salvo con Haiku, que no tiene respaldo en el servidor): si los clasificadores de seguridad del modelo rechazan la petición,
    la API la repite en otro modelo de Anthropic dentro de la misma llamada. Si aun así termina en
    rechazo, se trata como fallo y se pasa al siguiente proveedor.
    """

    nombre = "claude"

    def __init__(self, api_key: str, modelo: str = DEFAULT_CLAUDE_MODEL, *, client: Any = None) -> None:
        self._api_key = api_key
        self._client = client
        self.modelo = modelo

    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=self._api_key, timeout=DEFAULT_TIMEOUT, max_retries=1)
        return self._client

    async def completar(self, sistema: str, mensaje: str, max_tokens: int) -> Respuesta:
        # Haiku no tiene respaldo en el servidor: con él, `fallbacks` no repite nada (y en lista da 400).
        respaldo = {} if "haiku" in self.modelo else {"betas": [CLAUDE_FALLBACK_BETA], "fallbacks": "default"}
        response = await self.client.beta.messages.create(
            model=self.modelo,
            max_tokens=max_tokens,
            system=sistema,
            messages=[{"role": "user", "content": mensaje}],
            **respaldo,
        )
        if response.stop_reason == "refusal":
            categoria = getattr(getattr(response, "stop_details", None), "category", None)
            raise ProveedorError(f"rechazo del modelo (categoría: {categoria})")
        texto = "".join(b.text for b in response.content if b.type == "text").strip()
        if not texto:
            raise ProveedorError(f"respuesta vacía (stop_reason={response.stop_reason})")
        if response.stop_reason == "max_tokens":
            log.warning("Claude cortó la respuesta por max_tokens=%d", max_tokens)
        return Respuesta(texto, self.nombre, response.model, response.usage.input_tokens, response.usage.output_tokens)


class GeminiProveedor:
    nombre = "gemini"

    def __init__(self, api_key: str, modelo: str = DEFAULT_GEMINI_MODEL, *, client: Any = None) -> None:
        self._api_key = api_key
        self._client = client
        self.modelo = modelo

    @property
    def client(self) -> Any:
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self._api_key)
        return self._client

    async def completar(self, sistema: str, mensaje: str, max_tokens: int) -> Respuesta:
        from google.genai import types

        response = await self.client.aio.models.generate_content(
            model=self.modelo,
            contents=mensaje,
            config=types.GenerateContentConfig(system_instruction=sistema, max_output_tokens=max_tokens),
        )
        texto = (response.text or "").strip()
        if not texto:
            raise ProveedorError("respuesta vacía")
        usage = response.usage_metadata
        return Respuesta(texto, self.nombre, self.modelo,
                         getattr(usage, "prompt_token_count", 0) or 0, getattr(usage, "candidates_token_count", 0) or 0)


class OpenAIProveedor:
    nombre = "openai"

    def __init__(self, api_key: str, modelo: str, *, client: Any = None) -> None:
        self._api_key = api_key
        self._client = client
        self.modelo = modelo

    @property
    def client(self) -> Any:
        if self._client is None:
            import openai

            self._client = openai.AsyncOpenAI(api_key=self._api_key, timeout=DEFAULT_TIMEOUT, max_retries=1)
        return self._client

    async def completar(self, sistema: str, mensaje: str, max_tokens: int) -> Respuesta:
        response = await self.client.responses.create(
            model=self.modelo, instructions=sistema, input=mensaje, max_output_tokens=max_tokens,
        )
        texto = (response.output_text or "").strip()
        if not texto:
            raise ProveedorError("respuesta vacía")
        usage = response.usage
        return Respuesta(texto, self.nombre, self.modelo, getattr(usage, "input_tokens", 0), getattr(usage, "output_tokens", 0))


class ClienteIA:
    def __init__(self, proveedores: list[Proveedor], *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout
        self.proveedores = proveedores

    @property
    def cadena(self) -> str:
        return " → ".join(f"{p.nombre} ({p.modelo})" for p in self.proveedores) or "ninguno"

    @classmethod
    def desde_env(cls) -> ClienteIA:
        """Construye la cadena con los proveedores que tienen API key, en el orden de `IA_ORDEN`."""
        disponibles: dict[str, Any] = {}
        if key := optional_env("ANTHROPIC_API_KEY"):
            disponibles["claude"] = lambda: ClaudeProveedor(key, optional_env("IA_MODELO_CLAUDE", DEFAULT_CLAUDE_MODEL) or DEFAULT_CLAUDE_MODEL)
        if key_g := optional_env("GEMINI_API_KEY"):
            disponibles["gemini"] = lambda: GeminiProveedor(key_g, optional_env("IA_MODELO_GEMINI", DEFAULT_GEMINI_MODEL) or DEFAULT_GEMINI_MODEL)
        key_o, modelo_o = optional_env("OPENAI_API_KEY"), optional_env("IA_MODELO_OPENAI")
        if key_o and modelo_o:
            disponibles["openai"] = lambda: OpenAIProveedor(key_o, modelo_o)
        elif key_o:
            log.warning("OPENAI_API_KEY sin IA_MODELO_OPENAI: OpenAI queda fuera de la cadena.")
        orden = [p.strip() for p in (optional_env("IA_ORDEN") or ",".join(DEFAULT_ORDER)).split(",") if p.strip()]
        cliente = cls([disponibles[n]() for n in orden if n in disponibles])
        log.info("Cadena de IA: %s", cliente.cadena)
        return cliente

    async def completar(self, sistema: str, mensaje: str, *, max_tokens: int = DEFAULT_MAX_TOKENS) -> Respuesta:
        errores: dict[str, str] = {}
        for proveedor in self.proveedores:
            try:
                respuesta = await asyncio.wait_for(proveedor.completar(sistema, mensaje, max_tokens), self.timeout)
            except Exception as e:  # cualquier fallo de un proveedor → el siguiente
                errores[proveedor.nombre] = f"{type(e).__name__}: {e}"[:300]
                log.warning("IA %s falló (%s), probando el siguiente proveedor", proveedor.nombre, errores[proveedor.nombre])
                continue
            log.info("IA respondió %s/%s (%d→%d tokens)", respuesta.proveedor, respuesta.modelo,
                     respuesta.tokens_entrada, respuesta.tokens_salida)
            return respuesta
        raise SinRespuestaError(errores)
