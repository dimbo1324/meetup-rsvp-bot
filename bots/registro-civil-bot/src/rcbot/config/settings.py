"""Несекретные настройки из `.env` и окружения (префикс `RCBOT_`). Секреты — в `secrets.py`."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Жёсткие ограничения SPEC 0.3 (правило 3) и 2.3: не настраиваются ни .env, ни Telegram.
MIN_POLL_INTERVAL_SEC = 10.0
MIN_DOM_POLL_INTERVAL_SEC = 60.0
MAX_POLL_INTERVAL_SEC = 3600.0

GUBUY_HOSTS = ("bpmgob.mec.gub.uy", "mi.iduruguay.gub.uy")
TELEGRAM_HOST = "api.telegram.org"
CALLMEBOT_HOST = "api.callmebot.com"
SAE_HOST = "sae.mec.gub.uy"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RCBOT_", env_file_encoding="utf-8", extra="ignore", validate_default=True
    )

    agenda_url: str = "https://bpmgob.mec.gub.uy/etapas/ejecutar/<НОМЕР_ЭТАПА>/1"
    tramite_id: str = ""
    location_text: str = "Oficina Tramite Extranjeros"
    doc_country: str = "Rusia"
    doc_type: str = "Pasaporte"
    months_ahead: int = Field(default=6, ge=1, le=12)
    login_url: str = (
        "https://bpmgob.mec.gub.uy/autenticacion/login"
        "?redirect=https://bpmgob.mec.gub.uy/tramites/iniciar/8942"
    )
    # Адрес API календаря станет известен после разведки (этап 3); пусто — ApiSource выключен
    api_url: str = ""

    poll_interval_sec: float = 20.0
    poll_jitter: float = Field(default=0.10, ge=0.0, le=0.5)
    request_timeout_sec: float = Field(default=8.0, gt=0, le=60)
    relogin_max_per_hour: int = Field(default=6, ge=1, le=6)

    tg_repeat_sec: float = Field(default=60.0, ge=30)
    tg_repeat_max: int = Field(default=30, ge=0, le=100)
    ntfy_server: str = "https://ntfy.sh"
    ntfy_repeat_sec: float = Field(default=120.0, ge=60)
    ntfy_repeat_max: int = Field(default=10, ge=0, le=50)
    call_repeat_sec: float = Field(default=180.0, ge=120)
    call_repeat_max: int = Field(default=5, ge=0, le=10)
    call_lang: str = "ru-RU-Standard-A"

    timezone: str = "America/Montevideo"
    daily_report_hour: int = Field(default=9, ge=0, le=23)
    sentry_enabled: bool = False
    sentry_dsn: str = ""
    watchdog_url: str = ""
    sae_source_enabled: bool = False
    sae_url: str = "https://sae.mec.gub.uy/sae/agendarReserva/Paso1.xhtml?e=9&a=82"
    headless: bool = True
    discover_channel: str = ""

    @field_validator("poll_interval_sec")
    @classmethod
    def _interval_not_below_minimum(cls, value: float) -> float:
        if value < MIN_POLL_INTERVAL_SEC:
            raise ValueError(f"интервал опроса не может быть меньше {MIN_POLL_INTERVAL_SEC:.0f} с")
        return min(value, MAX_POLL_INTERVAL_SEC)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("agenda_url", "login_url", "ntfy_server", "sae_url")
    @classmethod
    def _https_only(cls, value: str) -> str:
        if urlparse(value).scheme != "https":
            raise ValueError("допустим только https")
        return value

    @field_validator("api_url", "watchdog_url")
    @classmethod
    def _optional_https(cls, value: str) -> str:
        if value and urlparse(value).scheme != "https":
            raise ValueError("допустим только https")
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def agenda_configured(self) -> bool:
        return "<" not in self.agenda_url

    def allowed_hosts(self) -> frozenset[str]:
        """Список хостов SPEC 2.6: HTTP-обёртка отказывает всем остальным."""
        hosts = {*GUBUY_HOSTS, TELEGRAM_HOST, CALLMEBOT_HOST, _host(self.ntfy_server)}
        for optional in (self.watchdog_url, self.api_url):
            if optional:
                hosts.add(_host(optional))
        if self.sae_source_enabled:
            hosts.add(SAE_HOST)
        return frozenset(hosts)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def load_settings(env_file: Path) -> Settings:
    return Settings(_env_file=env_file if env_file.exists() else None)  # type: ignore[call-arg]
