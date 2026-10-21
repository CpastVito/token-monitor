#!/usr/bin/env pythonw.exe
# -*- coding: utf-8 -*-
"""
一键启动器（供桌面快捷方式调用，无控制台窗口）
==============================================

做三件事，且都是幂等的：
  1. 本地服务没起 → 起一个（pythonw 后台，无窗口）
  2. 桌宠没起 → 起一个（单实例靠 8799 端口判断）
  3. 打开仪表盘

已经全部在跑时，只会把仪表盘再打开一次，不会重复启动。
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

import prefs

BASE = Path(__file__).resolve().parent
PET_LOCK_PORT = 8799

# 走 ProxyHandler({}) 绕过系统代理：本机装了代理时，127.0.0.1 的请求可能被转出去，
# 探测结果就不再是"本地服务活没活"了。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _port_open(port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _server_ok(port: int) -> bool:
    """端口通不等于就是我们的服务，确认 /api/health 应答才算数。"""
    try:
        with _OPENER.open(f"http://127.0.0.1:{port}/api/health", timeout=1.0) as r:
            return r.status == 200
    except Exception:
        return False


def _wait_server(timeout: float = 4.0) -> int:
    """等服务起来，返回它真正监听的端口（首选端口被占时它会顺延）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        port = prefs.read_port()
        if _server_ok(port):
            return port
        time.sleep(0.1)
    return prefs.read_port()


def _pyw() -> str:
    exe = Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    return str(cand if cand.exists() else exe)


def _spawn(script: str, *args: str) -> None:
    subprocess.Popen(
        [_pyw(), str(BASE / script), *args],
        cwd=str(BASE),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        close_fds=True,
    )


def main() -> None:
    # 1) 服务：端口只能从端口文件读，不能硬认 8971 —— server.py 在首选端口被占时
    #    会顺延，认死端口就会误判"服务没起"，每点一次快捷方式多养一个孤儿进程。
    port = prefs.read_port()
    if not _server_ok(port):
        _spawn("server.py", "--no-browser")
        port = _wait_server()

    # 2) 桌宠（用单实例锁端口判断是否已在运行）
    if not _port_open(PET_LOCK_PORT):
        _spawn("overlay.py")

    # 3) 仪表盘
    time.sleep(0.4)
    try:
        webbrowser.open(f"http://127.0.0.1:{port}/")
    except Exception:
        pass


if __name__ == "__main__":
    main()
