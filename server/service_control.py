"""User-operated local shortcuts; nothing is installed automatically at import."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import subprocess

from .build_info import BUILD_ID, VERSION

ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ID = hashlib.sha256(str(ROOT).lower().encode()).hexdigest()[:12]
STARTED = datetime.now(timezone.utc).isoformat()


def startup_folder():
    if os.name != "nt" or not os.environ.get("APPDATA"):
        return None
    return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"


def startup_path():
    folder = startup_folder()
    return folder / f"Touchline-{WORKSPACE_ID}.lnk" if folder else None


def service_status():
    path = startup_path()
    return {"version": VERSION, "build_id": BUILD_ID, "running": True, "autostart_enabled": bool(path and path.is_file()),
            "workspace_id": WORKSPACE_ID, "started_at": STARTED, "pid": os.getpid(), "url": "http://127.0.0.1:8765/"}


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def _shortcut(path_expression, *, no_browser=False):
    if os.name != "nt":
        raise ValueError("桌面快捷方式当前仅支持 Windows；可使用项目内启动脚本。")
    script = ("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); $shortcutPath=" + path_expression + "; "
        "$shortcutShell=New-Object -ComObject WScript.Shell; $shortcut=$shortcutShell.CreateShortcut($shortcutPath); "
        "$shortcut.TargetPath=Join-Path $env:WINDIR 'System32/wscript.exe'; "
        "$shortcut.Arguments=" + ps_quote('"' + str(ROOT / "launch.vbs") + '"' + (' --no-browser' if no_browser else '')) + "; "
        "$shortcut.WorkingDirectory=" + ps_quote(ROOT) + "; $shortcut.Description='看台足球助理'; "
        "$shortcut.IconLocation=" + (ps_quote(ROOT / "public" / "app.ico") if (ROOT / "public" / "app.ico").is_file() else "(Join-Path $env:WINDIR 'System32/shell32.dll')") + "; $shortcut.Save(); Write-Output $shortcutPath")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise ValueError("快捷方式未能创建，请检查当前用户的文件权限，或双击项目中的启动看台.cmd。")
    return result.stdout.strip()


def create_desktop_shortcut():
    path = _shortcut("(Join-Path ([Environment]::GetFolderPath('Desktop')) '看台 Touchline.lnk')")
    return {"ok": True, "message": "已创建桌面快捷方式。", "path": path}


def set_autostart(enabled):
    path = startup_path()
    if path is None:
        raise ValueError("登录自启当前仅支持 Windows。")
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        _shortcut(ps_quote(path), no_browser=True)
    else:
        # Delete only this application's exact per-workspace shortcut.
        expected = startup_folder().resolve()
        if path.resolve().parent != expected:
            raise ValueError("启动项路径校验失败。")
        path.unlink(missing_ok=True)
    return service_status()
