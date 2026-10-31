"""`updates_api._applying_flag_present` 的读失败路径：断言**返回值**，不只是「不抛异常」。

背景：`noticehub/updates_api.py:445-450` 在 `is_file()` 抛 OSError 时记 warning 并返回
`False`。这条 except 分支此前没有任何测试覆盖（全仓零引用 `updates_api`）。
"""

from __future__ import annotations

import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from noticehub import updates_api
from noticehub import webapp as _webapp_module  # noqa: F401 - `_update_payload` 通过 sys.modules 取 webapp


def _server(data_dir: Path) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        settings=types.SimpleNamespace(data_dir=data_dir),
        update_lock=threading.Lock(),
        update_download={},
    )


class ApplyingFlagTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)

    def test_flag_file_present_reports_applying(self) -> None:
        updates_dir = self.data_dir / "updates"
        updates_dir.mkdir()
        (updates_dir / updates_api._APPLYING_FLAG_NAME).write_text("", encoding="utf-8")
        self.assertIs(updates_api._applying_flag_present(_server(self.data_dir)), True)

    def test_missing_flag_reports_not_applying(self) -> None:
        self.assertIs(updates_api._applying_flag_present(_server(self.data_dir)), False)

    def test_parent_is_a_file_returns_false_without_raising(self) -> None:
        """坏文件条件：`updates` 是个普通文件，flag 无处可查。

        pathlib 的 ``is_file()`` 会吞掉这类 OSError 并回 False，所以这里必须断言
        「返回 False」——只断言不抛异常是抓不到回归的。
        """
        (self.data_dir / "updates").write_text("not a directory", encoding="utf-8")
        self.assertIs(updates_api._applying_flag_present(_server(self.data_dir)), False)

    def test_state_path_oserror_returns_false_and_warns(self) -> None:
        """真正的 except 分支：解析状态文件路径时 OSError。

        `Path.is_file()`（follow_symlinks=True）内部走 ``os.path.isfile``，会把所有
        OSError 吞成 False，所以文件系统层面造不出这条分支，只能从依赖点注入。
        """
        server = _server(self.data_dir)
        with mock.patch.object(updates_api, "_updates_state_path", side_effect=OSError("permission denied")):
            with self.assertLogs("noticehub.webapp", level="WARNING") as logs:
                self.assertIs(updates_api._applying_flag_present(server), False)
        self.assertIn("读取更新安装标记失败", "\n".join(logs.output))

    def test_payload_surfaces_false_when_flag_read_fails(self) -> None:
        """调用点契约：读失败时 `/api/update` 载荷里的 applying 必须是 False。"""
        server = _server(self.data_dir)
        cached = {"ok": True, "has_update": False, "applying": True}
        with mock.patch.object(updates_api, "_updates_state_path", side_effect=OSError("boom")):
            with self.assertLogs("noticehub.webapp", level="WARNING"):
                payload = updates_api._update_payload(server, cached, checking=False)
        self.assertIs(payload["applying"], False)


if __name__ == "__main__":
    unittest.main()
