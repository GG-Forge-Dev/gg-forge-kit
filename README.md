# gg-forge-kit

Utilidades compartidas por los bots de Discord de GG Forge. Diseño y hoja de ruta en
[ROADMAP-BOTS-GG-FORGE.md § 3b](../../ROADMAP-BOTS-GG-FORGE.md).

| Módulo | Uso |
|---|---|
| `gg_forge_kit.config` | `require_env("X")`, `optional_env("X", "def")`, `load_json("config/x.json")` |
| `gg_forge_kit.logs` | `setup_logging()` al arrancar |
| `gg_forge_kit.heartbeat` | `Heartbeat(url, is_healthy=...)` → `.start()` hace ping cada 10 min; `.fail(motivo)` |
| `gg_forge_kit.alerts` | `EmailAlerter.from_env(bot_name=...)` → `await .send(asunto, cuerpo)` |
| `gg_forge_kit.ia` | `ClienteIA.desde_env()` → `await .completar(sistema, mensaje)`: Claude primero (con `fallbacks="default"` ante rechazos), luego Gemini, luego OpenAI. Requiere el extra `[ia]` |
| `gg_forge_kit.github` | `GitHubAuth.from_env(repositories=[...], permissions={...})` → `await .token()` / `await .headers()`: token de la GitHub App (1 h, se renueva solo, limitado a esos repos y permisos) o, si no hay App, `GITHUB_TOKEN`. Requiere el extra `[github]` |
| `gg_forge_kit.validation` | Validar JSON de config: `check_keys` (clave desconocida → error con sugerencia; `_x` = comentario), `discord_id(s)`, `choice`, `positive_int`, `positive_number`, `string_list`, `ConfigError` |

## Instalar en un bot

Producción (`requirements.txt`), fijado a una versión:

```
gg-forge-kit @ https://github.com/GG-Forge-Dev/gg-forge-kit/archive/refs/tags/v0.4.0.tar.gz
```

Con IA: `gg-forge-kit[ia] @ …` (instala los SDK de Anthropic, Gemini y OpenAI). Con GitHub: `gg-forge-kit[github] @ …` (PyJWT).

Desarrollo local (desde la carpeta del bot):

```bash
pip install -e ../gg-forge-kit
```

## Variables de entorno que lee

| Variable | Módulo | Descripción |
|---|---|---|
| `RESEND_API_KEY` | alerts | API key de [Resend](https://resend.com). Sin ella, no se envían correos |
| `ALERTAS_DESTINATARIOS` | alerts | Correos separados por comas. Sin dominio propio verificado, solo el Gmail con el que se registró la cuenta de Resend |
| `ALERTAS_REMITENTE` | alerts | Opcional. Por defecto `GG Forge Alertas <onboarding@resend.dev>` |

| `ANTHROPIC_API_KEY`, `IA_MODELO_CLAUDE` | ia | Claude (modelo por defecto `claude-opus-5`) |
| `GEMINI_API_KEY`, `IA_MODELO_GEMINI` | ia | Respaldo 1 (por defecto `gemini-3.6-flash`) |
| `OPENAI_API_KEY`, `IA_MODELO_OPENAI` | ia | Respaldo 2 (el modelo es obligatorio) |
| `IA_ORDEN` | ia | Orden de la cadena (por defecto `claude,gemini,openai`) |

`HEARTBEAT_URL` la lee cada bot y se la pasa a `Heartbeat`.

## Tests

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

## Publicar una versión

1. Subir `version` en `pyproject.toml` y `__init__.py`.
2. `git tag v0.X.0 && git push --tags`.
3. Actualizar la URL en el `requirements.txt` de cada bot.

| `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` | github | La GitHub App «GG Forge Bots». La clave vale como `.pem`, con `
` escritos o en base64 |
| `GITHUB_APP_INSTALLATION_ID` | github | Opcional: si falta, se busca la instalación en `GITHUB_APP_OWNER` |
| `GITHUB_APP_OWNER` | github | Opcional. Por defecto `GG-Forge-Dev` |
| `GITHUB_TOKEN` | github | Solo si no hay App (token fijo, para la transición) |
