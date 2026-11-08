"""파일 열기 (Windows). 메일 파일(.eml/.msg)은 '새 Outlook' 대신 클래식 Outlook(OUTLOOK.EXE)으로 연다.

Windows 기본 앱이 '새 Outlook'으로 지정되어 있으면 .eml 을 열 때 로그인 화면이 뜨기 때문.
클래식 Outlook 명령줄: OUTLOOK.EXE /eml "파일.eml",  OUTLOOK.EXE /f "파일.msg"
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def find_outlook() -> str | None:
    """클래식 Outlook(OUTLOOK.EXE) 위치. 레지스트리 App Paths → 흔한 설치 경로 순."""
    if os.name != "nt":
        return None
    try:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for key in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\OUTLOOK.EXE",
                        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\OUTLOOK.EXE"):
                try:
                    with winreg.OpenKey(root, key) as k:
                        p = winreg.QueryValue(k, None)
                        if p and Path(p.strip('"')).exists():
                            return p.strip('"')
                except OSError:
                    continue
    except ImportError:
        pass
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        for sub in (r"Microsoft Office\root\Office16", r"Microsoft Office\Office16", r"Microsoft Office\root\Office15"):
            p = Path(base) / sub / "OUTLOOK.EXE"
            if p.exists():
                return str(p)
    return None


def open_file(path: str | os.PathLike) -> str:
    """파일 열기. 메일 파일은 클래식 Outlook, 나머지는 Windows 기본 앱. 무엇으로 열었는지 반환."""
    p = str(Path(path).resolve())
    ext = Path(p).suffix.lower()
    if ext in (".eml", ".msg"):
        exe = find_outlook()
        if exe:
            subprocess.Popen([exe, "/eml" if ext == ".eml" else "/f", p])
            return "Outlook"
    if os.name == "nt":
        os.startfile(p)  # type: ignore[attr-defined]
        return "기본 앱"
    raise RuntimeError("Windows 에서만 열 수 있습니다")
