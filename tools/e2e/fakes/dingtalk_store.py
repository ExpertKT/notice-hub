"""W3 平台替身：假钉钉本地库（`%APPDATA%\\DingTalk\\<uid>_v3\\DBFiles\\dingtalk.db`）。

真机怎么读
----------
`noticehub/dingtalk_receiver.py`：
* `_locate_db_dir()`：`Path(os.environ["APPDATA"]) / "DingTalk"` 下找 `*_v3` 目录，
  要求 `DBFiles/dingtalk.db` 与 `user_config` 都在（`:385-393`）；
* `_read_salt()`：`user_config` 是 **base64(JSON)**，含 `salt`（32 位 ASCII）与 `salt_md5`
  （md5(salt)），校验不过直接报「钉钉本地配置校验失败」（`:395-406`）；
* `_uid_candidates()`：扫 `DingTalk` 目录下非库文件的内容，抽 9~10 位数字当 uid（`:408-434`）；
* `_ensure_cipher()`：`derive_key_v3(uid, salt)`（pbkdf2-sha1 1000 轮 + md5 前 16 个 hex 字符，
  `:33-38`）得到的 **16 字节 ASCII** 作 AES-128 key，逐 **4096 字节页 ECB** 解密，
  首解出 `SQLite format 3\\0` 即为对（`:436-459`）；
* 表结构：`tbconversation(cid,title,memberCount,type)`、`tbmsg_NNN(cid,mid,senderId,createdAt,contentType,content)`
  （`:529`、`:568-590`）。

所以这个替身**必须造加密库**（明文库连 `_ensure_cipher` 都过不去），加密方式就是上面那条
「按 16 字节块 AES-128-ECB 整页加密」，用 `tools/wechat_probe.py` 里真机同一套 Windows CNG。

可切换状态
----------
* `ok`     —— 目录齐、库可读（正例）。
* `locked` —— 库被占：以 share=0 独占打开 `dingtalk.db`，接收端 `_read_shared` 抛 PermissionError
              （对应真机上钉钉正在写库/被安全软件锁定的情形）。

环境约定
--------
`env` 返回 `{"APPDATA": <run>/dingtalk}`；harness 起被测实例时把 APPDATA 换成它即可，
不会碰到用户真实的 `%APPDATA%`。停止时关独占句柄并删目录。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ._rundir import default_run_dir  # noqa: E402
from tools.wechat_probe import CngAesEcb  # noqa: E402

PAGE_SIZE = 4096
DEFAULT_UID = "1234567890"
DEFAULT_GROUPS = [
    {"cid": "cid_e2e_group_1", "title": "学院通知群", "members": 27, "type": 2},
    {"cid": "cid_e2e_group_2", "title": "2024 级年级群", "members": 96, "type": 2},
]
# 时间戳必须落在「最近两分钟」窗口内：`scan_once()` 在没有同步游标时只取
# `now - 2min` 之后的消息（dingtalk_receiver.py:680-685）。写死历史日期会读到 0 条。
def _now_ms(minutes_ago: float = 1.0) -> int:
    return int((time.time() - minutes_ago * 60) * 1000)


DEFAULT_MESSAGES = [
    ("cid_e2e_group_1", "mid_e2e_1", "u_e2e_teacher", _now_ms(1.5), 1, json.dumps({"text": "请各班今天18:00前提交材料"}, ensure_ascii=False)),
    ("cid_e2e_group_1", "mid_e2e_2", "u_e2e_teacher", _now_ms(1.2), 1, json.dumps({"text": "本周五下午的班会改到线上"}, ensure_ascii=False)),
    ("cid_e2e_group_2", "mid_e2e_3", "u_e2e_leader", _now_ms(1.0), 1, json.dumps({"text": "收到请回复"}, ensure_ascii=False)),
]


def derive_key_v3(uid: str, salt: bytes) -> bytes:
    """与接收端同一口径：pbkdf2-sha1(uid+salt, b"666DingT", 1000, 32) 的 md5 前 16 个 hex 字符。"""
    derived = hashlib.pbkdf2_hmac("sha1", uid.encode("ascii") + salt, b"666DingT", 1000, dklen=32)
    return hashlib.md5(derived).hexdigest()[:16].encode("ascii")


def _encrypt_pages(plain: bytes, key: bytes) -> bytes:
    if len(plain) % PAGE_SIZE:
        raise ValueError("明文库长度不是 4096 的整数倍：%d" % len(plain))
    out = bytearray()
    with CngAesEcb(key) as aes:
        for offset in range(0, len(plain), 16):
            out += aes.block_encrypt(plain[offset:offset + 16])
    return bytes(out)


def _lock_file(path: Path) -> int | None:
    import ctypes

    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    handle = kernel32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise OSError("CreateFileW 失败，错误码 %d" % ctypes.get_last_error())
    return int(handle)


def _unlock_file(handle: int | None) -> None:
    import ctypes

    if not handle or os.name != "nt":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle(ctypes.c_void_p(handle))


def build_plaintext_db(
    path: Path,
    groups: Iterable[dict[str, Any]],
    messages: Iterable[tuple[str, str, str, int, int, str]],
) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA page_size = 4096")
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("CREATE TABLE tbconversation (cid TEXT, title TEXT, memberCount INTEGER, type INTEGER)")
        connection.executemany(
            "INSERT INTO tbconversation (cid, title, memberCount, type) VALUES (?, ?, ?, ?)",
            [(row["cid"], row["title"], int(row.get("members") or 0), int(row.get("type") or 2)) for row in groups],
        )
        connection.execute(
            "CREATE TABLE tbmsg_000 (cid TEXT, mid TEXT, senderId TEXT, createdAt INTEGER, "
            "contentType INTEGER, content TEXT)"
        )
        connection.executemany("INSERT INTO tbmsg_000 VALUES (?, ?, ?, ?, ?, ?)", messages)
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()


class DingtalkStore:
    def __init__(
        self,
        *,
        mode: str = "ok",
        root: Path | str | None = None,
        run_id: str | None = None,
        uid: str = DEFAULT_UID,
        groups: Iterable[dict[str, Any]] | None = None,
        messages: Iterable[tuple[str, str, str, int, int, str]] | None = None,
        salt: str | None = None,
    ) -> None:
        if mode not in {"ok", "locked"}:
            raise ValueError("mode 只支持 ok / locked，拿到 %r" % (mode,))
        self.mode = mode
        self.uid = uid
        self.groups = list(groups if groups is not None else DEFAULT_GROUPS)
        self.messages = list(messages if messages is not None else DEFAULT_MESSAGES)
        self.salt_text = salt or ("s" * 32)
        if len(self.salt_text) != 32:
            raise ValueError("salt 必须是 32 位 ASCII")
        self._created_run_dir = True
        self.root = default_run_dir("dingtalk", run_id) if root is None else Path(root)
        self.handle: int | None = None
        self.account_dir = self.root / "DingTalk" / (self.uid + "_v3")
        self.db_path = self.account_dir / "DBFiles" / "dingtalk.db"
        self._build()

    # ---- 构造 ----
    def _build(self) -> None:
        (self.account_dir / "DBFiles").mkdir(parents=True, exist_ok=True)
        salt = self.salt_text.encode("ascii")
        config = {"salt": self.salt_text, "salt_md5": hashlib.md5(salt).hexdigest()}
        (self.account_dir / "user_config").write_bytes(
            base64.b64encode(json.dumps(config).encode("utf-8"))
        )
        # uid 候选来源：接收端扫目录内非库文件的内容抽 9~10 位数字
        (self.account_dir / "cache.dat").write_text(self.uid + "\n", encoding="utf-8")
        staging = self.root / "_plain"
        staging.mkdir(parents=True, exist_ok=True)
        plain_path = staging / "dingtalk.db"
        try:
            build_plaintext_db(plain_path, self.groups, self.messages)
            key = derive_key_v3(self.uid, salt)
            self.db_path.write_bytes(_encrypt_pages(plain_path.read_bytes(), key))
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        if self.mode == "locked":
            self.handle = _lock_file(self.db_path)

    # ---- 契约 ----
    @property
    def port(self) -> None:
        return None

    def env(self) -> dict[str, str]:
        return {"APPDATA": str(self.root)}

    def info(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "root": str(self.root),
            "db": str(self.db_path),
            "uid": self.uid,
            "groups": [row["cid"] for row in self.groups],
            "messages": len(self.messages),
            "locked": bool(self.handle),
        }

    def stop(self) -> None:
        _unlock_file(self.handle)
        self.handle = None
        if self._created_run_dir:
            shutil.rmtree(self.root, ignore_errors=True)
            try:
                self.root.parent.rmdir()
            except OSError:
                pass


def start(
    mode: str = "ok",
    root: Path | str | None = None,
    run_id: str | None = None,
    uid: str = DEFAULT_UID,
    groups: Iterable[dict[str, Any]] | None = None,
    messages: Iterable[tuple[str, str, str, int, int, str]] | None = None,
    salt: str | None = None,
) -> dict[str, Any]:
    """统一入口：造假钉钉库目录并返回 handle 字典。"""
    fake = DingtalkStore(
        mode=mode, root=root, run_id=run_id, uid=uid,
        groups=groups, messages=messages, salt=salt,
    )
    return {
        "name": "dingtalk",
        "kind": "dir",
        "port": None,
        "base_url": None,
        "env": fake.env(),
        "info": fake.info(),
        "impl": fake,
        "stop": fake.stop,
    }
