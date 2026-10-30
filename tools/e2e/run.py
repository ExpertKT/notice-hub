"""notice-hub E2E 入口。

    python tools/e2e/run.py                 # 全部剧本 × 各自声明的视口，每个用例一份全新冷装
    python tools/e2e/run.py --only demo     # 按名字/tag 过滤（可重复、可逗号分隔）
    python tools/e2e/run.py --list          # 只列剧本
    python tools/e2e/run.py --reuse         # 同一冷装实例跑到挂再重建（仅限不需要替身的剧本）
    python tools/e2e/run.py --keep          # 保留本次运行的目录（打印路径）
    python tools/e2e/run.py --headful       # 有头浏览器（调试用）
    python tools/e2e/run.py --zip <path>    # 换一个安装包
    python tools/e2e/run.py --run-dir <p>   # 产物根目录（也可用 NH_E2E_RUN_DIR）

运行产物一律在 `%TEMP%\\nh-e2e-<run-id>` 或 `NH_E2E_RUN_DIR` 指定的目录里；
**绝不回落进仓库**（建不出来就报错退出）。

退出码：全部通过 0，有任何失败 1，跑不起来（包缺 / 浏览器缺 / 冷装失败 / 运行目录不可用）2。
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import harness  # noqa: E402
import scenarios as scenario_package  # noqa: E402
from harness import install as install_module  # noqa: E402
from harness import paths as paths_module  # noqa: E402
from harness import report as report_module  # noqa: E402
from harness.cdp import free_port  # noqa: E402


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="run.py", description="notice-hub 冷装 E2E")
    parser.add_argument("--only", action="append", default=[], help="按名字或 tag 过滤；可重复")
    parser.add_argument("--list", action="store_true", help="只列剧本，不执行")
    parser.add_argument("--keep", action="store_true", help="保留本次运行目录并打印路径")
    parser.add_argument("--reuse", action="store_true", help="复用同一实例（仅 fakes:none 的剧本）")
    parser.add_argument("--headful", action="store_true", help="有头浏览器（调试用）")
    parser.add_argument("--zip", default="", help="安装包路径，默认 packaging/QQ-Notice-Hub.zip")
    parser.add_argument("--run-dir", default="", help="产物根目录（默认 %%TEMP%%；禁止仓库内）")
    parser.add_argument(
        "--from-source", action="store_true",
        help="源码模式：不解包，复制仓库最小源码树用系统 python 起 main.py run（改完代码即时验证用；发版前仍请跑 zip 模式）",
    )
    return parser.parse_args(argv)


def select(scenarios: list[harness.Scenario], only: list[str]) -> list[harness.Scenario]:
    needles = [needle.strip() for item in only for needle in item.split(",") if needle.strip()]
    return [item for item in scenarios if item.matches(needles)]


def fake_names(tags: tuple[str, ...]) -> list[str]:
    names = []
    for tag in tags:
        text = str(tag)
        if not text.startswith("fakes:"):
            continue
        name = text.split(":", 1)[1].strip().lower()
        if name and name != "none":
            names.append(name)
    return names


def start_fakes(
    names: list[str],
    *,
    run_id: str,
    case_dir: Path,
    base_url: str,
    token: str,
    onebot_port: int | None = None,
):
    """按 tag 起替身。必须早于冷装：替身给的 env（如微信目录）要写进被测实例的 .env。"""
    if not names:
        return [], {}
    from fakes import start as start_fake  # 顶层 fakes 包（W3 维护）

    handles: list = []
    env: dict[str, str] = {}
    for name in names:
        options: dict = {}
        if name == "onebot":
            # OneBot 事件不是打给 Web UI 的：被测实例自带接收器（QQ_DIGEST_ONEBOT_HOST:PORT），
            # 所以 target_url 必须指向那个端口，否则注入的事件没人收。
            target = f"http://127.0.0.1:{onebot_port}/onebot/event" if onebot_port else f"{base_url}/onebot/event"
            options = {"token": token, "target_url": target, "target_token": token}
        elif name in {"wechat", "dingtalk", "push"}:
            # 这三个会造目录/落盘：统一把 root 指到本轮用例目录，产物跟着 run dir 走。
            options = {"run_id": run_id, "root": str(case_dir / name)}
        handle = start_fake(name, **options)
        env.update(handle.env or {})
        if name == "onebot" and onebot_port:
            # 打开接收器；token 同时是 NapCat API token（config.py:348 回落），替身与实例才对得上。
            env.update({
                "QQ_DIGEST_ONEBOT_ENABLED": "1",
                "QQ_DIGEST_ONEBOT_HOST": "127.0.0.1",
                "QQ_DIGEST_ONEBOT_PORT": str(onebot_port),
                "QQ_DIGEST_ONEBOT_TOKEN": token,
            })
        handles.append(handle)
    return handles, env


def stop_fakes(handles: list) -> None:
    while handles:
        handle = handles.pop()
        try:
            handle.stop()
        except Exception:  # noqa: BLE001 - 收尾失败不该盖住真正的失败
            pass


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    all_scenarios = scenario_package.load()
    chosen = select(all_scenarios, args.only)

    if args.list:
        for item in all_scenarios:
            mark = "*" if item in chosen else " "
            print(f"{mark} {item.name:<32} 视口={','.join(item.viewports):<14} tags={','.join(item.tags) or '-'}")
        print(f"\n共 {len(all_scenarios)} 个剧本，选中 {len(chosen)} 个")
        return 0

    if not chosen:
        print("没有匹配的剧本：--only " + ", ".join(args.only), file=sys.stderr)
        return 2

    run_id = report_module.new_run_id()
    paths_module.set_work_root(args.run_dir or os.environ.get("NH_E2E_WORK_ROOT") or None)
    run_root = paths_module.run_dir(run_id)
    zip_path = args.zip or str(install_module.DEFAULT_ZIP)
    mode = "source" if args.from_source else "zip"
    report = report_module.RunReport(
        run_id=run_id, zip_path=zip_path, keep=args.keep, reuse=args.reuse, mode=mode, root=run_root
    )
    print(
        f"E2E run {run_id}｜模式：{mode}｜包：{zip_path}｜剧本 {len(chosen)} 个｜复用={'是' if args.reuse else '否'}"
    )
    print(f"运行目录：{run_root}")

    state: dict = {"shared": None}
    started = time.time()
    try:
        for scenario in chosen:
            for viewport in scenario.viewports:
                report.add(run_case(scenario, viewport, args, report, state))
    finally:
        shared = state["shared"]
        if shared is not None:
            if args.keep or report.failed:
                shared.keep = True
            shared.teardown()
        report.finished_at = time.time()

    json_path, md_path = report.write()
    report.print_table()
    print(f"耗时 {report.finished_at - started:.1f}s｜JSON：{json_path}")
    print(f"Markdown：{md_path}")
    return 1 if report.failed else 0


def run_case(scenario, viewport, args, report, state) -> harness.CaseResult:
    out_dir = report.case_dir(scenario.name, viewport)
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    install = None
    app = None
    fakes: list = []
    shared = state["shared"]
    names = fake_names(scenario.tags)
    print(f"\n▶ {scenario.name} [{viewport}]")
    try:
        if names and args.reuse:
            raise AssertionError(
                "需要平台替身（%s）的剧本不支持 --reuse：替身的 env 必须在冷装前写进 .env。" % ",".join(names)
            )
        if shared is not None and shared.alive():
            install = shared
            print(f"  复用实例 {install.base_url}")
        else:
            port = free_port()
            token = secrets.token_hex(24)
            onebot_port = free_port(exclude={port}) if "onebot" in names else None
            fakes, fake_env = start_fakes(
                names,
                run_id=report.run_id,
                case_dir=out_dir,
                base_url=f"http://127.0.0.1:{port}",
                token=token,
                onebot_port=onebot_port,
            )
            if names:
                print(f"  替身 {', '.join(names)}｜env {', '.join(sorted(fake_env))}")
            install = harness.cold_install(
                args.zip or None,
                keep=args.keep,
                port=port,
                token=token,
                extra_env=fake_env,
                root=out_dir,
                from_source=bool(args.from_source),
            )
            if args.reuse:
                state["shared"] = install
            print(f"  冷装 {install.base_url}｜{install.root}")
        if "wechat" in names and not install.has_env(harness.App.WECHAT_DIR_ENV):
            raise AssertionError(
                "微信替身没把 %s 写进被测实例的 .env：拒绝让服务端 locate_storage() 去扫真实微信目录。"
                % harness.App.WECHAT_DIR_ENV
            )
        app = harness.App(
            install,
            viewport=viewport,
            out_dir=out_dir,
            headful=args.headful,
            fakes={handle.name: handle for handle in fakes},
            # reuse 时同一个 install 被多个用例共用：每个用例单独一份 profile，别串状态。
            profile_tag=(f"{scenario.name}-{int(started)}" if args.reuse else ""),
        ).open()
        scenario.func(app)
        # M4：失败请求/console 报错不再靠剧本自觉，收尾统一判（NH_E2E_SKIP_REQUEST_ASSERT=1 可关）。
        if not os.environ.get("NH_E2E_SKIP_REQUEST_ASSERT"):
            app.assert_no_failed_requests()
            app.assert_no_console_errors()
            failed_requests = app.failed_request_count()
        else:
            failed_requests = 0
        seconds = time.time() - started
        print(f"  ✓ 通过 {seconds:.1f}s，截图 {len(app.shots)} 张，失败请求断言 {failed_requests} 条")
        return harness.CaseResult(
            scenario=scenario.name,
            viewport=viewport,
            ok=True,
            seconds=seconds,
            screenshots=list(app.shots),
            out_dir=str(out_dir),
            failed_requests=failed_requests,
        )
    except BaseException as error:  # noqa: BLE001 - 任何异常都落成一个失败用例，不中断整轮
        seconds = time.time() - started
        text = _describe(error)
        failed_requests = app.failed_request_count() if app is not None else 0
        shots: list[str] = []
        if app is not None:
            try:
                shots.append(app.shot(f"failure-{viewport}"))
            except Exception:  # noqa: BLE001 - 截图失败别掩盖原始错误
                pass
        print(f"  ✗ 失败 {seconds:.1f}s：{text.splitlines()[0] if text else '未知错误'}")
        if install is not None and install is not shared:
            install.keep = True  # 失败现场留着，方便按报告里的路径复现
        return harness.CaseResult(
            scenario=scenario.name,
            viewport=viewport,
            ok=False,
            seconds=seconds,
            error=text,
            screenshots=shots,
            out_dir=str(out_dir),
            failed_requests=failed_requests,
        )
    finally:
        if app is not None:
            try:
                app.close()
            except Exception:  # noqa: BLE001
                pass
        stop_fakes(fakes)
        if install is not None and install is not shared:
            install.teardown()


def _describe(error: BaseException) -> str:
    if isinstance(error, AssertionError):
        return str(error) or "断言失败（无说明）"
    lines = traceback.format_exception(type(error), error, error.__traceback__)
    return "".join(lines[-4:]).strip()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (harness.ColdInstallError, RuntimeError) as error:
        print(f"跑不起来：{error}", file=sys.stderr)
        raise SystemExit(2)
