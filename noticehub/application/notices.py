"""最近摘要。读库失败由 Web 转成页面能显示的原因。"""

from __future__ import annotations

from typing import Any


def list_notices(store: Any) -> dict[str, Any]:
    """GET /api/notices。固定最近 8 条，正文最多 600 字。"""
    items = []
    for digest in store.recent_digests(limit=8):
        items.append(
            {
                "id": digest.get("id"),
                "kind": digest.get("kind"),
                "created_at": digest.get("created_at"),
                "summary": f"{digest.get('item_count') or 0} 条重点",
                "body": str(digest.get("body") or "")[:600],
            }
        )
    return {"items": items}
