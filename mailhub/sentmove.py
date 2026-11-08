"""퇴근 전 알림: 그룹웨어 '보낸편지함' 메일을 '받은편지함'으로 옮기기.

POP3 로는 받은편지함만 수집되므로, 퇴근 전에 오늘 보낸 메일을 받은편지함으로 옮겨 두면
다음 수집 때 '내가 보낸 메일'로 들어와 회신 여부 판단·이력 검색이 정확해진다.
- 평일 19:00 / 20:00 / 21:00 / 22:00 (config [sent_move] times) 에 확인
- 오늘 보낸 메일이 이미 수집되어 있으면 알리지 않음 (옮기고 수집 완료)
- 알림 창: [옮겼어요 - 지금 수집] / [나중에]
"""
from __future__ import annotations

import base64
import os
import subprocess
from datetime import date, datetime

DEFAULT_TIMES = ["19:00", "20:00", "21:00", "22:00"]


def settings(cfg: dict) -> dict:
    s = cfg.get("sent_move", {})
    return {"enabled": s.get("enabled", True), "times": s.get("times", DEFAULT_TIMES),
            "weekdays": s.get("weekdays", [0, 1, 2, 3, 4])}


def sent_today(conn, today: date | None = None) -> int:
    d = (today or date.today()).isoformat()
    return conn.execute("SELECT COUNT(*) FROM mails WHERE is_mine=1 AND substr(sent_at,1,10)=?", (d,)).fetchone()[0]


def status(conn, cfg: dict, now: datetime | None = None) -> dict:
    """앱 화면용: 지금 배너를 보여줄지."""
    now = now or datetime.now()
    s = settings(cfg)
    first = min(s["times"]) if s["times"] else "19:00"
    n = sent_today(conn, now.date())
    show = bool(s["enabled"] and now.weekday() in s["weekdays"] and now.strftime("%H:%M") >= first and n == 0)
    return {"show": show, "sent_today": n, "first": first, "times": s["times"]}


POPUP = r"""
Add-Type -AssemblyName System.Windows.Forms
$f = New-Object Windows.Forms.Form
$f.TopMost = $true
$r = [Windows.Forms.MessageBox]::Show($f, "__MSG__", '퇴근 전 확인 - 업무 비서', 'YesNo', 'Information')
if ($r -eq 'Yes') { Write-Output 'ANSWER=yes' } else { Write-Output 'ANSWER=no' }
"""
MSG = ("퇴근 전에 그룹웨어 [보낸편지함]의 오늘 보낸 메일을 [받은편지함]으로 옮겨 주세요.\n"
       "(업무 비서가 내가 보낸 메일까지 백업하고, 회신한 건을 정확히 정리합니다)\n\n"
       "옮겼으면 [예]를 누르세요. 바로 수집합니다.\n나중에 할 거면 [아니요] - 다음 시간에 다시 알려 드립니다.")


def ask(message: str = MSG) -> bool:
    if os.name != "nt":
        return False
    # PowerShell 큰따옴표 문자열: ` " $ 는 이스케이프, 줄바꿈은 `n
    safe = message.replace("`", "``").replace('"', '`"').replace("$", "`$").replace("\n", "`n")
    ps = POPUP.replace("__MSG__", safe)
    enc = base64.b64encode(ps.encode("utf-16-le")).decode()
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
                           capture_output=True, text=True, timeout=4 * 3600)
        return "ANSWER=yes" in (r.stdout or "")
    except Exception:
        return False


def check_and_ask(conn_factory, cfg: dict, sync_fn, log=print, now: datetime | None = None, asker=ask) -> str:
    """오늘 보낸 메일이 없으면 알림 창. [예] 면 수집 후 결과 알림."""
    now = now or datetime.now()
    s = settings(cfg)
    if not s["enabled"] or now.weekday() not in s["weekdays"]:
        return "평일 아님/꺼짐"
    c = conn_factory()
    try:
        if sent_today(c, now.date()):
            return "이미 수집됨"
    finally:
        c.close()
    if not asker():
        return "나중에"
    sync_fn()
    c = conn_factory()
    try:
        n = sent_today(c, now.date())
    finally:
        c.close()
    from .followup import notify
    notify("보낸 메일 수집 완료" if n else "보낸 메일이 아직 없습니다",
           f"오늘 보낸 메일 {n}건을 백업했습니다" if n else "받은편지함으로 옮긴 뒤 '새 메일 받기'를 눌러 주세요")
    log(f"보낸 메일 수집: {n}건")
    return f"수집 {n}건"


class Scheduler:
    """앱이 켜져 있는 동안 정해진 시각마다 확인 (같은 시각은 하루 한 번)."""

    def __init__(self, conn_factory, cfg: dict, sync_fn, log=print):
        import threading
        self.conn_factory, self.cfg, self.sync_fn, self.log = conn_factory, cfg, sync_fn, log
        self.fired: set[str] = set()
        self.busy = False
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self._stop.set()

    def tick(self, now: datetime | None = None):
        now = now or datetime.now()
        s = settings(self.cfg)
        hm = now.strftime("%H:%M")
        key = f"{now.date()} {hm}"
        if hm in s["times"] and key not in self.fired and not self.busy:
            self.fired.add(key)
            self.busy = True
            try:
                return check_and_ask(self.conn_factory, self.cfg, self.sync_fn, self.log, now)
            finally:
                self.busy = False
        return None

    def _loop(self):
        while not self._stop.wait(20):
            try:
                self.tick()
            except Exception as e:  # 알림 실패로 앱이 멈추지 않게
                self.log(f"보낸메일 알림 오류: {e}")
