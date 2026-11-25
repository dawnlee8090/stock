"""바탕화면 · 시작 메뉴에 '현지 금형 육성 관리' 앱 바로가기 만들기 (Windows).

바로가기를 누르면 scripts/localmold.bat 이 앱 엔진을 작은 검은 창(최소화)으로 켜고 앱 창을 연다.
이미 켜져 있으면 창만 다시 연다. 아이콘은 localmold/web/localmold.ico.
"""
from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path

NAME = "현지 금형 육성 관리"
ROOT = Path(__file__).resolve().parent.parent
ICON = Path(__file__).resolve().parent / "web" / "localmold.ico"

# 값은 환경 변수로 넘긴다 (경로 · 한글 · 공백 따옴표 문제 없게). 바탕화면 위치는 Windows 에 묻는다 (OneDrive 바탕화면 포함)
_PS = r"""
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
$sh = New-Object -ComObject WScript.Shell
$dirs = @([Environment]::GetFolderPath('Desktop'))
if ($env:LM_START -eq '1') { $dirs += [Environment]::GetFolderPath('Programs') }
foreach ($d in $dirs) {
  $p = Join-Path $d ($env:LM_NAME + '.lnk')
  $s = $sh.CreateShortcut($p)
  $s.TargetPath = $env:LM_BAT
  $s.WorkingDirectory = $env:LM_ROOT
  $s.IconLocation = $env:LM_ICO + ',0'
  $s.WindowStyle = 7
  $s.Description = $env:LM_DESC
  $s.Save()
  Write-Output $p
}
"""


def command(root: Path = ROOT, start_menu: bool = False) -> tuple[list[str], dict]:
    """PowerShell 실행 인자와 환경 변수 (테스트용으로 분리)."""
    bat = Path(root) / "scripts" / "localmold.bat"
    enc = base64.b64encode(_PS.encode("utf-16-le")).decode("ascii")
    env = {"LM_NAME": NAME, "LM_BAT": str(bat), "LM_ROOT": str(Path(root)), "LM_ICO": str(ICON),
           "LM_START": "1" if start_menu else "0", "LM_DESC": "현지 금형 육성 관리 (차종 · 외주처 · 문제점 · 사진)"}
    return ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc], env


def make(root: Path = ROOT, start_menu: bool = False) -> list[str]:
    """바로가기를 만들고 만든 파일 경로를 돌려준다."""
    if os.name != "nt":
        raise SystemExit("바탕화면 바로가기는 Windows PC 에서만 만들 수 있습니다")
    bat = Path(root) / "scripts" / "localmold.bat"
    if not bat.is_file():
        raise SystemExit(f"실행 파일이 없습니다: {bat}")
    args, env = command(root, start_menu)
    r = subprocess.run(args, env={**os.environ, **env}, capture_output=True, timeout=60)
    out = r.stdout.decode("utf-8", "replace").strip()
    if r.returncode != 0 or not out:
        err = r.stderr.decode("utf-8", "replace").strip() or r.stderr.decode("cp949", "replace").strip()
        raise SystemExit(f"바로가기를 만들지 못했습니다: {err or '알 수 없는 오류'}")
    return [x.strip() for x in out.splitlines() if x.strip()]
