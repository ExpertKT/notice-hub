"""通知器。通道实现仍在 qq_live_digest.push，这里只负责去重和投递。"""

from __future__ import annotations

import logging
from typing import Any

from noticehub.infrastructure.sqlite.deliveries import DeliveryRepository

LOGGER = logging.getLogger("qq_live_digest.push")


class PushManager:
    def __init__(self, store: Any, settings: Any, pushers: list[Any], logger: logging.Logger | None = None) -> None:
        self.store = store
        self.deliveries = DeliveryRepository(store)
        self.settings = settings
        self.pushers = pushers
        self.logger = logger or LOGGER

    @property
    def has_channels(self) -> bool:
        return bool(self.pushers)

    def send_digest(self, digest: Any, *, dedupe_key: str = "") -> list[Any]:
        if digest.id <= 0 or not digest.items:
            return []
        dedupe_key = dedupe_key or f"digest:{digest.id}"
        summary = digest.summary or digest.title
        outcomes = self._dispatch(
            digest.id, digest.title, digest.body, dedupe_key, tier=0, summary=summary, html=digest.html
        )
        if not any(item.ok for item in outcomes):
            fallback = self._dispatch(
                digest.id, digest.title, digest.body, dedupe_key, tier=1, summary=summary, html=digest.html
            )
            outcomes.extend(fallback)
        return outcomes

    def send_text(self, title: str, body: str, *, dedupe_key: str, digest_id: int = 0) -> list[Any]:
        return self._dispatch(digest_id, title, body, dedupe_key, tier=0)

    def send_card(
        self,
        title: str,
        body: str,
        *,
        dedupe_key: str,
        summary: str = "",
        html: str = "",
    ) -> list[Any]:
        """一次性卡片推送（用于测试），不写入摘要表。"""
        return self._dispatch(0, title, body, dedupe_key, tier=0, summary=summary, html=html)

    def _dispatch(
        self,
        digest_id: int,
        title: str,
        body: str,
        dedupe_key: str,
        *,
        tier: int,
        summary: str = "",
        html: str = "",
    ) -> list[Any]:
        from qq_live_digest.push import PushOutcome

        outcomes: list[Any] = []
        for pusher in self.pushers:
            if pusher.tier != tier:
                continue
            should_send, delivery_id = self.deliveries.claim_delivery(
                digest_id=digest_id,
                channel=pusher.name,
                target=pusher.target,
                dedupe_key=dedupe_key,
                max_attempts=self.settings.delivery_max_attempts,
                retry_seconds=self.settings.delivery_retry_seconds,
            )
            if not should_send:
                existing = self.deliveries.get_delivery(delivery_id or 0) or {}
                outcomes.append(
                    PushOutcome(
                        pusher.name,
                        pusher.target,
                        ok=True,
                        skipped=True,
                        error=str(existing.get("status") or "skipped"),
                        status=str(existing.get("status") or "skipped"),
                    )
                )
                continue
            try:
                pusher.send(title, body, summary=summary, html=html)
            except Exception as error:  # noqa: BLE001 - 需要记录任意通道异常
                message = str(error)[:400]
                self.logger.warning("推送失败 %s: %s", pusher.describe(), message)
                self.deliveries.mark_delivery(delivery_id or 0, ok=False, error=message)
                outcomes.append(PushOutcome(pusher.name, pusher.target, ok=False, error=message, status="failed"))
            else:
                self.logger.info("推送成功 %s", pusher.describe())
                self.deliveries.mark_delivery(delivery_id or 0, ok=True)
                outcomes.append(PushOutcome(pusher.name, pusher.target, ok=True, status="sent"))
        return outcomes

    def retry_pending(self, *, retry_seconds: int | None = None) -> list[Any]:
        from qq_live_digest.push import PushOutcome

        cooldown = self.settings.delivery_retry_seconds if retry_seconds is None else int(retry_seconds)
        outcomes: list[Any] = []
        for row in self.deliveries.pending_deliveries(
            max_attempts=self.settings.delivery_max_attempts,
            retry_seconds=cooldown,
        ):
            digest = self.store.get_digest(int(row["digest_id"]))
            if not digest or not digest.get("body"):
                self.deliveries.mark_delivery(int(row["id"]), ok=False, error="摘要正文为空，放弃重试")
                continue
            pusher = next(
                (item for item in self.pushers if item.name == row["channel"] and item.target == row["target"]),
                None,
            )
            if pusher is None:
                self.deliveries.mark_delivery(int(row["id"]), ok=False, error="通道已移除")
                continue
            attempts = self.deliveries.increment_delivery_attempt(int(row["id"]))
            title = f"QQ群通知（{row.get('window_end') or ''}）"
            try:
                pusher.send(title, digest["body"])
            except Exception as error:  # noqa: BLE001
                message = str(error)[:400]
                self.deliveries.mark_delivery(int(row["id"]), ok=False, error=message)
                outcomes.append(
                    PushOutcome(pusher.name, pusher.target, ok=False, error=message, attempts=attempts)
                )
            else:
                self.deliveries.mark_delivery(int(row["id"]), ok=True)
                outcomes.append(PushOutcome(pusher.name, pusher.target, ok=True, attempts=attempts))
        return outcomes
