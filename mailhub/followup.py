"""업체 요청 추적: 금형 수정 검토서, 수정 진행 요청 등에 기한을 걸고 회신을 챙긴다.

흐름
  1) 요청 등록   : req add --to 업체주소 --subject "보낸 메일 제목" --due 11/12
  2) 회신 자동확인: 매일 수집 후 req check → 같은 제목(RE:)으로 그 업체에서 메일이 오면 '회신받음'
  3) 알림       : 기한 지남 / 오늘·내일 기한 → 윈도우 알림 + 목록
  4) 재요청     : req remind 번호 → 아웃룩 새 메일 창에 재요청 메일을 띄움 (확인 후 직접 보내기)
                 config.toml [reminder] auto_send = true 이고 등록 시 --auto 를 준 건만 자동 발송
"""
from __future__ import annotations

import base64
import os
import re
import sqlite3
import subprocess
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import getaddresses
from pathlib import Path

from .classify import thread_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id              INTEGER PRIMARY KEY,
    created_at      TEXT,
    requested_at    TEXT,              -- 요청 메일 보낸 날 (YYYY-MM-DD)
    due_date        TEXT,              -- 회신 기한 (YYYY-MM-DD)
    to_addrs        TEXT,              -- 업체 담당자 주소 (쉼표 구분)
    cc_addrs        TEXT DEFAULT '',
    vendor          TEXT DEFAULT '',   -- 업체명 (표시용)
    subject         TEXT,              -- 요청 메일 제목
    thread_key      TEXT,
    req_type        TEXT DEFAULT '',   -- 금형수정검토 / 수정진행 / 검사구수정 / 견적 ...
    part_no         TEXT DEFAULT '',
    mail_id         INTEGER,           -- 관련(원인) 메일 번호
    note            TEXT DEFAULT '',
    status          TEXT DEFAULT '요청',  -- 요청 → 회신받음 → 완료 / 취소
    reply_mail_id   INTEGER,
    replied_at      TEXT,
    remind_count    INTEGER DEFAULT 0,
    last_reminded_at TEXT,
    auto_remind     INTEGER DEFAULT 0
);
"""

OPEN_STATUSES = ("요청",)
WEEKDAYS = "월화수목금토일"


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


# ---------- 날짜 ----------
def parse_due(s: str, today: date | None = None) -> str:
    """'2026-11-12', '11/12', '11-12', '+3'(3일 뒤), '+3영업일' → 'YYYY-MM-DD'"""
    today = today or date.today()
    s = (s or "").strip()
    m = re.fullmatch(r"\+(\d+)\s*(영업일|bd)?", s)
    if m:
        n, biz = int(m.group(1)), bool(m.group(2))
        d = today
        if not biz:
            return (d + timedelta(days=n)).isoformat()
        while n:
            d += timedelta(days=1)
            if d.weekday() < 5:
                n -= 1
        return d.isoformat()
    m = re.fullmatch(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    m = re.fullmatch(r"(\d{1,2})[-./](\d{1,2})", s)
    if m:
        d = date(today.year, int(m.group(1)), int(m.group(2)))
        if d < today - timedelta(days=180):  # 12월에 1월 기한 입력 등
            d = date(today.year + 1, d.month, d.day)
        return d.isoformat()
    raise ValueError(f"날짜 형식을 알 수 없습니다: {s} (예: 2026-11-12, 11/12, +3, +3영업일)")


def dday(due: str, today: date | None = None) -> str:
    today = today or date.today()
    d = date.fromisoformat(due)
    n = (d - today).days
    wd = WEEKDAYS[d.weekday()]
    if n == 0:
        return f"{d.month}/{d.day}({wd}) 오늘"
    return f"{d.month}/{d.day}({wd}) " + (f"D-{n}" if n > 0 else f"{-n}일 지남")


# ---------- 등록 / 조회 ----------
def _domains(addrs: str) -> list[str]:
    out = []
    for _, a in getaddresses([addrs or ""]):
        if "@" in a:
            out.append(a.split("@", 1)[1].lower())
    return sorted(set(out))


def add(conn, to: str, subject: str, due: str, cc: str = "", vendor: str = "", req_type: str = "",
        part_no: str = "", mail_id: int | None = None, note: str = "", requested: str | None = None,
        auto: bool = False, today: date | None = None) -> int:
    ensure(conn)
    today = today or date.today()
    if not _domains(to):
        raise ValueError("--to 에 업체 담당자 메일 주소를 넣어 주세요 (예: kim@abc-mold.co.kr)")
    cur = conn.execute(
        "INSERT INTO requests (created_at, requested_at, due_date, to_addrs, cc_addrs, vendor, subject, thread_key, "
        "req_type, part_no, mail_id, note, auto_remind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), parse_due(requested, today) if requested else today.isoformat(),
         parse_due(due, today), to, cc, vendor, subject, thread_key(subject), req_type, part_no, mail_id, note,
         int(auto)))
    conn.commit()
    return cur.lastrowid


def find_reply(conn, r: sqlite3.Row) -> sqlite3.Row | None:
    """요청 이후 그 업체(도메인)에서 같은 건(제목)으로 온 메일."""
    doms = _domains(r["to_addrs"])
    if not doms or not r["thread_key"]:
        return None
    dom_sql = " OR ".join("m.sender_email LIKE ?" for _ in doms)
    return conn.execute(
        f"SELECT m.id, m.sent_at, m.sender_name, m.subject FROM mails m "
        f"WHERE m.is_mine = 0 AND m.sent_at >= ? AND ({dom_sql}) "
        f"AND (m.thread_key = ? OR lower(m.subject) LIKE ?) ORDER BY m.sent_at LIMIT 1",
        (r["requested_at"], *[f"%@{d}" for d in doms], r["thread_key"], f"%{r['thread_key']}%")).fetchone()


def check(conn, cfg: dict, today: date | None = None) -> dict:
    """회신 자동 확인 후 분류: overdue(기한 지남) / soon(오늘·내일) / remind(재요청 대상) / replied(새로 회신받음)"""
    ensure(conn)
    today = today or date.today()
    rc = cfg.get("reminder", {})
    interval = int(rc.get("interval_days", 2))
    max_count = int(rc.get("max_count", 3))
    out = {"replied": [], "overdue": [], "soon": [], "remind": []}
    for r in conn.execute("SELECT * FROM requests WHERE status IN ('요청') ORDER BY due_date").fetchall():
        rep = find_reply(conn, r)
        if rep:
            conn.execute("UPDATE requests SET status='회신받음', reply_mail_id=?, replied_at=? WHERE id=?",
                         (rep["id"], rep["sent_at"], r["id"]))
            out["replied"].append((r, rep))
            continue
        due = date.fromisoformat(r["due_date"])
        if due < today:
            out["overdue"].append(r)
            last = date.fromisoformat(r["last_reminded_at"][:10]) if r["last_reminded_at"] else None
            if r["remind_count"] < max_count and (last is None or (today - last).days >= interval):
                out["remind"].append(r)
        elif (due - today).days <= 1:
            out["soon"].append(r)
    conn.commit()
    return out


# ---------- 재요청 메일 ----------
DEFAULT_TEMPLATE = """{greeting}

{requested_md}에 요청드린 "{subject}" 건 관련하여,
회신 기한({due_md})이 지나 다시 한번 요청드립니다.

진행 현황과 회신 가능 일정을 금일 중으로 회신 부탁드립니다.
일정 지연이 예상되는 경우 사유와 만회 계획도 함께 공유 부탁드립니다.

감사합니다.

{signature}"""


def reminder_mail(r: sqlite3.Row, cfg: dict) -> tuple[str, str]:
    rc = cfg.get("reminder", {})
    me = cfg.get("me", {})
    req = date.fromisoformat(r["requested_at"])
    due = date.fromisoformat(r["due_date"])
    n = (r["remind_count"] or 0) + 1
    prefix = rc.get("subject_prefix", "[재요청]") if n == 1 else f"[재요청 {n}차]"
    base = re.sub(r"^(re|fw|fwd)\s*:\s*", "", r["subject"], flags=re.I)
    subject = f"{prefix} {base}"
    body = (rc.get("template") or DEFAULT_TEMPLATE).format(
        greeting=rc.get("greeting", "안녕하십니까.\n" + (me.get("intro") or "")).strip(),
        subject=r["subject"], requested_md=f"{req.month}/{req.day}", due_md=f"{due.month}/{due.day}",
        vendor=r["vendor"] or "", part_no=r["part_no"] or "", count=n,
        signature=(me.get("signature") or "").strip())
    return subject, body


def open_draft(to: str, cc: str, subject: str, body: str, send: bool, draft_dir: Path) -> str:
    """아웃룩 새 메일 창을 띄운다 (send=True 면 바로 발송). 아웃룩이 없으면 .eml 초안 파일을 연다."""
    try:
        import win32com.client  # type: ignore
        ol = win32com.client.Dispatch("Outlook.Application")
        m = ol.CreateItem(0)
        m.To, m.CC, m.Subject, m.Body = to, cc or "", subject, body
        if send:
            m.Send()
            return "발송함(아웃룩)"
        m.Display()
        return "아웃룩 작성 창을 띄움"
    except Exception as e:
        if send:
            raise RuntimeError(f"자동 발송 실패: {e}") from e
    msg = EmailMessage()
    msg["To"], msg["Subject"], msg["X-Unsent"] = to, subject, "1"
    if cc:
        msg["Cc"] = cc
    msg.set_content(body)
    draft_dir.mkdir(parents=True, exist_ok=True)
    p = draft_dir / f"{datetime.now():%Y%m%d_%H%M%S}_재요청.eml"
    p.write_bytes(bytes(msg))
    if os.name == "nt":
        os.startfile(str(p))  # type: ignore[attr-defined]
    return f"초안 파일: {p}"


def mark_reminded(conn, rid: int) -> None:
    conn.execute("UPDATE requests SET remind_count = remind_count + 1, last_reminded_at = ? WHERE id = ?",
                 (datetime.now().isoformat(timespec="seconds"), rid))
    conn.commit()


# ---------- 윈도우 알림 ----------
def notify(title: str, message: str) -> bool:
    if os.name != "nt":
        return False

    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))

    ps = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml('<toast duration="long"><visual><binding template="ToastGeneric"><text>{esc(title)}</text><text>{esc(message)}</text></binding></visual></toast>')
$app = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($x))
"""
    enc = base64.b64encode(ps.encode("utf-16-le")).decode()
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False
