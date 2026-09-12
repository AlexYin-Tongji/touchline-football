"""Local desktop launcher: reuse an existing service or start a bounded local one."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener
import webbrowser

ROOT = Path(__file__).resolve().parent
URL = "http://127.0.0.1:8765"
WORKSPACE_ID = hashlib.sha256(str(ROOT).lower().encode()).hexdigest()[:12]


def status():
    try:
        with build_opener(ProxyHandler({})).open(URL + "/api/service", timeout=1.5) as response:
            data = json.load(response)
        return data if data.get("workspace_id") == WORKSPACE_ID else None
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    ready = status()
    if args.check:
        print(json.dumps({"running": bool(ready), "url": URL}, ensure_ascii=False))
        return 0
    if not ready:
        if not (ROOT / "dist" / "index.html").exists():
            raise RuntimeError("页面尚未构建，请先运行 start.ps1 -Setup。")
        import uvicorn  # Fail before opening a hidden child if dependencies are missing.
        output = ROOT / "output"
        output.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        with (output / f"service-{stamp}.log").open("a", encoding="utf-8") as log:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "server.main:app", "--host", "127.0.0.1", "--port", "8765"],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=flags, start_new_session=os.name != "nt")
            for _ in range(40):
                if status():
                    ready = True
                    break
                if process.poll() is not None:
                    raise RuntimeError(f"服务未能启动，请查看 output/service-{stamp}.log。端口可能已被其他服务占用。")
                time.sleep(.5)
        if not ready:
            raise RuntimeError(f"服务仍在启动，请稍后打开 {URL} 或查看 output 中的日志。")
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "python-path.txt").write_text(sys.executable, encoding="utf-8")
    if not args.no_browser:
        webbrowser.open(URL)
    print("看台服务已就绪：" + URL)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        if os.name == "nt" and "--no-browser" not in sys.argv:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(exc), "看台启动提示", 0x10)
        raise SystemExit(1)
