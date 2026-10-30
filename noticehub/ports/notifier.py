"""通知器。无通道时 send_digest 不抛，返回空列表。"""

from __future__ import annotations

from typing import Any, Protocol


class Notifier(Protocol):
    def send_digest(self, digest: Any, *, dedupe_key: str = "") -> list[Any]:
        ...

    def send_text(self, title: str, body: str, *, dedupe_key: str, digest_id: int = 0) -> list[Any]:
        ...

    def send_card(
        self,
        title: str,
        body: str,
        *,
        dedupe_key: str,
        summary: str = "",
        html: str = "",
    ) -> list[Any]:
        ...

    def retry_pending(self, *, retry_seconds: int | None = None) -> list[Any]:
        ...
