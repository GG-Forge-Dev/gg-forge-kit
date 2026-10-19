from __future__ import annotations

import logging
import time
from collections import deque
from typing import Callable

from gg_forge_kit._http import PostFn, http_post
from gg_forge_kit.config import optional_env

log = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"
# Dominio verificado en Resend (2026-10-19). La dirección de pruebas onboarding@resend.dev solo entregaba
# al dueño de la cuenta de Resend.
DEFAULT_SENDER = "GG Forge Alerts <alerts@gg-forge.com>"
DEFAULT_MAX_PER_HOUR = 20


class EmailAlerter:
    """Envía alertas por correo (a Gmail) usando la API HTTPS de Resend.

    Railway bloquea el SMTP en el plan Hobby, por eso no se usa SMTP. Nunca lanza excepciones:
    una alerta que no sale se registra en el log, pero no debe tumbar al bot que la envía.

    Límite anti-inundación: como máximo `max_per_hour` correos por hora. Los que se omiten se
    cuentan y se informan en el siguiente correo que sí salga.
    """

    def __init__(
        self,
        api_key: str,
        recipients: list[str],
        *,
        sender: str = DEFAULT_SENDER,
        bot_name: str = "",
        max_per_hour: int = DEFAULT_MAX_PER_HOUR,
        post: PostFn = http_post,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not recipients:
            raise ValueError("EmailAlerter necesita al menos un destinatario")
        self._api_key = api_key
        self.recipients = recipients
        self.sender = sender
        self.bot_name = bot_name
        self.max_per_hour = max_per_hour
        self._post = post
        self._clock = clock
        self._sent_at: deque[float] = deque()
        self.suppressed = 0

    @classmethod
    def from_env(cls, *, bot_name: str = "", post: PostFn = http_post) -> EmailAlerter | None:
        """Crea el alertador desde `RESEND_API_KEY` y `ALERTAS_DESTINATARIOS` (separados por comas).

        Devuelve None si falta alguna de las dos: las alertas por correo son opcionales.
        """
        api_key = optional_env("RESEND_API_KEY")
        recipients = [r.strip() for r in (optional_env("ALERTAS_DESTINATARIOS") or "").split(",") if r.strip()]
        if not api_key or not recipients:
            log.info("Alertas por correo desactivadas (faltan RESEND_API_KEY o ALERTAS_DESTINATARIOS).")
            return None
        return cls(
            api_key,
            recipients,
            sender=optional_env("ALERTAS_REMITENTE", DEFAULT_SENDER) or DEFAULT_SENDER,
            bot_name=bot_name,
            post=post,
        )

    async def send(self, subject: str, body: str) -> bool:
        """Envía una alerta. Devuelve si salió."""
        if not self._take_slot():
            self.suppressed += 1
            log.warning("Alerta por correo omitida por límite (%d/h): %s", self.max_per_hour, subject)
            return False

        if self.suppressed:
            body += f"\n\n⚠️ Se omitieron {self.suppressed} alertas anteriores por el límite de {self.max_per_hour}/hora."

        prefix = f"[{self.bot_name}] " if self.bot_name else ""
        payload = {
            "from": self.sender,
            "to": self.recipients,
            "subject": f"{prefix}{subject}",
            "text": body,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            status, text = await self._post(RESEND_URL, headers, payload, None)
        except Exception as e:
            log.error("No se pudo enviar la alerta por correo: %s", e)
            return False
        if status >= 400:
            log.error("Resend rechazó la alerta (HTTP %d): %s", status, text[:300])
            return False
        self.suppressed = 0
        return True

    def _take_slot(self) -> bool:
        now = self._clock()
        while self._sent_at and now - self._sent_at[0] >= 3600:
            self._sent_at.popleft()
        if len(self._sent_at) >= self.max_per_hour:
            return False
        self._sent_at.append(now)
        return True
