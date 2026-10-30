"""W3 平台替身：假微信 4.x 库目录树（`xwechat_files/<wxid>/db_storage`）。

选型：**造 SQLCipher v4 格式的加密库**，而不是「未加密库 + 打桩解密」。
依据
----
真机走的是 `qq_live_digest/wechat_receiver.py` 的 `fetch_messages`：
`locate_storage()` → `resolve_key()` → `decrypt_database()`（`decrypt_database` 在
`key is None` 时直接 `raise ValueError("必须提供密钥")`，见 `wechat_receiver.py:855-856`）。
所以「未加密库」在**真进程**里根本进不到读表那一步，只能覆盖「有目录但读不了」的负例，
覆盖不到「微信消息正常进来」的正例。E2E 剧本要的是后者，故这里伪造真加密库：
手工 set 了 `QQ_DIGEST_WECHAT_KEY`（64 位十六进制 raw key）时，`resolve_key`
（`wechat_receiver.py:1039-1045`）会**直接拿这把 key 当页 key**（`_page_key_valid(page, key)`
→ `_BCryptAES(key)` + `_check_page_key`），本文件就是按这条路径加密的：
页密钥 = 你给的 raw key 本身，不做 KDF。

格式（照 `wechat_receiver.py:796-826` 的 decrypt 反向写）
--------------------------------------------------------
* 页 4096 字节，尾部留 80 字节（reserve）。页 1 前 16 字节是明文盐。
* AES-256-**CBC**，IV = 页尾 reserve 的头 16 字节（`page[4016:4032]`）。
* 页 1 明文 = `b"SQLite format 3\\x00"` + 4000 字节；其余页明文 = 4016 字节 + 80 字节 0。
* 页 1 尾部 64 字节 = HMAC-SHA512(`pbkdf2_sha512(key, 盐^0x3A, 2, 32)`,
  `page[16:4032] + LE32(page_no)`)——接收端只复核页 1，其余页照样写上。
* CBC 与 AES-ECB 单块原语直接复用 `tools/wechat_probe.py` 的 `CngAesEcb`
  （真机同一套 Windows CNG 实现，不是自己另写一份密码学）。

三种可切换状态（`mode=`）
-------------------------
* `ok`        —— 目录齐、库可读（正例）。
* `locked`    —— 目录齐，但 `message_0.db` 被本进程以 **share=0** 独占打开，
                 接收端读时拿到 PermissionError（Windows 的「无权限」语义）。
* `missing`   —— 路径不存在（`QQ_DIGEST_WECHAT_DIR` 指过去也没有）。

收尾
----
`stop()` 关独占句柄、删掉临时目录；端口不适用（这是目录 fixture，`port` 为 None）。
"""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.wechat_probe import CngAesEcb  # noqa: E402
from ._rundir import default_run_dir  # noqa: E402

PAGE_SIZE = 4096
RESERVE_SIZE = 80
SQLITE_HEADER = b"SQLite format 3\x00"
KEY_ENV = "QQ_DIGEST_WECHAT_KEY"
DIR_ENV = "QQ_DIGEST_WECHAT_DIR"

DEFAULT_GROUP = "1234@chatroom"
DEFAULT_GROUP_NAME = "学院通知群"
DEFAULT_MESSAGES = [
    (1, 1, 2, 1791626082, "【学院通知】关于国庆节放假安排的通知"),
    (2, 1, 2, 1791626090, "请各班班长今天18:00前提交材料"),
]


# --------------------------------------------------------------------------
# 明文库
# --------------------------------------------------------------------------
def message_table_name(group_id: str) -> str:
    """与 `wechat_receiver.message_table_name` 同一口径：Msg_ + md5(group_id)。"""
    return "Msg_" + hashlib.md5(group_id.encode("utf-8")).hexdigest()


def build_message_db(
    path: Path,
    groups: Iterable[str],
    messages: Iterable[tuple[int, int, int, int, Any]],
) -> None:
    """造 `message_0.db` 的明文版本：每个群一张 Msg_<md5>，外加 Name2Id。"""
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA page_size = 4096")
        connection.execute("PRAGMA journal_mode = DELETE")
        for group_id in groups:
            connection.execute(
                'CREATE TABLE "%s" (local_id INTEGER, local_type INTEGER, '
                "real_sender_id INTEGER, create_time INTEGER, message_content BLOB)"
                % message_table_name(group_id)
            )
            connection.executemany(
                'INSERT INTO "%s" VALUES (?, ?, ?, ?, ?)' % message_table_name(group_id),
                list(messages),
            )
        connection.execute("CREATE TABLE Name2Id (user_name TEXT)")
        connection.executemany("INSERT INTO Name2Id (rowid, user_name) VALUES (?, ?)", [(2, "wxid_e2e_sender")])
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()


def build_contact_db(path: Path, groups: Iterable[tuple[str, str]]) -> None:
    """造 `contact.db` 的明文版本（群名解析用）。"""
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA page_size = 4096")
        connection.execute(
            "CREATE TABLE contact (id INTEGER, username TEXT, nick_name TEXT, remark TEXT, alias TEXT)"
        )
        connection.executemany(
            "INSERT INTO contact (username, nick_name, remark) VALUES (?, ?, '')",
            [(group_id, name) for group_id, name in groups],
        )
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()


# --------------------------------------------------------------------------
# 加密（SQLCipher v4，页 key = 手填 raw key）
# --------------------------------------------------------------------------
def _cbc_encrypt(data: bytes, aes: Any, iv: bytes) -> bytes:
    if len(data) % 16 or len(iv) != 16:
        raise ValueError("CBC 输入必须 16 字节对齐且 IV 为 16 字节")
    out = []
    previous = iv
    for offset in range(0, len(data), 16):
        block = bytes(a ^ b for a, b in zip(data[offset:offset + 16], previous))
        cipher = aes.block_encrypt(block)
        out.append(cipher)
        previous = cipher
    return b"".join(out)


def _page_hmac(key: bytes, salt: bytes, mac_data: bytes, page_no: int) -> bytes:
    mac_salt = bytes(value ^ 0x3A for value in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, 32)
    return hmac.new(mac_key, mac_data + struct.pack("<I", page_no), hashlib.sha512).digest()


def rebase_reserve(plain: bytes, reserve: int = RESERVE_SIZE) -> bytes:
    """把「reserve=0 的普通 sqlite 页」平移成「尾部 reserve 字节空闲」的页。

    为什么需要这一步：SQLCipher 的 80 字节 reserve 是建库时用 C 接口
    `SQLITE_FCNTL_RESERVE_BYTES` 设的，Python `sqlite3` 设不出来；而接收端解密后固定把
    每页最后 80 字节补 0（`wechat_receiver.decrypt_page`）。普通库的 B-tree 单元是从页尾
    倒着排的，最后 80 字节里有真实数据，直接加密会丢 → 必须先把内容整体上移 80 字节。

    平移规则（只动偏移，不动内容顺序）：
    * 内容区 `[content_start, 4096)` → `[content_start-80, 4016)`；
    * 单元指针数组、freeblock 链、`content_start` 字段一律减 80；
    * `fragmented free bytes` 加 80（否则 `pragma integrity_check` 会每页报一条
      "Fragmentation of 80 bytes reported as 0"）。
    """
    if len(plain) % PAGE_SIZE:
        raise ValueError("明文库长度不是 4096 的整数倍：%d" % len(plain))
    rebased = bytearray()
    for index in range(len(plain) // PAGE_SIZE):
        page = plain[index * PAGE_SIZE:(index + 1) * PAGE_SIZE]
        page_no = index + 1
        header = 100 if page_no == 1 else 0
        page_type = page[header]
        if page_type not in (2, 5, 10, 13):
            raise ValueError("页 %d 类型未知：%d" % (page_no, page_type))
        interior = page_type in (2, 5)
        pointer_start = header + (12 if interior else 8)
        cells = int.from_bytes(page[header + 3:header + 5], "big")
        content_start = int.from_bytes(page[header + 5:header + 7], "big")
        if content_start == 0 or content_start > PAGE_SIZE:
            raise ValueError("页 %d 的 content_start 异常：%d" % (page_no, content_start))
        shifted = content_start - reserve
        if shifted < pointer_start + 2 * cells:
            raise ValueError("页 %d 平移后放不下（content=%d cells=%d）" % (page_no, content_start, cells))
        new_page = bytearray(PAGE_SIZE)
        new_page[:shifted] = page[:shifted]
        new_page[shifted:PAGE_SIZE - reserve] = page[content_start:PAGE_SIZE]
        for cell_index in range(cells):
            offset = pointer_start + 2 * cell_index
            value = struct.unpack(">H", page[offset:offset + 2])[0]
            if value >= content_start:
                struct.pack_into(">H", new_page, offset, value - reserve)
        freeblock = struct.unpack(">H", page[header + 1:header + 3])[0]
        if freeblock:
            struct.pack_into(">H", new_page, header + 1, freeblock - reserve)
            while freeblock:
                following, size = struct.unpack(">HH", page[freeblock:freeblock + 4])
                struct.pack_into(
                    ">HH", new_page, freeblock - reserve,
                    (following - reserve) if following else 0, size,
                )
                freeblock = following
        struct.pack_into(">H", new_page, header + 5, shifted)
        fragmented = page[header + 7] + reserve
        new_page[header + 7] = fragmented if fragmented <= 255 else 255
        rebased += new_page
    return bytes(rebased)


def encrypt_database(plain: bytes, key: bytes, salt: bytes | None = None) -> bytes:
    """把明文 sqlite 字节加密成 SQLCipher v4 页格式（receiver 能直接解）。"""
    if len(key) != 32:
        raise ValueError("raw key 必须是 32 字节")
    if len(plain) % PAGE_SIZE:
        raise ValueError("明文库长度不是 4096 的整数倍：%d" % len(plain))
    salt = salt or os.urandom(16)
    if len(salt) != 16:
        raise ValueError("盐必须是 16 字节")
    pages = []
    with CngAesEcb(key) as aes:
        for index in range(len(plain) // PAGE_SIZE):
            page_no = index + 1
            block = plain[index * PAGE_SIZE:(index + 1) * PAGE_SIZE]
            iv = os.urandom(16)
            if page_no == 1:
                cipher = _cbc_encrypt(block[16:PAGE_SIZE - RESERVE_SIZE], aes, iv)
                page = salt + cipher + iv
                page += _page_hmac(key, salt, page[16:PAGE_SIZE - RESERVE_SIZE + 16], page_no)
            else:
                cipher = _cbc_encrypt(block[:PAGE_SIZE - RESERVE_SIZE], aes, iv)
                page = cipher + iv
                page += _page_hmac(key, salt, page[16:PAGE_SIZE - RESERVE_SIZE + 16], page_no)
            if len(page) != PAGE_SIZE:
                raise AssertionError("加密页长度异常：%d" % len(page))
            pages.append(page)
    return b"".join(pages)


# --------------------------------------------------------------------------
# Windows 独占句柄（造「无权限」）
# --------------------------------------------------------------------------
GENERIC_READ = 0x80000000
FILE_SHARE_NONE = 0x00000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def _lock_file(path: Path) -> int | None:
    """用 share=0 打开文件，直到返回的句柄被关掉之前别人读不了（Windows 语义）。"""
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    handle = kernel32.CreateFileW(
        str(path), GENERIC_READ, FILE_SHARE_NONE, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None
    )
    if handle in (None, INVALID_HANDLE_VALUE):
        raise OSError("CreateFileW 失败，错误码 %d" % ctypes.get_last_error())
    return int(handle)


def _unlock_file(handle: int | None) -> None:
    if not handle or os.name != "nt":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle(ctypes.c_void_p(handle))


# --------------------------------------------------------------------------
# fixture 本体
# --------------------------------------------------------------------------
def _default_run_dir(run_id: str | None = None) -> Path:
    """替身目录的默认落点：`%TEMP%\\nh-e2e-<run-id>\\wechat`（见 `_rundir.py`）。"""
    return default_run_dir("wechat", run_id)


class WechatStore:
    def __init__(
        self,
        *,
        mode: str = "ok",
        root: Path | str | None = None,
        run_id: str | None = None,
        wxid: str = "wxid_e2e_demo",
        groups: Iterable[str] | None = None,
        group_names: dict[str, str] | None = None,
        messages: Iterable[tuple[int, int, int, int, Any]] | None = None,
        key_hex: str | None = None,
    ) -> None:
        if mode not in {"ok", "locked", "missing"}:
            raise ValueError("mode 只支持 ok / locked / missing，拿到 %r" % (mode,))
        self.mode = mode
        self.wxid = wxid
        self.groups = list(groups or [DEFAULT_GROUP])
        self.group_names = dict(group_names or {self.groups[0]: DEFAULT_GROUP_NAME})
        self.messages = list(messages if messages is not None else DEFAULT_MESSAGES)
        self.key_hex = key_hex or os.urandom(32).hex()
        # 运行产物一律落 %TEMP%\nh-e2e-<run-id>\，绝不进仓库（E2E 硬要求）。
        if root is None:
            self._created_run_dir = True
            self.root = _default_run_dir(run_id)
        else:
            self._created_run_dir = True
            self.root = Path(root)
        self.handle: int | None = None
        self.storage: Path | None = None
        self._build()

    # ---- 路径 ----
    @property
    def account_dir(self) -> Path:
        return self.root / self.wxid

    @property
    def storage_dir(self) -> Path:
        return self.account_dir / "db_storage"

    def _build(self) -> None:
        if self.mode == "missing":
            return
        storage = self.storage_dir
        (storage / "message").mkdir(parents=True, exist_ok=True)
        (storage / "contact").mkdir(parents=True, exist_ok=True)
        # 明文临时库也放这个 run 目录下（别用 tempfile：沙箱里 gettempdir() 可能回落成仓库根）
        staging = self.root / "_plain"
        staging.mkdir(parents=True, exist_ok=True)
        plain_message = staging / "message_0.db"
        plain_contact = staging / "contact.db"
        try:
            build_message_db(plain_message, self.groups, self.messages)
            build_contact_db(plain_contact, [(g, self.group_names.get(g, g)) for g in self.groups])
            key = bytes.fromhex(self.key_hex)
            (storage / "message" / "message_0.db").write_bytes(
                encrypt_database(rebase_reserve(plain_message.read_bytes()), key)
            )
            (storage / "contact" / "contact.db").write_bytes(
                encrypt_database(rebase_reserve(plain_contact.read_bytes()), key)
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        self.storage = storage
        if self.mode == "locked":
            self.handle = _lock_file(storage / "message" / "message_0.db")

    # ---- 契约 ----
    @property
    def port(self) -> None:
        return None

    def env(self) -> dict[str, str]:
        """剧本要写进被测实例 .env 的两行。state≠ok 时也给 key，便于对照负例。"""
        return {DIR_ENV: str(self.root), KEY_ENV: self.key_hex}

    def info(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "root": str(self.root),
            "storage": str(self.storage) if self.storage else None,
            "groups": list(self.groups),
            "group_names": dict(self.group_names),
            "messages": len(self.messages),
            "locked": bool(self.handle),
        }

    def stop(self) -> None:
        _unlock_file(self.handle)
        self.handle = None
        if self._created_run_dir:
            shutil.rmtree(self.root, ignore_errors=True)
            try:
                self.root.parent.rmdir()  # 只删空的 nh-e2e-<run-id>；harness 还有别的产物就留着
            except OSError:
                pass


def start(
    mode: str = "ok",
    root: Path | str | None = None,
    run_id: str | None = None,
    wxid: str = "wxid_e2e_demo",
    groups: Iterable[str] | None = None,
    group_names: dict[str, str] | None = None,
    messages: Iterable[tuple[int, int, int, int, Any]] | None = None,
    key_hex: str | None = None,
) -> dict[str, Any]:
    """统一入口：造目录树并返回 handle 字典（与 fakes/__init__.start 的契约一致）。"""
    fake = WechatStore(
        mode=mode, root=root, run_id=run_id, wxid=wxid, groups=groups,
        group_names=group_names, messages=messages, key_hex=key_hex,
    )
    return {
        "name": "wechat",
        "kind": "dir",
        "port": None,
        "base_url": None,
        "env": fake.env(),
        "info": fake.info(),
        "impl": fake,
        "stop": fake.stop,
    }
