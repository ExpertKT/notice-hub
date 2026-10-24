"""tools/version_bump.py 的用例：全部在临时目录里造假的仓库树，不碰真仓库。"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "version_bump.py"
_SPEC = importlib.util.spec_from_file_location("version_bump", _SCRIPT)
version_bump = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(version_bump)

_README = """# 群务台

[下载测试版 v1.1.0-beta.11](https://example.com/releases/tag/v1.1.0-beta.11)

**🧪 v1.1.0 测试版（beta.11）已发布 — [查看说明](https://example.com/releases/tag/v1.1.0-beta.11)**

beta.11 只修装机问题，beta.10 修托盘。
"""

_GRADLE = """android {
    defaultConfig {
        applicationId 'com.noticehub.app'
        versionCode 10
        versionName '1.1.0-beta.11'
    }
}
"""

_ANNOUNCEMENTS = """{"version": 1, "items": [
  {"id": "a", "date": "2026-10-15", "title": "群务台 1.1.0 测试版（beta.1）已发布"},
  {"id": "b", "date": "2026-10-24", "title": "群务台 1.1.0 测试版（beta.11）已发布"}
]}
"""


def _version_py(version: str) -> str:
    return 'APP_VERSION = "%s"\nAPP_CHANNEL = "beta"\n' % version


def _manifest(version: str) -> str:
    return (
        '{"version": 1, "items": [{"tag": "v%s", "version": "%s", '
        '"asset_name": "QQ-Notice-Hub.zip", "size": 123, "sha256": "%s"}]}'
        % (version, version, "a" * 64)
    )


class _Tree:
    """一次性的假仓库树。"""

    def __init__(self, version: str = "1.1.0-beta.11", readme: str = _README,
                 gradle: str = _GRADLE, announcements: str = _ANNOUNCEMENTS,
                 manifest: str | None = None) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "qq_live_digest").mkdir()
        (self.root / "android" / "app").mkdir(parents=True)
        (self.root / "release").mkdir()
        (self.root / "qq_live_digest" / "version.py").write_text(
            _version_py(version), encoding="utf-8", newline="")
        (self.root / "README.md").write_text(readme, encoding="utf-8", newline="")
        (self.root / "android" / "app" / "build.gradle").write_text(
            gradle, encoding="utf-8", newline="")
        (self.root / "announcements.json").write_text(
            announcements, encoding="utf-8", newline="")
        (self.root / "release" / "update-manifest.json").write_text(
            manifest if manifest is not None else _manifest(version),
            encoding="utf-8", newline="")

    def path(self, rel: str) -> Path:
        return self.root / rel

    def run(self, *args: str) -> tuple[int, str]:
        buffer = io.StringIO()
        argv = list(args) + ["--root", str(self.root)]
        with contextlib.redirect_stdout(buffer):
            code = version_bump.main(argv)
        return code, buffer.getvalue()

    def close(self) -> None:
        self._tmp.cleanup()


class VersionBumpTests(unittest.TestCase):
    def make_tree(self, **kwargs) -> _Tree:
        tree = _Tree(**kwargs)
        self.addCleanup(tree.close)
        return tree

    def test_check_passes_when_every_site_matches(self) -> None:
        tree = self.make_tree()
        code, out = tree.run("--check")
        self.assertEqual(code, 0, out)
        self.assertIn("版本号一致：1.1.0-beta.11", out)
        self.assertIn("与 APP_VERSION 一致", out)

    def test_check_fails_on_readme_mismatch(self) -> None:
        tree = self.make_tree(version="1.1.0-beta.12")
        code, out = tree.run("--check")
        self.assertEqual(code, 1, out)
        self.assertIn("README.md:1.1.0-beta.12:1.1.0-beta.11", out)

    def test_check_fails_on_gradle_mismatch(self) -> None:
        tree = self.make_tree(gradle=_GRADLE.replace("'1.1.0-beta.11'", "'1.1.0-beta.9'"))
        code, out = tree.run("--check")
        self.assertEqual(code, 1, out)
        self.assertIn("android/app/build.gradle:1.1.0-beta.11:1.1.0-beta.9", out)

    def test_check_fails_when_version_file_is_missing(self) -> None:
        tree = self.make_tree()
        tree.path("qq_live_digest/version.py").unlink()
        code, out = tree.run("--check")
        self.assertEqual(code, 1, out)
        self.assertIn("读不到 APP_VERSION", out)

    def test_check_fails_on_manifest_version_mismatch(self) -> None:
        tree = self.make_tree(manifest=_manifest("1.1.0-beta.10"))
        code, out = tree.run("--check")
        self.assertEqual(code, 1, out)
        self.assertIn("release/update-manifest.json:1.1.0-beta.11:1.1.0-beta.10", out)

    def test_check_fails_on_manifest_bad_sha256_and_size(self) -> None:
        bad = ('{"version": 1, "items": [{"tag": "v1.1.0-beta.11", '
               '"version": "1.1.0-beta.11", "size": 0, "sha256": "not-a-hash"}]}')
        tree = self.make_tree(manifest=bad)
        code, out = tree.run("--check")
        self.assertEqual(code, 1, out)
        self.assertIn("sha256 不是 64 位十六进制", out)
        self.assertIn("size 不是正整数", out)

    def test_dry_run_changes_nothing(self) -> None:
        tree = self.make_tree()
        before = {rel: tree.path(rel).read_bytes() for rel in (
            "qq_live_digest/version.py", "README.md", "android/app/build.gradle")}
        code, out = tree.run("--set", "1.1.0-beta.12", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("dry-run：未写入任何文件", out)
        self.assertIn("APP_VERSION 1.1.0-beta.11 → 1.1.0-beta.12", out)
        after = {rel: tree.path(rel).read_bytes() for rel in before}
        self.assertEqual(before, after)

    def test_set_updates_all_sites_and_keeps_version_code(self) -> None:
        tree = self.make_tree()
        code, out = tree.run("--set", "1.1.0-beta.12")
        self.assertEqual(code, 0, out)
        self.assertIn("versionCode 不能自动加，记得手动 +1", out)
        self.assertIn('APP_VERSION = "1.1.0-beta.12"', tree.path("qq_live_digest/version.py").read_text(encoding="utf-8"))
        readme = tree.path("README.md").read_text(encoding="utf-8")
        self.assertNotIn("1.1.0-beta.11", readme)
        self.assertIn("v1.1.0-beta.12", readme)
        self.assertIn("测试版（beta.12）", readme)
        # 正文里描述历史版本的 beta.11 不该被改
        self.assertIn("beta.11 只修装机问题", readme)
        gradle = tree.path("android/app/build.gradle").read_text(encoding="utf-8")
        self.assertIn("versionName '1.1.0-beta.12'", gradle)
        self.assertIn("versionCode 10", gradle)

    def test_set_rejects_malformed_version(self) -> None:
        tree = self.make_tree()
        code, out = tree.run("--set", "beta.12")
        self.assertEqual(code, 1, out)
        self.assertIn("版本号格式不对", out)


if __name__ == "__main__":
    unittest.main()
