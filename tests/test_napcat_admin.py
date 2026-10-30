import io
import json
import hashlib
import os
import sys
import tempfile
import time
import types
import urllib.error
import zipfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qq_live_digest import napcat_admin as na

class S:
    onebot_port = 8765
    onebot_token = "one"
    napcat_api_url = "http://127.0.0.1:3001"
    napcat_api_token = "api"
    napcat_qr_path = Path(tempfile.gettempdir()) / "qq-notice-hub-test-qr.png"
    data_dir = Path(tempfile.gettempdir()) / "qq-notice-hub-test-data"

class NapcatAdminTests(unittest.TestCase):
    def setUp(self):
        # launch() 会探测「本安装的 NapCat 是否已经在跑」。测试不许依赖真机进程，
        # 也不该为此去起一个 powershell；默认按「没有实例在跑」处理。
        # 需要走这条路径的用例自己再 patch 一次覆盖它。
        self._pids_patcher = patch.object(na, "_matching_pids", return_value=[])
        self._pids_patcher.start()
        self.addCleanup(self._pids_patcher.stop)
        # launch() 现在启动前会补齐 OneBot 配置。测试里 boot 的 data_dir 就是当前目录，
        # 不拦住它就会往仓库根目录写 config/onebot11*.json（测试污染工作区）。
        # 需要断言「补齐」的用例自己再 patch 一层拿 mock。
        self._config_patcher = patch.object(
            na, "ensure_onebot_config",
            return_value={"ok": True, "written": [], "port": 3001, "servers": [], "error": None},
        )
        self._config_patcher.start()
        self.addCleanup(self._config_patcher.stop)

    def test_ports_accept_any_listening_local_address(self):
        text = "  TCP    0.0.0.0:6099    0.0.0.0:0    LISTENING    10\n  TCP    [::]:6099       [::]:0       LISTENING    10\n"
        self.assertIn(6099, na._ports(text))

    def test_live_root_wins_even_when_later(self):
        with tempfile.TemporaryDirectory() as td:
            first, second = Path(td) / "first", Path(td) / "second"
            for root in (first, second):
                (root / "config").mkdir(parents=True)
                (root / "config" / "webui.json").write_text('{"port":6199}', encoding="utf-8")
            (second / "config" / "onebot11_2.json").write_text(json.dumps({"network":{"httpServers":[],"httpClients":[]}}), encoding="utf-8")
            roots = [na.NapcatRoot(first, 6199, "", ["1"], False), na.NapcatRoot(second, 6199, "", ["2"], True)]
            # 不能依赖真机的 detect_boot()：它的 data_dir 兜底按文件 mtime 取最大，而 NTFS 时间戳
            # 在同一刻度上会打平，max() 打平时取**第一个**候选，于是 uin 偶发变成 "1"。
            # 「本机装了 QQ」同理要显式 patch：CI runner 没装 QQ 时会走 stage="qq_missing" 分支，
            # 断言就随机器环境飘（这条现在只断 uin 所以还过得去，但不是靠运气该过的地方）。
            with patch.object(na, "find_roots", return_value=roots), patch.object(na, "detect_boot", return_value=None), patch.object(na, "_request", side_effect=OSError("offline")), patch.object(na, "restart", return_value={"ok": False, "error": "test"}), patch.object(na, "find_qq_exe", return_value=first / "QQ.exe"):
                result = na.auto_setup(S())
            self.assertEqual(result["uin"], "2")

    def test_file_mtime_beats_directory_mtime(self):
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td) / "a", Path(td) / "b"
            for root in (a, b):
                (root / "config").mkdir(parents=True)
                (root / "config" / "webui.json").write_text('{"port":6199}', encoding="utf-8")
                (root / "config" / "onebot11_x.json").write_text('{"network": {}}', encoding="utf-8")
            import os
            os.utime(a / "config", (30, 30)); os.utime(a / "config" / "webui.json", (5, 5)); os.utime(a / "config" / "onebot11_x.json", (10, 10))
            os.utime(b / "config", (10, 10)); os.utime(b / "config" / "webui.json", (5, 5)); os.utime(b / "config" / "onebot11_x.json", (30, 30))
            self.assertGreater(na._root_mtime(na.NapcatRoot(b, 6199, "", ["x"], True)), na._root_mtime(na.NapcatRoot(a, 6199, "", ["x"], True)))

    def test_qrcode_mtime_breaks_tie(self):
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td) / "a", Path(td) / "b"
            for root in (a, b):
                (root / "config").mkdir(parents=True); (root / "cache").mkdir()
                (root / "config" / "onebot11_x.json").write_text('{}')
            (b / "cache" / "qrcode.png").write_bytes(b"qr")
            self.assertGreater(na._qr_mtime(na.NapcatRoot(b, 6199, "", [], True)), na._qr_mtime(na.NapcatRoot(a, 6199, "", [], True)))

    def test_launch_skips_when_already_running(self):
        with patch.object(na, "_ports", return_value={3001}), patch.object(na.subprocess, "Popen") as popen:
            result = na.launch(S(), uin="123")
        self.assertTrue(result["already_running"])
        popen.assert_not_called()

    def test_launch_does_not_spawn_a_second_instance_while_waiting_for_login(self):
        """用户报的「二维码不刷新、点击也没反应」：NapCat 的 OneBot 端口要等手机扫码
        登录成功之后才监听，而「等扫码」可能持续几分钟。以前只按端口判断「在不在跑」，
        于是这段等待期里每点一次「启动并登录」就再拉一套 NapCat + QQ；两套实例抢同一个
        --user-data-dir，谁都登不进去、端口永远不开，二维码看着一直在变却永远扫不进去。
        """
        boot = {"boot_exe": "b", "qq_exe": "q", "hook_dll": "h", "data_dir": "."}
        with patch.object(na, "_ports", return_value=set()), \
             patch.object(na, "detect_boot", return_value=boot), \
             patch.object(na, "_matching_pids", return_value=[4242]) as pids_probe, \
             patch.object(na.subprocess, "Popen") as popen:
            result = na.launch(S(), uin="123")
        self.assertTrue(result["ok"])
        self.assertTrue(result["already_running"])
        self.assertTrue(result["starting"])
        self.assertEqual(result["pid"], 4242)
        popen.assert_not_called()
        # 兜底必须按本安装的 profile 目录匹配，绝不能退回按进程名匹配
        # （NapCatWinBootMain.exe 的命令行里必然含自己的文件名，按名字等于杀光全机实例）。
        pattern = Path(pids_probe.call_args.args[0])
        self.assertEqual(pattern, (S.data_dir / "napcat-profile").resolve())
        self.assertNotIn("NapCatWinBootMain", str(pattern))

    def test_running_pids_keys_on_this_install_profile(self):
        """状态查询靠这个判断「引擎在不在跑」：只认本安装的 profile 目录。

        同一台机器上可能有别人的 NapCat（比如 F:\\NapCat 用另一个 --user-data-dir），
        所以既不能按进程名算，也不能拿端口算 —— 端口要等登录成功才开。
        """
        with patch.object(na, "_matching_pids", return_value=[11, 22]) as probe:
            self.assertEqual(na.running_pids(S()), [11, 22])
        self.assertEqual(Path(probe.call_args.args[0]), (S.data_dir / "napcat-profile").resolve())
        self.assertNotIn("NapCatWinBootMain", probe.call_args.args[0])
        # 探测本身出错（比如 powershell 不可用）只当「没在跑」，不能把状态接口打挂。
        with patch.object(na, "_matching_pids", side_effect=OSError("no powershell")):
            self.assertEqual(na.running_pids(S()), [])

    def test_ensure_onebot_config_writes_an_enabled_http_server(self) -> None:
        # setUp 默认把 ensure_onebot_config 换成了桩；这个用例要跑真函数。
        self._config_patcher.stop()
        """NapCat 只在启动时读 config/onebot11_<uin>.json。那个文件里没有启用的 httpServers，
        它就根本不会开 OneBot 的 HTTP 接口，3001 永远不监听 —— 于是「扫码登录成功」在这套
        程序里什么都不发生，页面连一句反馈都给不出来（用户实测报的恶性 bug）。"""
        with tempfile.TemporaryDirectory() as td:
            engine = Path(td) / "engine"
            (engine / "config").mkdir(parents=True)
            # NapCat 自己的默认配置：network 全空，正是出问题的那份
            (engine / "config" / "onebot11_12345.json").write_text(
                json.dumps({"network": {"httpServers": [], "httpClients": []}}), encoding="utf-8"
            )
            result = na.ensure_onebot_config(S(), engine=engine)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["port"], 3001)
            self.assertIn("onebot11_12345.json", result["written"])
            # 兜底文件也要有：NapCat 还没为某个号建过配置时也得有可用的
            self.assertIn("onebot11.json", result["written"])
            cfg = json.loads((engine / "config" / "onebot11_12345.json").read_text(encoding="utf-8"))
            servers = cfg["network"]["httpServers"]
            self.assertEqual(len(servers), 1)
            self.assertTrue(servers[0]["enable"])
            self.assertEqual(servers[0]["port"], 3001)
            # 事件回传的 httpClients 也得在，否则收到的消息不会送进本程序
            self.assertTrue(cfg["network"]["httpClients"])
            self.assertEqual(cfg["network"]["httpClients"][0]["url"], "http://127.0.0.1:8765")

    def test_ensure_onebot_config_covers_an_explicit_uin_and_reports_servers(self) -> None:
        self._config_patcher.stop()
        with tempfile.TemporaryDirectory() as td:
            engine = Path(td) / "engine"
            (engine / "config").mkdir(parents=True)
            result = na.ensure_onebot_config(S(), engine=engine, uin="999")
            self.assertTrue(result["ok"], result)
            self.assertIn("onebot11_999.json", result["written"])
            self.assertEqual(na.onebot_config_servers(engine), [
                {"file": "onebot11.json", "port": 3001, "host": "127.0.0.1"},
                {"file": "onebot11_999.json", "port": 3001, "host": "127.0.0.1"},
            ])

    def test_launch_reports_config_failure_and_does_not_spawn(self) -> None:
        boot = {"boot_exe": "b", "qq_exe": "q", "hook_dll": "h", "data_dir": "."}
        with patch.object(na, "_ports", return_value=set()), patch.object(na, "detect_boot", return_value=boot), patch.object(na, "_ensure_qq_loader", return_value=None), patch.object(na, "ensure_onebot_config", return_value={"ok": False, "error": "拒绝访问"}), patch.object(na.subprocess, "Popen") as popen:
            result = na.launch(S(), uin="123")
        self.assertFalse(result["ok"])
        self.assertIn("引擎配置没写成功", result["error"])
        self.assertIn("拒绝访问", result["error"])
        popen.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows netstat-based NapCat restart")
    def test_restart_reports_old_stopped_when_config_fails(self) -> None:
        with patch.object(na, "stop", return_value={"ok": True, "stopped": [7], "already_stopped": False}), patch.object(na, "launch", return_value={"ok": False, "error": "引擎配置没写成功：拒绝访问"}):
            result = na.restart(S(), wait_seconds=0)
        self.assertFalse(result["ok"])
        self.assertIn("旧的 NapCat 已经停了", result["error"])
        self.assertIn("引擎配置没写成功", result["error"])

    def test_launch_repairs_the_onebot_config_before_spawning(self) -> None:
        """「启动并登录」也必须先补齐配置：只点它、不点「一键接入」的人同样要能用。"""
        boot = {"boot_exe": "b", "qq_exe": "q", "hook_dll": "h", "data_dir": "."}
        child = type("Child", (), {"pid": 7})()
        repaired = {"ok": True, "written": ["onebot11.json"], "port": 3001, "servers": [], "error": None}
        with patch.object(na, "_ports", return_value=set()), \
             patch.object(na, "detect_boot", return_value=boot), \
             patch.object(na, "_ensure_qq_loader", return_value=None), \
             patch.object(na, "ensure_onebot_config", return_value=repaired) as ensure, \
             patch.object(na.subprocess, "Popen", return_value=child) as popen:
            result = na.launch(S(), uin="123")
        self.assertTrue(result["ok"])
        ensure.assert_called_once()
        popen.assert_called_once()
        self.assertEqual(result["onebot_config"], repaired)

    def test_diagnose_points_at_the_missing_onebot_http_server(self) -> None:
        """这正是「服务自检」过去查不出来的那一格：配置里没有 httpServers。

        旧自检只看 /api/napcat/status，而那个接口本身依赖 OneBot API —— API 起不来的
        根因它永远看不见，用户只看到「扫码登录了但什么都没发生」。
        """
        with tempfile.TemporaryDirectory() as td:
            engine = Path(td) / "engine"
            (engine / "config").mkdir(parents=True)
            (engine / "config" / "onebot11_12345.json").write_text(
                json.dumps({"network": {"httpServers": []}}), encoding="utf-8"
            )
            boot = {"boot_exe": str(engine / "NapCatWinBootMain.exe"), "qq_exe": "q", "hook_dll": "h", "data_dir": str(engine)}
            with patch.object(na, "detect_boot", return_value=boot), patch.object(na, "_ports", return_value=set()):
                report = na.diagnose(S())
        self.assertFalse(report["ok"])
        self.assertIn("httpServer", report["broken"])
        self.assertIn("重启引擎", report["hint"])
        self.assertTrue(report["steps"][0]["ok"])
        self.assertFalse(report["steps"][-1]["ok"])

    def test_diagnose_reports_waiting_for_login_when_the_port_is_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            engine = Path(td) / "engine"
            (engine / "config").mkdir(parents=True)
            (engine / "config" / "onebot11_12345.json").write_text(
                json.dumps({"network": {"httpServers": [{"enable": True, "port": 3001, "host": "127.0.0.1"}]}}),
                encoding="utf-8",
            )
            boot = {"boot_exe": str(engine / "NapCatWinBootMain.exe"), "qq_exe": "q", "hook_dll": "h", "data_dir": str(engine)}
            with patch.object(na, "detect_boot", return_value=boot), \
                 patch.object(na, "running_pids", return_value=[42]), \
                 patch.object(na, "_ports", return_value=set()):
                report = na.diagnose(S())
        self.assertFalse(report["ok"])
        self.assertIn("3001", report["broken"])
        self.assertIn("二维码", report["hint"])
        self.assertTrue(all(step["ok"] for step in report["steps"][:-1]))

    def test_diagnose_is_green_when_every_link_is_up(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            engine = Path(td) / "engine"
            (engine / "config").mkdir(parents=True)
            (engine / "config" / "onebot11_12345.json").write_text(
                json.dumps({"network": {"httpServers": [{"enable": True, "port": 3001, "host": "127.0.0.1"}]}}),
                encoding="utf-8",
            )
            boot = {"boot_exe": str(engine / "NapCatWinBootMain.exe"), "qq_exe": "q", "hook_dll": "h", "data_dir": str(engine)}
            with patch.object(na, "detect_boot", return_value=boot), \
                 patch.object(na, "running_pids", return_value=[42]), \
                 patch.object(na, "_ports", return_value={3001}):
                report = na.diagnose(S())
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["broken"], "")
        self.assertTrue(all(step["ok"] for step in report["steps"]))

    def test_diagnose_says_so_when_no_engine_is_found(self) -> None:
        with patch.object(na, "detect_boot", return_value=None):
            report = na.diagnose(S())
        self.assertFalse(report["ok"])
        self.assertIn("引擎", report["broken"])
        self.assertIn("QQ_DIGEST_NAPCAT_DIR", report["hint"])

    def test_launch_ignores_qq_webui_port_6099(self):
        """用户开着日常 QQ（D:\QQ\QQ.exe 监听 6099）时不得误判为 NapCat 已运行。"""
        with patch.object(na, "_ports", return_value={6099}), patch.object(na.subprocess, "Popen") as popen, patch.object(na, "detect_boot", return_value={"boot_exe": "b", "qq_exe": "q", "hook_dll": "h", "data_dir": "."}):
            result = na.launch(S(), uin="123")
        self.assertFalse(result["already_running"])
        popen.assert_called_once()

    def test_launch_follows_configured_api_port(self):
        """改过 QQ_DIGEST_NAPCAT_API_URL 时，必须在配置端口上判「已在运行」，不能写死 3001。"""
        boot = {"boot_exe": "b", "qq_exe": "q", "hook_dll": "h", "data_dir": "."}
        custom = type("S", (), {"napcat_api_url": "http://127.0.0.1:3999", "data_dir": Path(tempfile.gettempdir())})()
        with patch.object(na, "detect_boot", return_value=boot), patch.object(na, "_ports", return_value={3999}), patch.object(na.subprocess, "Popen") as popen:
            result = na.launch(custom, uin="123")
        self.assertTrue(result["already_running"])
        popen.assert_not_called()
        # 配置端口没开、只有默认 3001 开着时，不能误判成本安装的 NapCat 在跑。
        with patch.object(na, "detect_boot", return_value=boot), patch.object(na, "_ports", return_value={3001}), patch.object(na.subprocess, "Popen") as popen:
            result = na.launch(custom, uin="123")
        self.assertFalse(result["already_running"])
        popen.assert_called_once()

    def test_launch_scan_mode_omits_quick_login(self):
        boot = {"boot_exe": str(Path(tempfile.gettempdir()) / "boot.exe"), "qq_exe": str(Path(tempfile.gettempdir()) / "QQ.exe"), "hook_dll": str(Path(tempfile.gettempdir()) / "hook.dll"), "data_dir": tempfile.gettempdir()}
        child = type("Child", (), {"pid": 7})()
        with patch.object(na, "_ports", return_value=set()), patch.object(na, "detect_boot", return_value=boot), patch.object(na.subprocess, "Popen", return_value=child) as popen:
            result = na.launch(S(), uin=None, profile_dir=str(Path(tempfile.gettempdir()) / "napcat-test-profile"))
        self.assertTrue(result["ok"])
        self.assertNotIn("-q", result["command"])
        self.assertTrue(any(x.startswith("--user-data-dir=") for x in result["command"]))
        popen.assert_called_once()
        kwargs = popen.call_args.kwargs
        self.assertEqual(kwargs["env"]["NAPCAT_WORKDIR"], str(kwargs["cwd"]))
        self.assertTrue(kwargs["creationflags"] & 0x08000000)
        self.assertFalse(kwargs["creationflags"] & 0x00000008)

    def test_console_helpers_hide_windows(self):
        no_window = 0x08000000
        with patch.object(na.subprocess, "check_output", return_value="") as check_output:
            na._ports()
        self.assertTrue(check_output.call_args.kwargs["creationflags"] & no_window)
        # 这一段要的是真实链路 _kill_matching → _matching_pids → check_output，
        # 所以先撤掉 setUp 里那个「没有实例在跑」的替身。
        self._pids_patcher.stop()
        with patch.object(na.subprocess, "check_output", return_value="7"), patch.object(na.subprocess, "run") as run:
            na._kill_matching("NapCat")
        self.assertTrue(check_output.call_args.kwargs["creationflags"] & no_window)
        self.assertTrue(run.call_args.kwargs["creationflags"] & no_window)

    def _make_fake_tree(self, data_dir):
        root = Path(data_dir) / "napcat"
        (root / "config").mkdir(parents=True)
        (root / "NapCatWinBootMain.exe").write_bytes(b"boot")
        (root / "NapCatWinBootHook.dll").write_bytes(b"hook")
        (root / "napcat.mjs").write_text("// shell")
        (root / "loadNapCat.js").write_text("// loader")
        (root / "qqnt.json").write_text('{"main":"./loadNapCat.js"}')
        (root / "config" / "napcat.json").write_text("{}")
        return root

    def test_detect_boot_uses_external_qq_and_explicit_data_root(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / "data"
            shell = self._make_fake_tree(data)
            qq = self._make_qq_file(td)
            na._LAST_INSTALL_ROOT = None
            env = {"QQ_DIGEST_DATA_DIR": "", "QQ_DIGEST_NAPCAT_DIR": ""}
            with patch.dict(os.environ, env, clear=False), patch.object(na, "find_roots", return_value=[]), patch.object(na, "_registry_qq_exe", return_value=qq):
                result = na.detect_boot(data_dir=data)
            self.assertIsNotNone(result)
            self.assertEqual(Path(result["boot_exe"]).resolve(), (shell / "NapCatWinBootMain.exe").resolve())
            self.assertEqual(Path(result["qq_exe"]).resolve(), qq.resolve())
            self.assertEqual(Path(result["data_dir"]).resolve(), shell.resolve())

    def test_missing_root_without_qq_reports_qq_missing(self):
        with patch.object(na, "find_roots", return_value=[]), patch.object(na, "find_qq_exe", return_value=None), \
             patch.object(na, "install") as installer:
            result = na.auto_setup(S())
        # 没 QQ 就没必要先下几百 MB：直接给一句能行动的中文
        installer.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "qq_missing")
        self.assertIn("没找到官方 QQ", result["error"])
        self.assertNotIn("not found", result["error"].lower())

    def _root(self, td):
        root = Path(td)
        (root / "config").mkdir()
        (root / "cache").mkdir()
        (root / "config" / "webui.json").write_text('{"port":6199,"token":"t"}', encoding="utf-8")
        (root / "config" / "onebot11_123.json").write_text('{"network":{}}', encoding="utf-8")
        (root / "cache" / "qrcode.png").write_bytes(b"png")
        return na.NapcatRoot(root, 6199, "t", ["123"], False)

    def test_auto_setup_installs_when_napcat_is_missing_then_waits_for_login(self):
        """「一键接入」必须真的装：没装 → 下载安装 → 继续配置/启动 → 等扫码。"""
        with tempfile.TemporaryDirectory() as td:
            fake = self._root(td)
            calls = {"n": 0}

            def find_roots():
                # 第一次：没装；安装后再查（detect_boot 内部也会查）都返回装好的根
                calls["n"] += 1
                return [] if calls["n"] == 1 else [fake]

            with patch.object(na, "find_roots", side_effect=find_roots), \
                 patch.object(na, "find_qq_exe", return_value=Path("C:/QQ/QQ.exe")), \
                 patch.object(na, "download_url", return_value="https://example.invalid/napcat.zip"), \
                 patch.object(na, "_expected_package_sha256", return_value="b" * 64), \
                 patch.object(na, "install", return_value={"ok": True, "root": str(fake.root)}) as installer, \
                 patch.object(na, "_request", side_effect=OSError("offline")), \
                 patch.object(na, "_probe_api", return_value=(False, "connect refused")), \
                 patch.object(na, "restart", return_value={"ok": True, "pid": 4, "waiting_for_login": True}), \
                 patch.object(na, "_wait_for_new_qr", return_value=True):
                result = na.auto_setup(S())
        installer.assert_called_once()
        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], "waiting_for_login")
        self.assertTrue(result["waiting_for_login"])
        names = [step["name"] for step in result["steps"]]
        # 第一条必须是成功态（下载启动），否则前端展示「第一条 ok:false」会盖住真正的失败
        self.assertEqual(names[0], "download")
        self.assertTrue(result["steps"][0]["ok"])
        self.assertIn("install", names)
        install_step = result["steps"][names.index("install")]
        self.assertIn("https://example.invalid/napcat.zip", result["steps"][0]["detail"])
        self.assertIn("NapCat 已安装到", install_step["detail"])
        for step in result["steps"]:
            self.assertNotIn("not found", str(step["detail"]).lower())

    def test_auto_setup_reports_download_failure_with_source_and_fallback(self):
        with patch.object(na, "find_roots", return_value=[]), \
             patch.object(na, "find_qq_exe", return_value=Path("C:/QQ/QQ.exe")), \
             patch.object(na, "download_url", return_value="https://example.invalid/napcat.zip"), \
             patch.object(na, "_expected_package_sha256", return_value="b" * 64), \
             patch.object(na, "install", return_value={"ok": False, "error": "NapCat 安装失败：连接超时"}):
            result = na.auto_setup(S())
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "download_failed")
        # 前端展示「第一条 ok:false 的 detail」：必须就是真正的失败原因，不能是「未找到 NapCat」
        failing = [step for step in result["steps"] if not step["ok"]]
        self.assertEqual(len(failing), 1, failing)
        self.assertEqual(failing[0]["name"], "install")
        self.assertIn("连接超时", failing[0]["detail"])
        self.assertIn("https://example.invalid/napcat.zip", result["error"])
        self.assertIn("napcat", result["error"])
        # 前端读 reason 这个字段名拼提示
        self.assertIn("https://example.invalid/napcat.zip", result["reason"])
        self.assertIn("连接超时", result["reason"])
        self.assertNotIn("not found", result["error"].lower())

    def test_auto_setup_drops_previous_pending_package_before_downloading_again(self):
        """用户下了包没点确认：再点一次下载前，旧包必须先被清掉，不留 29MB 死文件。"""
        payload = b"PK\x03\x04" + b"e2e-package" * 64
        sha = hashlib.sha256(payload).hexdigest()
        old_folder = Path(tempfile.mkdtemp(prefix="napcat-pending-old-"))
        (old_folder / "NapCat.zip").write_bytes(payload)

        def fake_download(settings, source, on_progress):
            folder = Path(tempfile.mkdtemp(prefix="pending-"))
            archive = folder / "NapCat.zip"
            archive.write_bytes(payload)
            return {"ok": True, "path": str(archive), "folder": str(folder), "bytes": len(payload), "sha256": sha}

        with patch.object(na, "find_roots", return_value=[]), \
             patch.object(na, "find_qq_exe", return_value=Path("C:/QQ/QQ.exe")), \
             patch.object(na, "download_url", return_value="https://example.invalid/napcat.zip"), \
             patch.object(na, "_expected_package_sha256", return_value=""), \
             patch.object(na, "_download_package", side_effect=fake_download), \
             patch.object(na, "install") as installer:
            try:
                na._PENDING_PACKAGE = {"path": str(old_folder / "NapCat.zip"), "folder": str(old_folder),
                                       "sha256": sha, "source": "https://example.invalid/napcat.zip"}
                installer.assert_not_called()
                result = na.auto_setup(S())
                self.assertEqual(result["stage"], "fingerprint_required")
                self.assertFalse(old_folder.exists(), "旧 pending 目录没被清掉")
                self.assertTrue(Path(na._PENDING_PACKAGE["path"]).is_file())  # 新的这份留着
            finally:
                na._drop_pending_package()

    def test_auto_setup_sweeps_hour_old_pending_directories(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            stale = data_dir / "napcat-pending-stale"
            fresh = data_dir / "napcat-pending-fresh"
            other = data_dir / "napcat-stage-keepme"
            for folder in (stale, fresh, other):
                folder.mkdir()
            old = time.time() - 7200
            os.utime(stale, (old, old))
            na._sweep_stale_pending(data_dir)
            self.assertFalse(stale.exists(), "超过 1 小时的 pending 应被清掉")
            self.assertTrue(fresh.exists(), "新的 pending 不能动")
            self.assertTrue(other.exists(), "别的目录（不同前缀）不能动")

    def test_auto_setup_downloads_once_then_waits_for_fingerprint(self):
        """指纹没登记：下载一次、算出真指纹，但**不安装**，包留在临时目录等确认。"""
        payload = b"PK\x03\x04" + b"e2e-package" * 64
        sha = hashlib.sha256(payload).hexdigest()
        downloads = []

        def fake_download(settings, source, on_progress):
            downloads.append(source)
            folder = Path(tempfile.mkdtemp(prefix="pending-"))
            archive = folder / "NapCat.zip"
            archive.write_bytes(payload)
            return {"ok": True, "path": str(archive), "folder": str(folder), "bytes": len(payload), "sha256": sha}

        with patch.object(na, "find_roots", return_value=[]), \
             patch.object(na, "find_qq_exe", return_value=Path("C:/QQ/QQ.exe")), \
             patch.object(na, "download_url", return_value="https://example.invalid/napcat.zip"), \
             patch.object(na, "_expected_package_sha256", return_value=""), \
             patch.object(na, "_download_package", side_effect=fake_download), \
             patch.object(na, "install") as installer:
            try:
                result = na.auto_setup(S())
                installer.assert_not_called()  # 没确认指纹前绝不安装
                self.assertEqual(len(downloads), 1)  # 下载一次
                self.assertEqual(result["stage"], "fingerprint_required")
                self.assertEqual(result["expected_sha256"], sha)  # 真指纹，不是 None
                self.assertEqual(result["download_url"], "https://example.invalid/napcat.zip")
                self.assertIn(sha, result["error"])
                self.assertIn("确认安装", result["error"])
                self.assertIn("不用重新下载", result["error"])
                self.assertTrue(Path(na._PENDING_PACKAGE["path"]).is_file())  # 包留着
            finally:
                na._drop_pending_package()

    def test_auto_setup_reuses_the_downloaded_package_after_confirmation(self):
        """带正确 accept_sha256 的第二次调用：复用那份包安装，不再下载。"""
        payload = b"PK\x03\x04" + b"e2e-package" * 64
        sha = hashlib.sha256(payload).hexdigest()
        downloads = []

        def fake_download(settings, source, on_progress):
            downloads.append(source)
            folder = Path(tempfile.mkdtemp(prefix="pending-"))
            archive = folder / "NapCat.zip"
            archive.write_bytes(payload)
            return {"ok": True, "path": str(archive), "folder": str(folder), "bytes": len(payload), "sha256": sha}

        with tempfile.TemporaryDirectory() as td:
            fake = self._root(td)
            calls = {"n": 0}

            def find_roots():
                # 两次 auto_setup 的入口都还没装；安装完成后再查（含 detect_boot）才是装好的根
                calls["n"] += 1
                return [] if calls["n"] <= 2 else [fake]

            with patch.object(na, "find_roots", side_effect=find_roots), \
                 patch.object(na, "find_qq_exe", return_value=Path("C:/QQ/QQ.exe")), \
                 patch.object(na, "download_url", return_value="https://example.invalid/napcat.zip"), \
                 patch.object(na, "_expected_package_sha256", return_value=""), \
                 patch.object(na, "_download_package", side_effect=fake_download), \
                 patch.object(na, "install", return_value={"ok": True, "root": str(fake.root)}) as installer, \
                 patch.object(na, "_request", side_effect=OSError("offline")), \
                 patch.object(na, "_probe_api", return_value=(False, "connect refused")), \
                 patch.object(na, "restart", return_value={"ok": True, "pid": 4, "waiting_for_login": True}), \
                 patch.object(na, "_wait_for_new_qr", return_value=True):
                first = na.auto_setup(S())
                self.assertEqual(first["stage"], "fingerprint_required")
                second = na.auto_setup(S(), accept_sha256=sha)
        installer.assert_called_once()
        self.assertEqual(len(downloads), 1)  # 第二次没重新下载
        passed = installer.call_args.kwargs
        self.assertEqual(passed["accept_sha256"], sha)
        self.assertEqual(Path(passed["archive_path"]).name, "NapCat.zip")
        self.assertTrue(second["ok"])
        self.assertEqual(second["stage"], "waiting_for_login")
        self.assertEqual(na._PENDING_PACKAGE, {})  # 装完就清掉

    def test_auto_setup_rejects_mismatched_fingerprint_and_drops_package(self):
        payload = b"PK\x03\x04" + b"e2e-package" * 64
        sha = hashlib.sha256(payload).hexdigest()

        def fake_download(settings, source, on_progress):
            folder = Path(tempfile.mkdtemp(prefix="pending-"))
            archive = folder / "NapCat.zip"
            archive.write_bytes(payload)
            return {"ok": True, "path": str(archive), "folder": str(folder), "bytes": len(payload), "sha256": sha}

        with patch.object(na, "find_roots", return_value=[]), \
             patch.object(na, "find_qq_exe", return_value=Path("C:/QQ/QQ.exe")), \
             patch.object(na, "download_url", return_value="https://example.invalid/napcat.zip"), \
             patch.object(na, "_expected_package_sha256", return_value=""), \
             patch.object(na, "_download_package", side_effect=fake_download), \
             patch.object(na, "install") as installer:
            na.auto_setup(S())
            result = na.auto_setup(S(), accept_sha256="f" * 64)
        installer.assert_not_called()  # 指纹不一致绝不安装
        self.assertEqual(result["stage"], "fingerprint_required")
        self.assertIn("不一致", result["error"])
        self.assertIn("已丢弃", result["error"])
        self.assertEqual(na._PENDING_PACKAGE, {})  # 包已丢弃

    def test_auto_setup_reports_computed_fingerprint_after_download(self):
        """兜底：install() 自己判出指纹未登记 → 也要给 fingerprint_required + 真指纹。"""
        with patch.object(na, "find_roots", return_value=[]), \
             patch.object(na, "find_qq_exe", return_value=Path("C:/QQ/QQ.exe")), \
             patch.object(na, "download_url", return_value="https://example.invalid/napcat.zip"), \
             patch.object(na, "_expected_package_sha256", return_value="b" * 64), \
             patch.object(na, "install", return_value={"ok": False, "error": "这一版 NapCat 还没有登记过指纹", "sha256": "a" * 64}):
            result = na.auto_setup(S())
        self.assertEqual(result["stage"], "fingerprint_required")
        self.assertEqual(result["expected_sha256"], "a" * 64)
        self.assertIn("aaaaaaa", result["error"])
        self.assertIn("确认安装", result["error"])

    def test_launch_missing_root_points_to_page_install_and_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            qq = self._make_qq_file(td)
            with patch.object(na, "detect_boot", return_value=None), patch.object(na, "find_qq_exe", return_value=qq), patch.object(na, "_ports", return_value=set()):
                result = na.launch(S())
        self.assertFalse(result["ok"])
        self.assertIn("自动下载并安装 NapCat", result["error"])
        self.assertIn("<数据目录>\\napcat\\", result["error"])
        self.assertNotIn(na.NAPCAT_DOWNLOAD_URL, result["error"])

    def test_detect_boot_data_dir_is_real_dir_for_handmade_install(self):
        with tempfile.TemporaryDirectory() as td:
            self._make_fake_tree(td)
            na._LAST_INSTALL_ROOT = None
            qq = self._make_qq_file(td)
            with patch.object(na, "find_roots", return_value=[]), patch.object(na, "_registry_qq_exe", return_value=qq), patch.dict(os.environ, {"QQ_DIGEST_NAPCAT_DIR": td, "QQ_DIGEST_DATA_DIR": ""}, clear=False):
                boot = na.detect_boot(data_dir=Path(td) / "unused-data")
            self.assertIsNotNone(boot)
            self.assertTrue(Path(boot["data_dir"]).is_dir())
            self.assertEqual(Path(boot["data_dir"]), Path(boot["boot_exe"]).parent)

    def test_launch_uses_an_existing_cwd_when_data_dir_is_missing(self):
        missing = str(Path(tempfile.gettempdir()) / "napcat-not-there")
        boot = {"boot_exe": str(Path(tempfile.gettempdir()) / "NapCatWinBootMain.exe"), "qq_exe": "F:/QQ.exe", "hook_dll": "F:/hook.dll", "data_dir": missing}
        child = type("Child", (), {"pid": 9})()
        with patch.object(na, "_ports", return_value=set()), patch.object(na, "detect_boot", return_value=boot), patch.object(na.subprocess, "Popen", return_value=child) as popen:
            result = na.launch(S(), uin=None, profile_dir=str(Path(tempfile.gettempdir()) / "napcat-test-profile-2"))
        self.assertTrue(result["ok"])
        kwargs = popen.call_args.kwargs
        self.assertEqual(kwargs["cwd"], Path(boot["boot_exe"]).parent)
        self.assertTrue(kwargs["cwd"].is_dir())
        self.assertEqual(kwargs["env"]["DATA_DIR"], str(Path(boot["boot_exe"]).parent))
        self.assertEqual(kwargs["env"]["NAPCAT_WORKDIR"], str(kwargs["cwd"]))

    def _make_loader_install(self, td):
        """造一份带 qqnt.json/加载器的引擎，加一个像样的 QQ 版本目录。"""
        shell = Path(td) / "napcat"
        (shell / "config").mkdir(parents=True)
        (shell / "config" / "webui.json").write_text('{"port":6199}', encoding="utf-8")
        (shell / "qqnt.json").write_text('{"main":"./loadNapCat.js"}', encoding="utf-8")
        (shell / "loadNapCat.js").write_text("// loader", encoding="utf-8")
        app = Path(td) / "qq" / "versions" / "9.9.31-49738" / "resources" / "app"
        app.mkdir(parents=True)
        (app / "package.json").write_text('{"main":"./application.asar/app_launcher/index.js"}', encoding="utf-8")
        boot = {"boot_exe": str(shell / "NapCatWinBootMain.exe"), "qq_exe": str(Path(td) / "qq" / "QQ.exe"), "hook_dll": str(shell / "NapCatWinBootHook.dll"), "data_dir": str(shell)}
        return shell, app, boot

    def test_launch_copies_the_loader_into_qq(self):
        # QQ 的 package.json main 被 hook 换成 ./loadNapCat.js，文件必须在 QQ 自己的目录里。
        with tempfile.TemporaryDirectory() as td:
            shell, app, boot = self._make_loader_install(td)
            child = type("Child", (), {"pid": 11})()
            with patch.object(na, "_ports", return_value=set()), patch.object(na, "detect_boot", return_value=boot), patch.object(na.subprocess, "Popen", return_value=child):
                result = na.launch(S(), profile_dir=str(Path(td) / "profile"))
            self.assertTrue(result["ok"])
            self.assertEqual((app / "loadNapCat.js").read_text(encoding="utf-8"), "// loader")
            self.assertFalse((shell / "loadNapCat.js").is_symlink())

    def test_launch_refuses_to_start_when_qq_dir_is_not_writable(self):
        with tempfile.TemporaryDirectory() as td:
            _shell, app, boot = self._make_loader_install(td)
            with patch.object(na, "_ports", return_value=set()), patch.object(na, "detect_boot", return_value=boot), patch.object(na.shutil, "copy2", side_effect=OSError("拒绝访问")), patch.object(na.subprocess, "Popen") as popen:
                result = na.launch(S(), profile_dir=str(Path(td) / "profile"))
            self.assertFalse(result["ok"])
            self.assertIn("loadNapCat.js", result["error"])
            self.assertIn(str(app), result["error"])
            popen.assert_not_called()

    def test_launch_error_tells_the_user_which_env_key_fixes_it(self):
        boot = {"boot_exe": str(Path(tempfile.gettempdir()) / "NapCatWinBootMain.exe"), "qq_exe": "F:/QQ.exe", "hook_dll": "F:/hook.dll", "data_dir": tempfile.gettempdir()}
        with patch.object(na, "_ports", return_value=set()), patch.object(na, "detect_boot", return_value=boot), patch.object(na.subprocess, "Popen", side_effect=OSError("[WinError 267] 目录名称无效。")):
            result = na.launch(S(), uin=None, profile_dir=str(Path(tempfile.gettempdir()) / "napcat-test-profile-3"))
        self.assertFalse(result["ok"])
        self.assertIn("QQ_DIGEST_NAPCAT_DIR", result["error"])
        self.assertIn("WinError 267", result["error"])

    def test_file_fallback_preserves_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); (root / "config").mkdir(); (root / "cache").mkdir()
            (root / "config" / "webui.json").write_text('{"port":6199,"token":"t"}', encoding="utf-8")
            cfg = {"network":{"httpServers":[],"httpClients":[{"url":"http://127.0.0.1:8765","old":1}]},"keep":True}
            (root / "config" / "onebot11_123.json").write_text(json.dumps(cfg), encoding="utf-8")
            (root / "cache" / "qrcode.png").write_bytes(b"png")
            fake = na.NapcatRoot(root, 6199, "t", ["123"], False)
            # 「本机装了 QQ」是这条断言的前提，必须显式 patch 进来：CI runner 上没装 QQ 时
            # auto_setup 会走 stage="qq_missing" 那条分支（不设 restart_required），
            # 断言就随「机器上有没有 QQ」飘——那是隐式环境依赖，不是被测行为。
            with patch.object(na, "find_roots", return_value=[fake]), patch.object(na, "_request", side_effect=OSError("offline")), patch.object(na, "restart", return_value={"ok": False, "error": "test"}), patch.object(na, "find_qq_exe", return_value=root / "QQ.exe"):
                result = na.auto_setup(S())
            self.assertEqual(result["applied_via"], "file")
            self.assertTrue(result["restart_required"])
            out = json.loads((root / "config" / "onebot11_123.json").read_text())
            self.assertTrue(out["keep"])
            self.assertEqual(len(out["network"]["httpClients"]), 1)
            self.assertEqual(out["network"]["httpClients"][0]["url"], "http://127.0.0.1:8765")
            self.assertEqual(out["network"]["httpServers"][0]["port"], 3001)
            self.assertFalse(Path(__file__).resolve().parents[1].joinpath("qr.png").exists())

    def test_file_fallback_without_qq_reports_qq_missing(self):
        # 没装 QQ 时不能报成笼统的「启动失败」：阶段必须是 qq_missing，错误里要带能照做的三步引导。
        # 这条正是 CI runner（没装 QQ）上跑到的真实分支，不是人造场景。
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); (root / "config").mkdir(); (root / "cache").mkdir()
            (root / "config" / "webui.json").write_text('{"port":6199,"token":"t"}', encoding="utf-8")
            cfg = {"network": {"httpServers": [], "httpClients": []}}
            (root / "config" / "onebot11_123.json").write_text(json.dumps(cfg), encoding="utf-8")
            (root / "cache" / "qrcode.png").write_bytes(b"png")
            fake = na.NapcatRoot(root, 6199, "t", ["123"], False)
            with patch.object(na, "find_roots", return_value=[fake]), patch.object(na, "_request", side_effect=OSError("offline")), patch.object(na, "restart", return_value={"ok": False, "error": "test"}), patch.object(na, "find_qq_exe", return_value=None):
                result = na.auto_setup(S())
            self.assertEqual(result["applied_via"], "file")
            self.assertFalse(result["ok"])
            self.assertEqual(result["stage"], "qq_missing")
            # 只断言关键片段，别整串照抄：_QQ_REQUIRED_SHORT + QQ_NOT_FOUND_GUIDE 会继续改文案。
            self.assertIn("没找到官方 QQ", result["error"])
            self.assertIn("下载官方 QQ", result["error"])
            self.assertIn("重新检测", result["error"])
            # 缺 QQ 时说成「NapCat 目录配错」会把用户引去改 .env，这里明确不要那句。
            self.assertNotIn("QQ_DIGEST_NAPCAT_DIR", result["error"])

    def _fake_zip(self, malicious=False):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as zf:
            zf.writestr("../evil.txt" if malicious else "NapCatWinBootMain.exe", b"x")
            if not malicious:
                zf.writestr("NapCatWinBootHook.dll", b"x")
                zf.writestr("qqnt.json", b'{"main":"./loadNapCat.js"}')
                zf.writestr("loadNapCat.js", b"// loader")
                zf.writestr("napcat.mjs", b"// shell")
                zf.writestr("config/napcat.json", b"{}")
        return stream.getvalue()

    def _make_qq_file(self, root):
        path = Path(root) / "machine-qq" / "QQ.exe"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"qqnt")
        return path

    @unittest.skipUnless(os.name == "nt", "Windows netstat-based NapCat detection")
    def test_install_downloads_to_data_dir_and_detects(self):
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): pass
        with tempfile.TemporaryDirectory() as td:
            settings = type("Settings", (), {"data_dir": Path(td)})()
            payload = self._fake_zip()
            na._LAST_INSTALL_ROOT = None
            qq = self._make_qq_file(td)
            with patch.dict(na.NAPCAT_KNOWN_SHA256, {na.NAPCAT_VERSION: hashlib.sha256(payload).hexdigest()}, clear=True), patch.dict(os.environ, {"QQ_DIGEST_DATA_DIR": td}, clear=False), patch.object(na, "find_qq_exe", return_value=qq), patch.object(na.urllib.request, "urlopen", return_value=Response(payload)):
                result = na.install(settings)
                # detect_boot() 必须在这个 patch 作用域里调：它靠 find_qq_exe 找 QQ，
                # 出了这个块 patch 就撤销，没装 QQ 的机器（CI runner）上会返回 None，
                # 下一行 boot["boot_exe"] 直接 TypeError —— CI 上就是这么红的。
                boot = na.detect_boot()
            self.assertTrue(result["ok"])
            self.assertTrue(Path(result["root"]).resolve().is_relative_to(Path(td).resolve()))
            self.assertTrue(Path(boot["boot_exe"]).resolve().is_relative_to(Path(td).resolve()))

    def test_registry_command_path_extracts_qq_exe(self):
        self.assertEqual(na._path_from_registry_value(r'"D:\QQ\QQ.exe",0'), Path(r"D:\QQ\QQ.exe"))

    def test_find_roots_recognizes_official_shell_config(self):
        with tempfile.TemporaryDirectory() as td:
            shell = Path(td) / "napcat"
            (shell / "config").mkdir(parents=True)
            (shell / "config" / "napcat.json").write_text("{}", encoding="utf-8")
            (shell / "NapCatWinBootMain.exe").write_bytes(b"boot")
            (shell / "NapCatWinBootHook.dll").write_bytes(b"hook")
            with patch.dict(os.environ, {"QQ_DIGEST_DATA_DIR": td, "QQ_DIGEST_NAPCAT_DIR": ""}, clear=False), patch.object(na, "_ports", return_value=set()):
                roots = na.find_roots()
            found = [root.root.resolve() for root in roots]
            self.assertIn(shell.resolve(), found)
            self.assertEqual(len(found), len(set(found)))

    def test_find_roots_searches_nested_shell_only_below_registered_qq(self):
        with tempfile.TemporaryDirectory() as td:
            qq_root = Path(td) / "qq-install"
            qq_root.mkdir()
            (qq_root / "QQ.exe").write_bytes(b"qq")
            shell = qq_root / "resources" / "shells" / "NapCat.Embedded" / "versions" / "1" / "resources" / "app" / "napcat"
            config = shell / "config"
            config.mkdir(parents=True)
            (shell / "NapCatWinBootMain.exe").write_bytes(b"boot")
            (shell / "NapCatWinBootHook.dll").write_bytes(b"hook")
            (config / "webui.json").write_text('{"port":6099}', encoding="utf-8")
            env = {key: "" for key in ("APPDATA", "LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)", "QQ_DIGEST_DATA_DIR", "QQ_DIGEST_NAPCAT_DIR")}
            with patch.dict(os.environ, env, clear=False), patch.object(na, "_registry_qq_exe", return_value=qq_root / "QQ.exe"), patch.object(na, "_ports", return_value=set()):
                roots = na.find_roots()
            self.assertIn(shell.resolve(), [root.root.resolve() for root in roots])

    def test_detect_boot_uses_registered_qq_and_managed_data_root_before_legacy_root(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / "isolated-data"
            shell = data / "napcat"
            config = shell / "config"
            config.mkdir(parents=True)
            (shell / "NapCatWinBootMain.exe").write_bytes(b"boot")
            (shell / "NapCatWinBootHook.dll").write_bytes(b"hook")
            (shell / "qqnt.json").write_text("{}", encoding="utf-8")
            (config / "napcat.json").write_text("{}", encoding="utf-8")
            qq = self._make_qq_file(td)
            roots = [na.NapcatRoot(Path(td) / "legacy-root", 6099, "", [], False), na.NapcatRoot(shell, 6099, "", [], False)]
            na._LAST_INSTALL_ROOT = None
            with patch.dict(os.environ, {"QQ_DIGEST_DATA_DIR": str(data), "QQ_DIGEST_NAPCAT_DIR": ""}, clear=False), patch.object(na, "_registry_qq_exe", return_value=qq), patch.object(na, "find_roots", return_value=roots):
                found = na.detect_boot()
            self.assertIsNotNone(found)
            self.assertEqual(Path(found["qq_exe"]), qq.resolve())
            self.assertEqual(Path(found["data_dir"]), shell.resolve())

    def test_explicit_handmade_root_beats_legacy_f_napcat(self):
        with tempfile.TemporaryDirectory() as td:
            shell = Path(td) / "manual-shell"
            (shell / "config").mkdir(parents=True)
            (shell / "NapCatWinBootMain.exe").write_bytes(b"boot")
            (shell / "NapCatWinBootHook.dll").write_bytes(b"hook")
            (shell / "qqnt.json").write_text("{}", encoding="utf-8")
            (shell / "config" / "napcat.json").write_text("{}", encoding="utf-8")
            qq = self._make_qq_file(td)
            roots = [na.NapcatRoot(Path(td) / "legacy-root", 6099, "", [], False), na.NapcatRoot(shell, 6099, "", [], False)]
            na._LAST_INSTALL_ROOT = None
            env = {"QQ_DIGEST_DATA_DIR": "", "QQ_DIGEST_NAPCAT_DIR": str(shell)}
            with patch.dict(os.environ, env, clear=False), patch.object(na, "_registry_qq_exe", return_value=qq), patch.object(na, "find_roots", return_value=roots):
                found = na.detect_boot()
            self.assertIsNotNone(found)
            self.assertEqual(Path(found["data_dir"]), shell.resolve())

    def test_install_fails_before_download_without_qq_nt(self):
        with tempfile.TemporaryDirectory() as td:
            settings = type("Settings", (), {"data_dir": Path(td)})()
            # 直接钉住检测结果：现在 find_qq_exe 还会读运行中的 QQ 进程和盘根路径，
            # 只 patch 注册表的话，开发机上开着 QQ 就会变成「找到 QQ 但没找到 NapCat」，
            # 用例就跟着机器环境飘了。
            with patch.object(na, "find_qq_exe", return_value=None), patch.object(na.urllib.request, "urlopen") as fetch:
                result = na.install(settings)
            self.assertFalse(result["ok"])
            self.assertIn("未找到电脑版 QQ NT", result["error"])
            fetch.assert_not_called()

    def test_install_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            settings = type("Settings", (), {"data_dir": Path(td)})()
            na._LAST_INSTALL_ROOT = Path(td) / "napcat"
            na._LAST_INSTALL_ROOT.mkdir(parents=True)
            with patch.object(na, "detect_boot", return_value={"boot_exe": str(na._LAST_INSTALL_ROOT / "x")} ), patch.object(na.urllib.request, "urlopen") as fetch:
                result = na.install(settings)
            self.assertTrue(result["already_installed"])
            fetch.assert_not_called()

    def test_install_download_error_is_returned(self):
        with tempfile.TemporaryDirectory() as td:
            settings = type("Settings", (), {"data_dir": Path(td)})()
            qq = self._make_qq_file(td)
            with patch.object(na, "_registry_qq_exe", return_value=qq), patch.object(na.urllib.request, "urlopen", side_effect=OSError("offline")):
                result = na.install(settings)
            self.assertFalse(result["ok"])
            self.assertIn("失败", result["error"])

    def test_install_rejects_html_download(self):
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): pass
        with tempfile.TemporaryDirectory() as td:
            settings = type("Settings", (), {"data_dir": Path(td)})()
            qq = self._make_qq_file(td)
            with patch.object(na, "_registry_qq_exe", return_value=qq), patch.object(na.urllib.request, "urlopen", return_value=Response(b"<html>not zip</html>")):
                result = na.install(settings)
            self.assertFalse(result["ok"])
            self.assertIn("不是有效的压缩包", result["error"])

    def test_install_rejects_zip_path_traversal(self):
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): pass
        with tempfile.TemporaryDirectory() as td:
            settings = type("Settings", (), {"data_dir": Path(td)})()
            qq = self._make_qq_file(td)
            payload = self._fake_zip(True)
            # 哈希登记上，确保走到路径校验（否则会先被「未登记哈希」挡住）
            with patch.dict(na.NAPCAT_KNOWN_SHA256, {na.NAPCAT_VERSION: hashlib.sha256(payload).hexdigest()}, clear=True), patch.object(na, "find_qq_exe", return_value=qq), patch.object(na.urllib.request, "urlopen", return_value=Response(payload)):
                result = na.install(settings)
            self.assertFalse(result["ok"])
            self.assertFalse((Path(td).parent / "evil.txt").exists())

    def test_install_rejects_package_hash_mismatch(self):
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): pass
        with tempfile.TemporaryDirectory() as td:
            settings = type("Settings", (), {"data_dir": Path(td)})()
            qq = self._make_qq_file(td)
            payload = self._fake_zip()
            with patch.dict(na.NAPCAT_KNOWN_SHA256, {na.NAPCAT_VERSION: "0" * 64}, clear=True), patch.object(na, "find_qq_exe", return_value=qq), patch.object(na.urllib.request, "urlopen", return_value=Response(payload)):
                result = na.install(settings)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "安装包校验失败（哈希不符），已丢弃")
            self.assertFalse((Path(td) / "napcat").exists())

    def _progress_payload(self, extra_bytes: int = 0) -> bytes:
        """带一个（可撑大多 chunk 的）大文件的合法包，用来观察分段下载进度。"""
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as zf:
            for name, data in (
                ("NapCatWinBootMain.exe", b"x"),
                ("NapCatWinBootHook.dll", b"x"),
                ("qqnt.json", b'{"main":"./loadNapCat.js"}'),
                ("loadNapCat.js", b"// loader"),
                ("napcat.mjs", b"// shell"),
            ):
                zf.writestr(name, data)
            if extra_bytes:
                zf.writestr("padding.bin", b"\0" * extra_bytes, compress_type=zipfile.ZIP_STORED)
        return stream.getvalue()

    def test_install_reports_monotonic_download_progress_with_total(self):
        """UX P1：下载要报「已下载多少字节 / 总字节」，让前端能显示百分比。"""
        class Response(io.BytesIO):
            headers = {}

            def __enter__(self): return self
            def __exit__(self, *args): pass

        payload = self._progress_payload(extra_bytes=2 * 1024 * 1024 + 512 * 1024)
        events: list[dict] = []
        with tempfile.TemporaryDirectory() as td:
            settings = type("Settings", (), {"data_dir": Path(td)})()
            qq = self._make_qq_file(td)
            response = Response(payload)
            response.headers = {"Content-Length": str(len(payload))}
            with patch.dict(na.NAPCAT_KNOWN_SHA256, {na.NAPCAT_VERSION: hashlib.sha256(payload).hexdigest()}, clear=True), patch.object(na, "find_qq_exe", return_value=qq), patch.object(na.urllib.request, "urlopen", return_value=response):
                result = na.install(settings, on_progress=events.append)
        self.assertTrue(result["ok"], result)
        downloads = [event for event in events if event["stage"] == "download"]
        self.assertGreaterEqual(len(downloads), 3, "2.5 MB 的包按 1 MB 分块，至少三段进度")
        sizes = [event["downloaded_bytes"] for event in downloads]
        self.assertEqual(sizes, sorted(sizes), "已下载字节必须单调递增")
        self.assertEqual(sizes[-1], len(payload))
        self.assertEqual(downloads[-1]["total_bytes"], len(payload), "Content-Length 拿得到就报总大小")
        self.assertEqual(events[0]["stage"], "download")
        self.assertIsNone(events[0]["total_bytes"], "还没读到响应头时不能编总大小")
        # 阶段顺序：下载 → 校验 → 解压 → 安装 → 完成
        stages = [event["stage"] for event in events]
        for name in ("verify", "extract", "install", "complete"):
            self.assertIn(name, stages)
        self.assertLess(stages.index("verify"), stages.index("extract"))
        self.assertLess(stages.index("extract"), stages.index("install"))
        self.assertLess(stages.index("install"), stages.index("complete"))

    def test_install_reports_no_total_when_content_length_missing(self):
        """服务端没给 Content-Length 时只报已下载字节，total_bytes 必须是 None（不估算）。"""
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): pass

        payload = self._progress_payload()
        events: list[dict] = []
        with tempfile.TemporaryDirectory() as td:
            settings = type("Settings", (), {"data_dir": Path(td)})()
            qq = self._make_qq_file(td)
            with patch.dict(na.NAPCAT_KNOWN_SHA256, {na.NAPCAT_VERSION: hashlib.sha256(payload).hexdigest()}, clear=True), patch.object(na, "find_qq_exe", return_value=qq), patch.object(na.urllib.request, "urlopen", return_value=Response(payload)):
                result = na.install(settings, on_progress=events.append)
        self.assertTrue(result["ok"], result)
        downloads = [event for event in events if event["stage"] == "download"]
        self.assertTrue(downloads)
        self.assertTrue(all(event["total_bytes"] is None for event in downloads))
        self.assertEqual(downloads[-1]["downloaded_bytes"], len(payload))

    def test_install_reports_extract_entry_counts(self):
        """解压报告「已完成条目数 / 总条目数」，别只给一句「解压中」。"""
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): pass

        payload = self._progress_payload()
        events: list[dict] = []
        with tempfile.TemporaryDirectory() as td:
            settings = type("Settings", (), {"data_dir": Path(td)})()
            qq = self._make_qq_file(td)
            with patch.dict(na.NAPCAT_KNOWN_SHA256, {na.NAPCAT_VERSION: hashlib.sha256(payload).hexdigest()}, clear=True), patch.object(na, "find_qq_exe", return_value=qq), patch.object(na.urllib.request, "urlopen", return_value=Response(payload)):
                with zipfile.ZipFile(io.BytesIO(payload)) as probe:
                    expected_entries = len(probe.infolist())
                result = na.install(settings, on_progress=events.append)
        self.assertTrue(result["ok"], result)
        extracts = [event for event in events if event["stage"] == "extract"]
        self.assertTrue(extracts)
        self.assertTrue(all(event["entries"] == expected_entries for event in extracts))
        counts = [event["extracted"] for event in extracts]
        self.assertEqual(counts, sorted(counts), "已解压条目数必须单调递增")
        self.assertEqual(counts[0], 0)
        self.assertEqual(counts[-1], expected_entries)

    def test_install_progress_callback_errors_do_not_break_install(self):
        """进度只是观感：回调自己抛异常也不能让安装失败。"""
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): pass

        def boom(_event):
            raise RuntimeError("ui went away")

        payload = self._progress_payload()
        with tempfile.TemporaryDirectory() as td:
            settings = type("Settings", (), {"data_dir": Path(td)})()
            qq = self._make_qq_file(td)
            with patch.dict(na.NAPCAT_KNOWN_SHA256, {na.NAPCAT_VERSION: hashlib.sha256(payload).hexdigest()}, clear=True), patch.object(na, "find_qq_exe", return_value=qq), patch.object(na.urllib.request, "urlopen", return_value=Response(payload)):
                result = na.install(settings, on_progress=boom)
        self.assertTrue(result["ok"], result)

    def test_install_refuses_when_version_has_no_registered_hash(self):
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): pass
        with tempfile.TemporaryDirectory() as td:
            settings = type("Settings", (), {"data_dir": Path(td)})()
            qq = self._make_qq_file(td)
            payload = self._fake_zip()
            # 空表必须表现为「拒绝」，不是「跳过校验」：第一次只把实算指纹交回来，不装。
            with patch.dict(na.NAPCAT_KNOWN_SHA256, {}, clear=True), patch.object(na, "find_qq_exe", return_value=qq), patch.object(na.urllib.request, "urlopen", side_effect=lambda *a, **k: Response(payload)):
                result = na.install(settings)
            self.assertFalse(result["ok"])
            self.assertIn("还没有登记过指纹", result["error"])
            self.assertIn("sha256", result)
            self.assertFalse((Path(td) / "napcat").exists())

    def test_install_aborts_when_extracted_bytes_exceed_limit(self):
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): pass
        with tempfile.TemporaryDirectory() as td:
            settings = type("Settings", (), {"data_dir": Path(td)})()
            qq = self._make_qq_file(td)
            payload = self._fake_zip()
            with patch.dict(na.NAPCAT_KNOWN_SHA256, {na.NAPCAT_VERSION: hashlib.sha256(payload).hexdigest()}, clear=True), patch.object(na, "MAX_EXTRACTED_BYTES", 3), patch.object(na, "find_qq_exe", return_value=qq), patch.object(na.urllib.request, "urlopen", return_value=Response(payload)):
                result = na.install(settings)
            self.assertFalse(result["ok"])
            self.assertIn("上限", result["error"])
            self.assertFalse((Path(td) / "napcat").exists())

    def test_install_aborts_when_entry_count_exceeds_limit(self):
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): pass
        with tempfile.TemporaryDirectory() as td:
            settings = type("Settings", (), {"data_dir": Path(td)})()
            qq = self._make_qq_file(td)
            payload = self._fake_zip()
            with patch.dict(na.NAPCAT_KNOWN_SHA256, {na.NAPCAT_VERSION: hashlib.sha256(payload).hexdigest()}, clear=True), patch.object(na, "MAX_EXTRACTED_ENTRIES", 2), patch.object(na, "find_qq_exe", return_value=qq), patch.object(na.urllib.request, "urlopen", return_value=Response(payload)):
                result = na.install(settings)
            self.assertFalse(result["ok"])
            self.assertIn("条目过多", result["error"])

    def test_qr_file_prefers_data_dir_and_stamp_follows_the_file(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / "data"
            (data / "cache").mkdir(parents=True)
            qr = data / "cache" / "qrcode.png"
            qr.write_bytes(b"one")
            settings = type("T", (), {"data_dir": data, "napcat_qr_path": Path(td) / "elsewhere.png"})()
            with patch.object(na, "find_roots", return_value=[]):
                self.assertEqual(na._qr_file(settings), qr)
                first = na.qr_stamp(settings)
                self.assertEqual(first, f"{qr.stat().st_mtime_ns}-3")
                os.utime(qr, ns=(111_111_111_111, 111_111_111_111))
                self.assertNotEqual(na.qr_stamp(settings), first)

    def test_qr_stamp_is_blank_without_a_file(self):
        with tempfile.TemporaryDirectory() as td:
            settings = type("T", (), {"data_dir": Path(td), "napcat_qr_path": Path(td) / "missing.png"})()
            with patch.object(na, "find_roots", return_value=[]), patch.object(na, "detect_boot", return_value=None):
                self.assertEqual(na.qr_stamp(settings), "")
                self.assertIsNone(na.qrcode_bytes(settings))

    def test_qr_file_finds_code_beside_handmade_engine(self):
        # 手工装的 NapCat：码在 QQ_DIGEST_NAPCAT_DIR 指向目录的 cache 下，不在默认搜索根里。
        with tempfile.TemporaryDirectory() as td:
            engine = Path(td) / "NapCat.52230.Shell"
            (engine / "cache").mkdir(parents=True)
            (engine / "NapCatWinBootMain.exe").write_bytes(b"boot")
            qr = engine / "cache" / "qrcode.png"
            qr.write_bytes(b"handmade")
            settings = type("T", (), {"data_dir": Path(td) / "hub", "napcat_qr_path": Path(td) / "missing.png"})()
            boot = {"boot_exe": str(engine / "NapCatWinBootMain.exe"), "qq_exe": "", "hook_dll": "", "data_dir": str(engine)}
            with patch.object(na, "detect_boot", return_value=boot):
                self.assertEqual(na._qr_file(settings), qr)
                self.assertEqual(na.qrcode_bytes(settings), b"handmade")

    def test_qr_file_prefers_the_newest_of_the_candidate_files(self):
        # 旧资料目录里可能留着过期二维码；取最新的一张，否则页面会一直显示扫不动的旧码。
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / "data"
            (data / "cache").mkdir(parents=True)
            stale = data / "cache" / "qrcode.png"
            stale.write_bytes(b"old")
            os.utime(stale, ns=(111_111_111_111, 111_111_111_111))
            fresh_root = Path(td) / "root"
            (fresh_root / "cache").mkdir(parents=True)
            fresh = fresh_root / "cache" / "qrcode.png"
            fresh.write_bytes(b"new")
            settings = type("T", (), {"data_dir": data, "napcat_qr_path": Path(td) / "missing.png"})()
            boot = {"boot_exe": str(fresh_root / "NapCatWinBootMain.exe"), "qq_exe": "", "hook_dll": "", "data_dir": str(fresh_root)}
            with patch.object(na, "detect_boot", return_value=boot):
                self.assertEqual(na._qr_file(settings), fresh)

    def test_wait_for_port_returns_once_the_port_listens(self):
        seen = {"n": 0}

        def ports():
            seen["n"] += 1
            return {3001} if seen["n"] >= 2 else set()

        with patch.object(na, "_ports", side_effect=ports), patch.object(na.time, "sleep"):
            self.assertTrue(na.wait_for_port(3001, timeout=5))

    def test_wait_for_port_times_out_without_the_port(self):
        with patch.object(na, "_ports", return_value=set()), patch.object(na.time, "sleep"), patch.object(na.time, "monotonic", side_effect=[0, 0, 99]):
            self.assertFalse(na.wait_for_port(3001, timeout=1))

    def test_probe_api_counts_any_http_answer_as_connected(self):
        with patch.object(na, "_request", return_value={"status": "failed", "retcode": 1}):
            ok, detail = na._probe_api("http://127.0.0.1:3001/get_login_info", "t")
        self.assertTrue(ok)
        self.assertIn("failed", detail)
        with patch.object(na, "_request", side_effect=OSError("refused")):
            self.assertFalse(na._probe_api("http://127.0.0.1:3001/get_login_info", "t")[0])

    def test_probe_api_explains_refused_port_instead_of_raw_winerror(self):
        """issue #4：页面上不该出现「WinError 10061 积极拒绝」原文，得说清下一步。"""
        with patch.object(na, "_request", side_effect=OSError("[WinError 10061] 由于目标计算机积极拒绝，无法连接。")):
            ok, detail = na._probe_api("http://127.0.0.1:3001/get_login_info", "t")
        self.assertFalse(ok)
        self.assertIn("还没监听", detail)
        self.assertIn("重启引擎", detail)
        self.assertNotIn("10061", detail)

    def test_probe_api_tells_token_mismatch_apart_from_not_started(self):
        with patch.object(na, "_request", side_effect=urllib.error.HTTPError("http://x", 401, "unauthorized", {}, None)):
            ok, detail = na._probe_api("http://127.0.0.1:3001/get_login_info", "t")
        self.assertFalse(ok)
        self.assertIn("token", detail)
        self.assertNotIn("还没监听", detail)

    def test_all_napcat_pids_matches_by_process_name_read_only(self):
        with patch.object(na.subprocess, "check_output", return_value=b"1234\r\n5678\r\n") as run:
            self.assertEqual(na.all_napcat_pids(), [1234, 5678])
        query = run.call_args[0][0][-1]
        self.assertIn("NapCatWinBootMain.exe", query)
        self.assertNotIn("taskkill", query)

    def test_restart_stops_waits_launches_and_probes(self):
        with patch.object(na, "stop", return_value={"ok": True, "stopped": [5], "already_stopped": False, "error": None}), \
             patch.object(na, "_ports", return_value=set()), \
             patch.object(na, "launch", return_value={"ok": True, "pid": 9}) as launched, \
             patch.object(na, "wait_for_port", return_value=True) as waited:
            result = na.restart(S(), uin="123")
        self.assertTrue(result["ok"])
        self.assertEqual(result["pid"], 9)
        self.assertEqual(result["stopped"], [5])
        self.assertTrue(result["api_up"])
        self.assertFalse(result["waiting_for_login"])
        launched.assert_called_once()
        waited.assert_called_once()

    def test_restart_waits_for_login_when_qr_changes_but_port_stays_closed(self):
        with patch.object(na, "stop", return_value={"ok": True, "stopped": [], "already_stopped": True, "error": None}), \
             patch.object(na, "_ports", return_value=set()), \
             patch.object(na, "launch", return_value={"ok": True, "pid": 9}), \
             patch.object(na, "wait_for_port", return_value=False), \
             patch.object(na, "_qr_token", side_effect=["before", "after", "after"]):
            result = na.restart(S(), uin="123", wait_seconds=5)
        self.assertTrue(result["ok"])
        self.assertFalse(result["api_up"])
        self.assertTrue(result["waiting_for_login"])

    def test_stop_never_falls_back_to_killing_by_process_name(self):
        # 换过安装目录时按启动文件路径找不到进程；此时绝不能按进程名杀——
        # NapCatWinBootMain.exe 的命令行里必然含它自己的文件名，按名字匹配会连带杀掉
        # 同机所有 NapCat 实例（别人的账号也被踢）。回退只允许匹配本安装的 profile 目录。
        with tempfile.TemporaryDirectory() as td:
            settings = type("S", (), {"data_dir": Path(td) / "hub"})()
            engine = Path(td) / "engine"
            engine.mkdir()
            boot = {"boot_exe": str(engine / "NapCatWinBootMain.exe"), "qq_exe": "", "hook_dll": "", "data_dir": str(engine)}
            with patch.object(na, "detect_boot", return_value=boot), patch.object(na, "_kill_matching", return_value=[]) as kill:
                result = na.stop(settings)
            patterns = [call.args[0] for call in kill.call_args_list]
            self.assertEqual(len(patterns), 2)
            self.assertEqual(patterns[0], str(engine))
            self.assertEqual(Path(patterns[1]), (Path(td) / "hub" / "napcat-profile").resolve())
            self.assertFalse(any(p.endswith("NapCatWinBootMain.exe") for p in patterns))
            self.assertTrue(result["ok"])
            self.assertTrue(result["already_stopped"])

    def test_restart_stops_on_stop_error(self):
        with patch.object(na, "stop", return_value={"ok": False, "stopped": [], "already_stopped": False, "error": "boom"}), patch.object(na, "launch") as launched:
            result = na.restart(S())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "boom")
        launched.assert_not_called()

    def test_auto_setup_restarts_napcat_so_the_new_config_is_read(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            (root / "cache").mkdir()
            (root / "config" / "webui.json").write_text('{"port":6199,"token":"t"}', encoding="utf-8")
            (root / "config" / "onebot11_123.json").write_text('{"network":{}}', encoding="utf-8")
            (root / "cache" / "qrcode.png").write_bytes(b"png")
            fake = na.NapcatRoot(root, 6199, "t", ["123"], False)
            answers = [False, True]

            def probe(_api, _token):
                return answers.pop(0), "{}"

            with patch.object(na, "find_roots", return_value=[fake]), patch.object(na, "_request", side_effect=OSError("offline")), \
                 patch.object(na, "_probe_api", side_effect=probe), \
                 patch.object(na, "restart", return_value={"ok": True, "pid": 4}) as restarted, \
                 patch.object(na, "_wait_for_new_qr"):
                result = na.auto_setup(S())
            restarted.assert_called_once()
            self.assertTrue(result["restarted"])
            self.assertTrue(result["ok"])
            self.assertFalse(result["restart_required"])
            self.assertEqual([s["name"] for s in result["steps"]], ["find_roots", "configure_onebot", "restart", "qrcode", "connect"])

    def test_auto_setup_skips_restart_when_the_api_already_answers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            (root / "cache").mkdir()
            (root / "config" / "webui.json").write_text('{"port":6199,"token":"t"}', encoding="utf-8")
            (root / "config" / "onebot11_123.json").write_text('{"network":{}}', encoding="utf-8")
            fake = na.NapcatRoot(root, 6199, "t", ["123"], False)
            with patch.object(na, "find_roots", return_value=[fake]), patch.object(na, "_request", side_effect=OSError("offline")), \
                 patch.object(na, "_probe_api", return_value=(True, "{}")), patch.object(na, "restart") as restarted:
                result = na.auto_setup(S())
            restarted.assert_not_called()
            self.assertFalse(result["restarted"])
            self.assertTrue(result["ok"])
            self.assertEqual([s["name"] for s in result["steps"]], ["find_roots", "configure_onebot", "qrcode", "connect"])

    def test_auto_setup_reports_waiting_for_login_instead_of_failure(self):
        """扫码前 3001 端口本就不会开：restart 后二维码变了就算等待登录，不能报失败。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            (root / "cache").mkdir()
            (root / "config" / "webui.json").write_text('{"port":6199,"token":"t"}', encoding="utf-8")
            (root / "config" / "onebot11_123.json").write_text('{"network":{}}', encoding="utf-8")
            (root / "cache" / "qrcode.png").write_bytes(b"png")
            fake = na.NapcatRoot(root, 6199, "t", ["123"], False)
            with patch.object(na, "find_roots", return_value=[fake]), patch.object(na, "_request", side_effect=OSError("offline")), \
                 patch.object(na, "_probe_api", return_value=(False, "connect refused")), \
                 patch.object(na, "restart", return_value={"ok": True, "pid": 4, "waiting_for_login": True}), \
                 patch.object(na, "_wait_for_new_qr", return_value=True):
                result = na.auto_setup(S())
            self.assertTrue(result["waiting_for_login"])
            self.assertTrue(result["ok"])
            self.assertFalse(result["restart_required"])
            connect = next(s for s in result["steps"] if s["name"] == "connect")
            self.assertTrue(connect["ok"])
            self.assertIn("扫码", connect["detail"])


    def test_registry_reads_tencent_install_value(self):
        """腾讯往 HKLM\...\Tencent\QQNT 写的是 Install=D:\\QQ；漏了它会把非系统盘的 QQ 判成没装。"""
        with tempfile.TemporaryDirectory() as td:
            qq = self._make_qq_file(td)

            class _Key:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            def open_key(hive, path):
                if path.casefold().endswith("tencent\\qqnt"):
                    return _Key()
                raise OSError("no such key")

            def enum_value(key, index):
                if index == 0:
                    return ("Install", str(qq.parent), 1)
                raise OSError("end of values")

            def enum_key(key, index):
                raise OSError("no subkeys")

            fake = types.SimpleNamespace(
                HKEY_CURRENT_USER=1,
                HKEY_LOCAL_MACHINE=2,
                OpenKey=open_key,
                EnumValue=enum_value,
                EnumKey=enum_key,
            )
            with patch.dict(sys.modules, {"winreg": fake}):
                found = na._registry_qq_exe()
        self.assertIsNotNone(found)
        self.assertEqual(found.name.casefold(), "qq.exe")

    def test_find_qq_exe_falls_back_to_running_qq_process(self):
        """注册表没写时，用户正开着的 QQ 进程路径就是最准的来源。"""
        with tempfile.TemporaryDirectory() as td:
            qq = self._make_qq_file(td)
            with (
                patch.object(na, "_registry_qq_exe", return_value=None),
                patch.object(na, "_running_qq_exe", return_value=qq),
                patch.object(na, "_drive_root_qq_candidates", return_value=[]),
                patch.dict(os.environ, {"QQ_DIGEST_QQ_EXE": ""}, clear=False),
            ):
                found = na.find_qq_exe()
        self.assertIsNotNone(found)
        self.assertEqual(found.resolve(), qq.resolve())

    def test_find_qq_exe_honours_env_override(self):
        with tempfile.TemporaryDirectory() as td:
            qq = self._make_qq_file(td)
            with (
                patch.object(na, "_registry_qq_exe", return_value=None),
                patch.object(na, "_running_qq_exe", return_value=None),
                patch.object(na, "_drive_root_qq_candidates", return_value=[]),
                patch.dict(os.environ, {"QQ_DIGEST_QQ_EXE": str(qq)}, clear=False),
            ):
                found = na.find_qq_exe()
        self.assertIsNotNone(found)
        self.assertEqual(found.resolve(), qq.resolve())

    def test_missing_qq_error_tells_user_three_steps(self):
        for word in ("下载官方 QQ", "重新检测", "NapCat"):
            self.assertIn(word, na._QQ_REQUIRED_ERROR)
        # 以前让人去设 NapCat 目录，对「找不到 QQ」是误导。
        self.assertNotIn("QQ_DIGEST_NAPCAT_DIR", na._QQ_REQUIRED_ERROR)


if __name__ == "__main__":
    unittest.main()
