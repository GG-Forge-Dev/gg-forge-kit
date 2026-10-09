"""Cadena de IA con clientes simulados (sin llamadas reales ni gasto)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gg_forge_kit.ia import (
    CLAUDE_FALLBACK_BETA, ClaudeProveedor, ClienteIA, GeminiProveedor, OpenAIProveedor, ProveedorError, Respuesta,
    SinRespuestaError,
)


class FakeAnthropic:
    def __init__(self, response):
        self.calls = []
        self.response = response
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.calls.append(kw)
        return self.response


def claude_response(text="hola", stop="end_turn", category=None):
    content = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(content=content, stop_reason=stop, model="claude-opus-5-5",
                           stop_details=SimpleNamespace(category=category) if category else None,
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5))


async def test_claude_request_shape_and_text_extraction():
    fake = FakeAnthropic(claude_response("Respuesta"))
    r = await ClaudeProveedor("k", client=fake).completar("sistema", "pregunta", 16000)
    assert r == Respuesta("Respuesta", "claude", "claude-opus-5-5", 10, 5)
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["system"] == "sistema"
    assert call["fallbacks"] == "default" and call["betas"] == [CLAUDE_FALLBACK_BETA]
    assert call["messages"] == [{"role": "user", "content": "pregunta"}]
    assert "thinking" not in call  # Opus 5: adaptativo por defecto

async def test_haiku_goes_without_server_fallback():
    fake = FakeAnthropic(claude_response("Respuesta"))
    await ClaudeProveedor("k", "claude-haiku-5-5", client=fake).completar("s", "m", 16000)
    assert fake.calls[0]["model"] == "claude-haiku-5-5"
    assert "fallbacks" not in fake.calls[0] and "betas" not in fake.calls[0]



async def test_claude_refusal_and_empty_are_failures():
    with pytest.raises(ProveedorError, match="rechazo"):
        await ClaudeProveedor("k", client=FakeAnthropic(claude_response(stop="refusal", category="cyber"))).completar("s", "m", 100)
    with pytest.raises(ProveedorError, match="vacía"):
        await ClaudeProveedor("k", client=FakeAnthropic(claude_response(text="  "))).completar("s", "m", 100)


async def test_gemini_and_openai_shapes():
    async def gen(**kw):
        assert kw["model"] == "gemini-3.6-flash" and kw["config"].system_instruction == "s"
        return SimpleNamespace(text="g", usage_metadata=SimpleNamespace(prompt_token_count=3, candidates_token_count=1))
    gemini = GeminiProveedor("k", client=SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=gen))))
    assert (await gemini.completar("s", "m", 50)).texto == "g"

    async def resp(**kw):
        assert kw["instructions"] == "s" and kw["input"] == "m" and kw["max_output_tokens"] == 50
        return SimpleNamespace(output_text="o", usage=SimpleNamespace(input_tokens=2, output_tokens=1))
    openai_p = OpenAIProveedor("k", "modelo-x", client=SimpleNamespace(responses=SimpleNamespace(create=resp)))
    assert (await openai_p.completar("s", "m", 50)).proveedor == "openai"


class Fixed:
    def __init__(self, nombre, result):
        self.nombre, self.modelo, self.result, self.calls = nombre, f"{nombre}-m", result, 0

    async def completar(self, sistema, mensaje, max_tokens):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return Respuesta(self.result, self.nombre, self.modelo, 1, 1)


async def test_chain_falls_back_in_order():
    a, b, c = Fixed("claude", RuntimeError("429")), Fixed("gemini", "ok"), Fixed("openai", "no-llega")
    r = await ClienteIA([a, b, c]).completar("s", "m")
    assert r.proveedor == "gemini" and (a.calls, b.calls, c.calls) == (1, 1, 0)


async def test_slow_provider_times_out_and_falls_back():
    import asyncio

    class Slow(Fixed):
        async def completar(self, sistema, mensaje, max_tokens):
            await asyncio.sleep(10)

    r = await ClienteIA([Slow("claude", "tarde"), Fixed("gemini", "ok")], timeout=0.05).completar("s", "m")
    assert r.proveedor == "gemini"


async def test_chain_reports_every_failure():
    with pytest.raises(SinRespuestaError) as err:
        await ClienteIA([Fixed("claude", RuntimeError("caído")), Fixed("gemini", ProveedorError("vacía"))]).completar("s", "m")
    assert set(err.value.errores) == {"claude", "gemini"}
    with pytest.raises(SinRespuestaError, match="no hay proveedores"):
        await ClienteIA([]).completar("s", "m")


def test_from_env_order_and_openai_needs_model(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "IA_MODELO_OPENAI", "IA_ORDEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("OPENAI_API_KEY", "o")  # sin modelo: queda fuera
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    assert [p.nombre for p in ClienteIA.desde_env().proveedores] == ["claude", "gemini"]
    monkeypatch.setenv("IA_MODELO_OPENAI", "modelo-x")
    monkeypatch.setenv("IA_ORDEN", "openai,claude")
    assert ClienteIA.desde_env().cadena == "openai (modelo-x) → claude (claude-opus-5-5)"


def test_sdks_load_on_first_use_not_at_startup(monkeypatch):
    """Crear la cadena no importa ningún SDK: ocupan ~50 MB y el de respaldo puede no usarse nunca."""
    import builtins

    real_import, imported = builtins.__import__, []

    def spy(name, *args, **kwargs):
        if name.split(".")[0] in ("anthropic", "google", "openai"):
            imported.append(name)
        return real_import(name, *args, **kwargs)

    for var in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "IA_MODELO_OPENAI", "IA_ORDEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    monkeypatch.setenv("IA_MODELO_OPENAI", "modelo-x")
    monkeypatch.setattr(builtins, "__import__", spy)
    cliente = ClienteIA.desde_env()
    assert len(cliente.proveedores) == 3 and imported == []
    fake = object()
    assert ClaudeProveedor("k", client=fake).client is fake
