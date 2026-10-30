"""投递去重。表仍是 deliveries，规则与 Store 上的原方法相同。"""

from __future__ import annotations

from typing import Any


class DeliveryRepository:
    def __init__(self, store: Any) -> None:
        self._store = store

    def claim_delivery(
        self,
        *,
        digest_id: int,
        channel: str,
        target: str,
        dedupe_key: str,
        max_attempts: int,
        retry_seconds: int,
    ) -> tuple[bool, int | None]:
        return self._store.claim_delivery(
            digest_id=digest_id,
            channel=channel,
            target=target,
            dedupe_key=dedupe_key,
            max_attempts=max_attempts,
            retry_seconds=retry_seconds,
        )

    def get_delivery(self, delivery_id: int) -> dict[str, Any] | None:
        return self._store.get_delivery(delivery_id)

    def mark_delivery(self, delivery_id: int, *, ok: bool, error: str = "") -> None:
        self._store.mark_delivery(delivery_id, ok=ok, error=error)

    def pending_deliveries(self, *, max_attempts: int, retry_seconds: int) -> list[dict[str, Any]]:
        return self._store.pending_deliveries(max_attempts=max_attempts, retry_seconds=retry_seconds)

    def increment_delivery_attempt(self, delivery_id: int) -> int:
        return self._store.increment_delivery_attempt(delivery_id)
