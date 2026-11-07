"""`updates_api._applying_flag_present` 的读失败路径：断言**返回值**，不只是「不抛异常」。

背景：`noticehub/updates_api.py:445-450` 在 `is_file()` 抛 OSError 时记 warning 并返回
`False`。这条 except 分支此前没有任何测试覆盖（全仓零引用 `updates_api`）。
"""

from __future__ import annotations

import json
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


class WriteUpdatesStateRetryTests(unittest.TestCase):
    """覆盖写重试：撞锁时要接着等，等到头了要吵出来，不许静默丢写。

    `_write_updates_state` 用 `tmp.replace(path)` 覆盖写，而这份 `state.json`
    启动器也在读写；Windows 上目标文件被任何读句柄打开过，`MoveFileEx` 就报
    sharing violation。真实环境里两个进程抢这一份文件，所以重试窗口必须够宽，
    耗尽后仍必须 `LOGGER.warning` + `raise`——否则后台刷新线程会静默退出，
    页面上的版本状态永远停在旧值。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.state_path = updates_api._updates_state_path(_server(self.data_dir))

    def test_three_permission_errors_then_success(self) -> None:
        """连续 3 次 PermissionError 后第 4 次成功：写进去了、内容对、没吵。"""
        real_replace = Path.replace
        calls = {"n": 0}

        def flaky_replace(self_path: Path, target: Path) -> Path:
            calls["n"] += 1
            if calls["n"] <= 3:
                raise PermissionError(13, "file is being used by another process")
            return real_replace(self_path, target)

        state = {"update_result": {"channel": "beta"}}
        with mock.patch.object(Path, "replace", new=flaky_replace):
            with mock.patch.object(updates_api.LOGGER, "warning") as warned:
                updates_api._write_updates_state(self.state_path, state)
        self.assertEqual(calls["n"], 4, "应该先失败 3 次、第 4 次成功")
        warned.assert_not_called()
        written = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(written, state)

    def test_permission_errors_exhausted_raises_and_warns(self) -> None:
        """一直撞锁：重试用尽后必须 raise，且必须留下一条 warning（不许静默丢写）。"""

        def always_locked(self_path: Path, target: Path) -> Path:
            raise PermissionError(13, "file is being used by another process")

        with mock.patch.object(Path, "replace", new=always_locked):
            with self.assertLogs("noticehub.webapp", level="WARNING") as logs:
                with self.assertRaises(PermissionError):
                    updates_api._write_updates_state(self.state_path, {"ok": True})
        text = "\n".join(logs.output)
        self.assertIn("更新状态写入失败", text)
        self.assertIn(str(self.state_path), text)
        self.assertFalse(self.state_path.exists(), "重试用尽时不该留下半份状态文件")


class ApplyGateTests(unittest.TestCase):
    """并发闸门：连点两次「重启并更新」只能起一个安装脚本，失败后必须放开（P1-2）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = _server(Path(self.tmp.name))
        patcher = mock.patch.object(
            updates_api, "_webapp", return_value=types.SimpleNamespace(updater=object())
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_second_apply_while_first_runs_is_refused(self) -> None:
        calls: list[int] = []

        def fake_apply(server):
            calls.append(1)
            return {"ok": True, "restarting": True, "script": "apply.ps1"}

        with mock.patch.object(updates_api, "_apply_update_once", side_effect=fake_apply):
            first = updates_api._apply_update(self.server)
            second = updates_api._apply_update(self.server)
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertIs(second.get("already_running"), True)
        self.assertIn("正在安装更新", str(second.get("error")))
        self.assertEqual(len(calls), 1, "第二次不该再起一个安装脚本")

    def test_failed_apply_releases_the_gate_for_retry(self) -> None:
        with mock.patch.object(
            updates_api, "_apply_update_once", return_value={"ok": False, "error": "还没下载好更新包"}
        ):
            first = updates_api._apply_update(self.server)
        self.assertFalse(first["ok"])
        self.assertIs(
            getattr(self.server, "update_applying", False),
            False,
            "失败后必须放开闸门，否则用户永远重试不了",
        )
        with mock.patch.object(
            updates_api, "_apply_update_once", return_value={"ok": True, "restarting": True}
        ):
            second = updates_api._apply_update(self.server)
        self.assertTrue(second["ok"], "放开闸门后重试必须能走通")


class PatchPayloadTests(unittest.TestCase):
    """同版本修复包：补丁号、待装记录、回退快照三样都要如实进 /api/update。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.server = _server(self.data_dir)

    def _with_updater(self, updater: object) -> None:
        patcher = mock.patch.object(
            updates_api, "_webapp", return_value=types.SimpleNamespace(updater=updater)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_same_version_patch_is_reported_while_has_update_stays_false(self) -> None:
        self._with_updater(
            types.SimpleNamespace(current_patch=lambda: 1, pending_update=lambda data_dir: None)
        )
        cached = {"ok": True, "has_update": False, "patch_latest": 2, "patch_update": True}
        payload = updates_api._update_payload(self.server, cached, checking=False)
        self.assertIs(payload["has_update"], False, "同版本补丁不许被说成新版本")
        self.assertEqual(
            payload["patch"], {"current": 1, "latest": 2, "update": True, "auto": True}
        )

    def test_pending_patch_is_carried_and_stops_crying_download(self) -> None:
        record = {
            "version": "1.1.0-beta.18",
            "at": "2026-11-07T05:00:00+00:00",
            "patch": 2,
        }
        self._with_updater(
            types.SimpleNamespace(current_patch=lambda: 2, pending_update=lambda data_dir: record)
        )
        cached = {"ok": True, "has_update": False, "patch_latest": 2, "patch_update": True}
        payload = updates_api._update_payload(self.server, cached, checking=False)
        self.assertEqual(payload["pending"], record)
        self.assertIs(
            payload["patch"]["update"], False, "补丁已经下好了，不该继续喊「可下载」"
        )

    def test_patch_auto_follows_the_setting(self) -> None:
        self.server.settings.patch_auto = False  # type: ignore[attr-defined]
        self._with_updater(
            types.SimpleNamespace(current_patch=lambda: 0, pending_update=lambda data_dir: None)
        )
        payload = updates_api._update_payload(
            self.server, {"ok": True, "has_update": False}, checking=False
        )
        self.assertIs(payload["patch"]["auto"], False)

    def test_rollback_is_unavailable_without_a_backup(self) -> None:
        self._with_updater(
            types.SimpleNamespace(
                _latest_backup=lambda data_dir: None, _patch_from_backup=lambda path: 0
            )
        )
        payload = updates_api._update_payload(self.server, {"ok": True}, checking=False)
        self.assertEqual(payload["rollback"], {"available": False, "patch": 0, "at": ""})

    def test_rollback_reports_the_patch_inside_the_backup(self) -> None:
        backup = self.data_dir / "updates" / "rollback" / "prev-1.zip"
        backup.parent.mkdir(parents=True)
        backup.write_bytes(b"PK\x03\x04")
        self._with_updater(
            types.SimpleNamespace(
                _latest_backup=lambda data_dir: backup, _patch_from_backup=lambda path: 1
            )
        )
        payload = updates_api._update_payload(self.server, {"ok": True}, checking=False)
        self.assertIs(payload["rollback"]["available"], True)
        self.assertEqual(payload["rollback"]["patch"], 1)
        self.assertTrue(payload["rollback"]["at"], "有备份就该带备份时间")


if __name__ == "__main__":
    unittest.main()
