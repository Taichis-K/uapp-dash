"""プロセス生存確認。

**Windows では os.kill(pid, 0) を使ってはならない**。CPython の Windows 実装は
CTRL_C_EVENT / CTRL_BREAK_EVENT 以外のシグナルを TerminateProcess(handle, sig) に
写像するため、生存確認のつもりで対象プロセスを終了させてしまう。
ここでは Win32 の OpenProcess + GetExitCodeProcess で判定する。
"""
from __future__ import annotations

import os
import socket
import sys

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5


def hostname() -> str:
    return socket.gethostname()


def _alive_windows(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # アクセス拒否は「存在するが権限が無い」＝生存扱い（別ユーザーのプロセス）
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True  # 判定できないときは生存側に倒す（誤って crashed と断定しない）
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _alive_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_alive(pid) -> bool | None:
    """pid が生存しているか。判定できない場合は None（呼び手は crashed と断定しないこと）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        if sys.platform == "win32":
            return _alive_windows(pid)
        return _alive_posix(pid)
    except OSError:
        return None


def alive_on_this_host(owner: dict | None) -> bool | None:
    """owner が自ホストのものであれば生存を判定する。他ホストなら None（判定不能）。"""
    if not isinstance(owner, dict):
        return None
    host = owner.get("host")
    # host が無い記録は「どのマシンの pid か分からない」＝判定不能。ここでローカル pid として
    # 検査すると、別ホストの単位を crashed と誤断定する（仕様: 判定不能なら stalled 止まり）
    if not host or str(host).lower() != hostname().lower():
        return None
    return process_alive(owner.get("pid"))
