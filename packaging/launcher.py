from __future__ import annotations

import os
import secrets
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path


def main() -> int:
    root = Path(sys.executable).resolve().parent
    data = root / "data"
    logs = root / "logs"
    data.mkdir(exist_ok=True)
    logs.mkdir(exist_ok=True)
    env_file = root / ".env"
    token = secrets.token_urlsafe(32)
    if not env_file.exists():
        env_file.write_text(
            "QQ_DIGEST_WEB=1\n"
            "QQ_DIGEST_WEB_HOST=127.0.0.1\n"
            "QQ_DIGEST_WEB_PORT=8766\n"
            f"QQ_DIGEST_WEB_TOKEN={token}\n"
            "QQ_DIGEST_ONEBOT_ENABLED=1\n"
            f"QQ_DIGEST_ONEBOT_TOKEN={secrets.token_urlsafe(32)}\n"
            "QQ_DIGEST_NAPCAT_API_TOKEN=\n"
            "QQ_DIGEST_NAPCAT_API_URL=http://127.0.0.1:3001\n"
            f"QQ_DIGEST_NAPCAT_QR_PATH={data / 'qrcode.png'}\n"
            "# 默认 auto：优先用本机 CodeBuddy/WorkBuddy CLI（消耗账号额度），没有 CLI 或调用失败会自动回退本机 Ollama，无需干预\n"
            "QQ_DIGEST_LLM=1\n"
            "QQ_DIGEST_LLM_BACKEND=auto\n"
            "QQ_DIGEST_LLM_MODEL=qwen3.6:35b-a3b-q4_k_m-gpu20\n"
            "QQ_DIGEST_LLM_ENDPOINT=http://127.0.0.1:11434/v1/chat/completions\n"
            "QQ_DIGEST_LLM_TIMEOUT=600\n"
            "QQ_DIGEST_VISION=0\n"
            "DASHSCOPE_API_KEY=ollama-local\n"
            f"QQ_DIGEST_DATA_DIR={data}\n"
            f"QQ_DIGEST_LOG_DIR={logs}\n",
            encoding="utf-8",
        )
    else:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("QQ_DIGEST_WEB_TOKEN="):
                token = line.split("=", 1)[1].strip()
                break
    app = root / "qq-live-digest.exe"
    # --env 是 main.py 的顶层参数，必须放在子命令 run 之前，否则 argparse 报
    # "unrecognized arguments: --env ..."。CREATE_NO_WINDOW 避免再弹一个控制台窗口，
    # 也避免用户误关那个窗口把服务一起关掉。
    process = subprocess.Popen(
        [str(app), "--env", str(env_file), "run"],
        cwd=root,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    url = "http://127.0.0.1:8766/setup?token=" + urllib.parse.quote(token, safe="")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8766/api/health", timeout=1) as response:
                if response.status == 200:
                    webbrowser.open(url)
                    return 0
        except Exception:
            if process.poll() is not None:
                return process.returncode or 1
        time.sleep(0.25)
    process.terminate()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
