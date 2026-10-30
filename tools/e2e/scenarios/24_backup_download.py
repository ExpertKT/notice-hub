"""24 导出备份：点「导出备份」必须真的产生一个可用的 zip 下载。

harness 没有 download 门面，所以这里直接用 `app.page.cdp` 开
`Browser.setDownloadBehavior`（只写在本剧本里，不动 harness），
再把下载目录固定到本次 case 目录下的 `downloads/`，最后验三件事：
文件真的落盘且字节数 > 0、文件名是 `notice-hub-backup-YYYY-MM-DD.zip`、开头是 ZIP 魔数（不是错误页 HTML）。

复现：
    python tools/e2e/run.py --only 24_backup_download
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from harness import scenario

NAME_PATTERN = re.compile(r"^notice-hub-backup-\d{4}-\d{2}-\d{2}\.zip$")


def _dismiss_wizard(app) -> None:
    visible = app.page.evaluate(
        "(function(){var w=document.getElementById('platform-setup-wizard');return !!(w&&!w.hidden);})()"
    )
    if visible:
        app.click("#platform-setup-wizard .platform-setup-later")
        app.wait_for("#platform-setup-wizard", state="hidden", timeout=10)


def _click_scrolled(app, selector: str, settle: float = 0.3) -> None:
    app.page.evaluate(
        "(function(){var el=document.querySelector(%s);if(!el)return false;"
        "el.scrollIntoView({block:'center',inline:'center'});return true;})()"
        % __import__("json").dumps(selector)
    )
    app.settle(settle)
    app.click(selector)


@scenario("24_backup_download", viewports=("desktop",), tags=("fakes:none", "settings", "backup"))
def run(app) -> None:
    """导出备份：必须真下载、文件名合规、内容是 zip。"""
    download_dir = Path(app.out_dir) / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    case_dir = Path(app.out_dir).resolve()
    assert str(download_dir.resolve()).startswith(str(case_dir)), (
        f"下载目录必须在本次 case 目录里：{download_dir} 不在 {case_dir} 下"
    )

    app.goto("/")
    _dismiss_wizard(app)
    app.click("#tab-settings-button")
    app.wait_for("#tab-settings", state="visible", timeout=10)
    app.wait_for("#backup-download", timeout=20)
    app.shot("01-backup-card")

    app.page.cdp.call(
        "Browser.setDownloadBehavior",
        behavior="allow",
        downloadPath=str(download_dir),
        eventsEnabled=True,
    )

    _click_scrolled(app, "#tab-settings #backup-download")

    # 等文件落盘：先出现 .crdownload 再改名，所以只认最终 .zip
    deadline = time.monotonic() + 40
    files: list[Path] = []
    while time.monotonic() < deadline:
        files = [p for p in download_dir.glob("*.zip") if p.is_file()]
        if files:
            app.settle(0.3)
            files = [p for p in download_dir.glob("*.zip") if p.is_file()]
            if files:
                break
        app.settle(0.2)

    leftovers = [p.name for p in download_dir.iterdir()]
    assert files, f"40 秒内没有收到 zip 下载；目录内容：{leftovers!r}"
    assert len(files) == 1, f"收到多个 zip：{[p.name for p in files]!r}"

    target = files[0]
    assert NAME_PATTERN.match(target.name), f"备份文件名不合规：{target.name!r}（应为 notice-hub-backup-YYYY-MM-DD.zip）"
    size = target.stat().st_size
    assert size > 0, f"备份文件是空的：{target}"
    head = target.read_bytes()[:4]
    assert head[:2] == b"PK", f"备份不是 zip（开头 {head!r}），可能是错误页"
    app.shot("02-backup-downloaded")

    # 页面这边也要给出成功反馈，不能静默
    try:
        result_text = app.text("#tab-settings #backup-result").strip()
    except AssertionError:
        result_text = ""
    assert "失败" not in result_text and "错误" not in result_text, f"导出报了错：{result_text!r}"
    assert result_text, "导出后 #backup-result 是空的，用户看不到任何反馈"

    print(f"BACKUP_FILE name={target.name} size={size} head={head!r} dir={download_dir}")
    app.assert_no_console_errors()
