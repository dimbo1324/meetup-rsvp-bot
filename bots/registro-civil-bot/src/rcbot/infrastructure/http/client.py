"""HTTP-обёртка: только https, только хосты из списка SPEC 2.6, TLS всегда проверяется.

Одна долгоживущая сессия на сайт (SPEC 2.2): `TCPConnector(limit=4, keepalive_timeout=75,
ttl_dns_cache=300)` и общий таймаут запроса. Отпечаток клиента не подменяется (SPEC 0.3, 2).
"""

from __future__ import annotations

import ssl
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

import aiohttp
from yarl import URL

from rcbot.domain.errors import HostNotAllowed

# Параметры, которыми вызывающий мог бы обойти защиту: TLS проверяется всегда (SPEC 2.6),
# переадресации не выполняются (иначе 302 уводит мимо списка хостов), прокси не используются (0.3)
_FORBIDDEN_OPTIONS = frozenset(
    {
        "ssl",
        "verify_ssl",
        "ssl_context",
        "fingerprint",
        "allow_redirects",
        "max_redirects",
        "proxy",
        "proxy_auth",
        "proxy_headers",
        "trust_env",
    }
)


class SafeHttp:
    def __init__(
        self,
        allowed_hosts: Iterable[str],
        timeout_sec: float,
        *,
        limit: int = 4,
        cookie_jar: aiohttp.abc.AbstractCookieJar | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        # ssl_context нужен только тестам с локальным TLS-сервером: в работе проверка по системным
        # сертификатам, отключить её (ssl=False) нельзя
        self._ssl: ssl.SSLContext | bool = ssl_context if ssl_context is not None else True
        self._allowed = frozenset(host.lower() for host in allowed_hosts)
        self._timeout = aiohttp.ClientTimeout(total=timeout_sec)
        self._limit = limit
        self._jar = cookie_jar
        self._session: aiohttp.ClientSession | None = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=self._limit, keepalive_timeout=75, ttl_dns_cache=300
            )
            if self._jar is None:
                # Хранилище cookies живёт дольше сессии: после сброса зависшего источника
                # (закрытие и новая сессия) вход не теряется и не тратит лимит 6 входов в час
                self._jar = aiohttp.CookieJar()
            self._session = aiohttp.ClientSession(
                connector=connector, timeout=self._timeout, cookie_jar=self._jar
            )
        return self._session

    @property
    def cookie_jar(self) -> aiohttp.abc.AbstractCookieJar:
        return self.session.cookie_jar

    def check(self, url: str) -> None:
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
        except ValueError:  # «https://[x»: urllib отказывает разбором, а не нашим исключением
            raise HostNotAllowed("адрес не разобран, запрос не разрешён") from None
        if parsed.scheme != "https" or host not in self._allowed:
            raise HostNotAllowed(f"запрос к {parsed.scheme}://{host} не разрешён")
        # Соединение откроет aiohttp (yarl), а список проверен по urllib: адрес, который они
        # читают по-разному (например, с обратной косой чертой), отклоняется до сети
        try:
            connect_host = (URL(url).host or "").lower()
        except ValueError:
            connect_host = ""
        if connect_host != host:
            raise HostNotAllowed(f"адрес с хостом {host} разобран неоднозначно, запрос не разрешён")

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        """Возвращает контекстный менеджер ответа aiohttp.

        Переадресации не выполняются: ответ 302 возвращается как есть, решает вызывающий.
        """
        banned = _FORBIDDEN_OPTIONS & kwargs.keys()
        if banned:
            raise TypeError(f"параметры {sorted(banned)} запросу SafeHttp не разрешён")
        self.check(url)
        kwargs["ssl"] = self._ssl
        kwargs["allow_redirects"] = False
        return self.session.request(method, url, **kwargs)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
