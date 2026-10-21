"""Update source constants and URL helpers; no application imports."""

from __future__ import annotations

import os
import re
from typing import Any

GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com/%s/main"
GITEE_API = "https://gitee.com/api/v5"
GITEE_RAW = "https://gitee.com/%s/raw/master"
DEFAULT_REPO = "ExpertKT/notice-hub"
DEFAULT_MIRROR_REPO = "ExpertKT/notice-hub-mirror"
MIRROR_ENV = "QQ_DIGEST_UPDATE_MIRROR"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _valid_repo(value: Any) -> str:
    text = str(value or "").strip()
    return text if _REPO_RE.fullmatch(text) else ""


def mirror_repo(settings: Any = None) -> str:
    """Return the first valid configured mirror, if any."""
    configured = _valid_repo(getattr(settings, "update_mirror", ""))
    if configured:
        return configured
    configured = _valid_repo(os.environ.get(MIRROR_ENV, ""))
    return configured or _valid_repo(DEFAULT_MIRROR_REPO)


def sources(settings: Any = None) -> list[dict[str, str]]:
    """Return GitHub first and the optional Gitee mirror second."""
    result = [{
        "id": "github",
        "repo": DEFAULT_REPO,
        "api_root": GITHUB_API,
        "raw_base": GITHUB_RAW % DEFAULT_REPO,
    }]
    mirror = mirror_repo(settings)
    if mirror:
        result.append({
            "id": "gitee",
            "repo": mirror,
            "api_root": GITEE_API,
            "raw_base": GITEE_RAW % mirror,
        })
    return result


def announcements_url(source: dict[str, Any]) -> str:
    return str(source["raw_base"]).rstrip("/") + "/announcements.json"


def manifest_url(source: dict[str, Any]) -> str:
    return str(source["raw_base"]).rstrip("/") + "/update-manifest.json"
