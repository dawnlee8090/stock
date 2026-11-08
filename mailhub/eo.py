"""E.O(설계변경) 리스트 관리: 접수 → 업체 검토 → 금형 수정 → 검증(T/O) → 완료.

- 항목 단위: E.O 1건 안에 품번이 여러 개면 품번(부품)별로 1행씩 관리한다.
- 단계가 바뀔 때마다 날짜·메모를 이력(eo_log)에 남긴다.
- 기한(검토 회신 / 수정 완료 / 검증)이 지나면 '지연'으로 표시하고 알림.
- 누락 방지: E.O 메일 중 아직 리스트에 연결 안 된 메일(eo inbox),
  등록된 E.O 의 번호·품번이 나오는 새 메일 자동 연결(eo sync).
"""
from __future__ import annotations

import csv
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

from .followup import parse_due

SCHEMA = """
CREATE TABLE IF NOT EXISTS eos (
    id            INTEGER PRIMARY KEY,
    eo_no         TEXT DEFAULT '',     -- E.O 번호
    car           TEXT DEFAULT '',     -- 차종 (BC4t CUV 등)
    part_no       TEXT DEFAULT '',
    part_name     TEXT DEFAULT '',
    change_desc   TEXT DEFAULT '',     -- 변경 내용
    customer      TEXT DEFAULT '',     -- 발행처 (고객사/설계팀)
    received_at   TEXT DEFAULT '',     -- 접수일
    mold_vendor   TEXT DEFAULT '',
    mold_action   TEXT DEFAULT '',     -- 육성 / 가공 / 입자교체 / 신규 / 수정불필요
    cf_impact     TEXT DEFAULT '',     -- 검사구 영향: 필요 / 불필요 / 검토중
    cf_vendor     TEXT DEFAULT '',
    apply_point   TEXT DEFAULT '',     -- 적용 시점 (이벤트, SOP, 재고 소진 후 등)
    cost          TEXT DEFAULT '',     -- 수정비
    status        TEXT DEFAULT '접수',
    review_due    TEXT DEFAULT '',     -- 업체 검토 회신 기한
    mold_due      TEXT DEFAULT '',     -- 금형 수정 완료 목표
    verify_due    TEXT DEFAULT '',     -- 검증(T/O) 목표
    reviewed_at   TEXT DEFAULT '',
    ordered_at    TEXT DEFAULT '',     -- 수정 요청(발주)일
    mold_done_at  TEXT DEFAULT '',
    verified_at   TEXT DEFAULT '',
    mail_id       INTEGER,             -- 최초 접수 메일
    note          TEXT DEFAULT '',
    created_at    TEXT,
    updated_at    TEXT,
    seen_mail_max INTEGER DEFAULT 0    -- 마지막으로 확인한 관련 메일 id (새 메일 표시용)
);
CREATE TABLE IF NOT EXISTS eo_mails (
    eo_id INTEGER, mail_id INTEGER, how TEXT, linked_at TEXT,
    PRIMARY KEY (eo_id, mail_id)
);
CREATE TABLE IF NOT EXISTS eo_log (
    id INTEGER PRIMARY KEY, eo_id INTEGER, at TEXT, from_status TEXT, to_status TEXT, note TEXT, mail_id INTEGER
);
"""

STATUSES = ["접수", "검토중", "수정요청", "수정중", "수정완료", "검증중", "완료", "수정불필요", "보류", "취소"]
CLOSED = ("완료", "수정불필요", "취소")
# 상태가 바뀔 때 자동으로 채우는 날짜
STAMP = {"수정요청": "ordered_at", "수정완료": "mold_done_at", "완료": "verified_at"}

FIELDS = ["eo_no", "car", "part_no", "part_name", "change_desc", "customer", "received_at", "mold_vendor",
          "mold_action", "cf_impact", "cf_vendor", "apply_point", "cost", "review_due", "mold_due", "verify_due",
          "reviewed_at", "ordered_at", "mold_done_at", "verified_at", "note"]
DATE_FIELDS = {"received_at", "review_due", "mold_due", "verify_due", "reviewed_at", "ordered_at",
               "mold_done_at", "verified_at"}

# 엑셀/CSV 열 이름
HEADERS = {
    "id": "No", "eo_no": "E.O No", "car": "차종", "part_no": "품번", "part_name": "품명", "change_desc": "변경내용",
    "customer": "발행처", "received_at": "접수일", "mold_vendor": "금형업체", "mold_action": "수정방식",
    "cf_impact": "검사구영향", "cf_vendor": "검사구업체", "status": "상태", "review_due": "검토회신기한",
    "reviewed_at": "검토완료일", "ordered_at": "수정요청일", "mold_due": "수정완료목표", "mold_done_at": "수정완료일",
    "verify_due": "검증목표", "verified_at": "검증완료일", "apply_point": "적용시점", "cost": "수정비",
    "delay": "지연", "mails": "관련메일", "note": "메모",
}


PART_RE = re.compile(r"(\d{5})((?:/\d{1,2})*)-([A-Z0-9]{5})")


def expand_parts(text: str) -> list[str]:
    """'86525/26-BNGA0, 86553-BNGA0' → ['86525-BNGA0', '86526-BNGA0', '86553-BNGA0'] (LH/RH 줄임 표기 풀기)"""
    out: list[str] = []
    for base, alts, suffix in PART_RE.findall((text or "").upper()):
        out.append(f"{base}-{suffix}")
        for a in [x for x in alts.split("/") if x]:
            out.append(f"{base[:5 - len(a)]}{a}-{suffix}")
    seen: list[str] = []
    for p in out:
        if p not in seen:
            seen.append(p)
    return seen


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _norm_date(v: str, today: date | None = None) -> str:
    return parse_due(v, today) if v else ""


def add(conn, today: date | None = None, status: str = "접수", **kw) -> int:
    ensure(conn)
    today = today or date.today()
    if status not in STATUSES:
        raise ValueError(f"상태는 {', '.join(STATUSES)} 중 하나")
    vals = {f: (kw.get(f) or "") for f in FIELDS}
    if not (vals["part_no"] or vals["part_name"] or vals["eo_no"]):
        raise ValueError("E.O 번호, 품번, 품명 중 하나는 넣어 주세요")
    for f in DATE_FIELDS:
        vals[f] = _norm_date(vals[f], today)
    vals["received_at"] = vals["received_at"] or today.isoformat()
    cols = ", ".join(FIELDS)
    cur = conn.execute(
        f"INSERT INTO eos ({cols}, status, mail_id, created_at, updated_at) VALUES "
        f"({', '.join('?' * len(FIELDS))}, ?, ?, ?, ?)",
        (*[vals[f] for f in FIELDS], status, kw.get("mail_id"), _now(), _now()))
    eid = cur.lastrowid
    conn.execute("INSERT INTO eo_log (eo_id, at, from_status, to_status, note, mail_id) VALUES (?,?,?,?,?,?)",
                 (eid, _now(), "", status, "등록", kw.get("mail_id")))
    if kw.get("mail_id"):
        link(conn, eid, kw["mail_id"], "등록")
        conn.execute("UPDATE eos SET seen_mail_max=? WHERE id=?", (kw["mail_id"], eid))  # 등록 메일은 확인한 것
    conn.commit()
    return eid


def update(conn, eid: int, status: str | None = None, note: str | None = None, mail_id: int | None = None,
           today: date | None = None, **fields) -> sqlite3.Row:
    ensure(conn)
    today = today or date.today()
    r = conn.execute("SELECT * FROM eos WHERE id=?", (eid,)).fetchone()
    if not r:
        raise ValueError(f"E{eid} 항목이 없습니다")
    sets = {}
    for f, v in fields.items():
        if f in FIELDS and v is not None:
            sets[f] = _norm_date(v, today) if f in DATE_FIELDS else v
    if status and status != r["status"]:
        if status not in STATUSES:
            raise ValueError(f"상태는 {', '.join(STATUSES)} 중 하나")
        sets["status"] = status
        stamp = STAMP.get(status)
        if stamp and not r[stamp] and stamp not in sets:
            sets[stamp] = today.isoformat()
        if status in ("수정요청", "수정불필요") and not r["reviewed_at"] and "reviewed_at" not in sets:
            sets["reviewed_at"] = today.isoformat()
    if note:
        sets["note"] = (r["note"] + "\n" if r["note"] else "") + f"[{today.month}/{today.day}] {note}"
    if sets:
        sets["updated_at"] = _now()
        conn.execute(f"UPDATE eos SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", (*sets.values(), eid))
    if status and status != r["status"] or note:
        conn.execute("INSERT INTO eo_log (eo_id, at, from_status, to_status, note, mail_id) VALUES (?,?,?,?,?,?)",
                     (eid, _now(), r["status"], sets.get("status", r["status"]), note or "", mail_id))
    if mail_id:
        link(conn, eid, mail_id, "수동")
    conn.commit()
    return conn.execute("SELECT * FROM eos WHERE id=?", (eid,)).fetchone()


def link(conn, eid: int, mail_id: int, how: str = "수동") -> bool:
    cur = conn.execute("INSERT OR IGNORE INTO eo_mails (eo_id, mail_id, how, linked_at) VALUES (?,?,?,?)",
                       (eid, mail_id, how, _now()))
    return cur.rowcount > 0


# ---------- 지연 판정 ----------
def delay_of(r, today: date | None = None) -> str:
    """현재 단계 기준으로 기한이 지났으면 '검토 회신 3일 지연' 같은 문구."""
    today = (today or date.today()).isoformat()
    st = r["status"]
    checks = []
    if st in ("접수", "검토중"):
        checks.append(("검토 회신", r["review_due"]))
    if st in ("접수", "검토중", "수정요청", "수정중"):
        checks.append(("금형 수정", r["mold_due"]))
    if st in ("접수", "검토중", "수정요청", "수정중", "수정완료", "검증중"):
        checks.append(("검증", r["verify_due"]))
    for name, due in checks:
        if due and due < today:
            n = (date.fromisoformat(today) - date.fromisoformat(due)).days
            return f"{name} {n}일 지연"
    return ""


def soon_of(r, today: date | None = None, days: int = 2) -> str:
    today = today or date.today()
    if r["status"] in CLOSED or r["status"] == "보류":
        return ""
    for name, f, sts in (("검토 회신", "review_due", ("접수", "검토중")),
                         ("금형 수정", "mold_due", ("접수", "검토중", "수정요청", "수정중")),
                         ("검증", "verify_due", ("수정완료", "검증중"))):
        if r["status"] in sts and r[f]:
            n = (date.fromisoformat(r[f]) - today).days
            if 0 <= n <= days:
                return f"{name} {'오늘' if n == 0 else f'D-{n}'}"
    return ""


def listing(conn, status: str = "open", car: str | None = None, q: str | None = None) -> list:
    ensure(conn)
    where, params = [], []
    if status == "open":
        where.append(f"status NOT IN ({', '.join('?' * len(CLOSED))})")
        params += list(CLOSED)
    elif status and status != "all":
        where.append("status = ?")
        params.append(status)
    if car:
        where.append("car LIKE ?")
        params.append(f"%{car}%")
    if q:
        where.append("(eo_no LIKE ? OR part_no LIKE ? OR part_name LIKE ? OR change_desc LIKE ? OR mold_vendor LIKE ?)")
        params += [f"%{q}%"] * 5
    sql = "SELECT * FROM eos" + (" WHERE " + " AND ".join(where) if where else "")
    order = {s: i for i, s in enumerate(STATUSES)}
    rows = conn.execute(sql, params).fetchall()
    return sorted(rows, key=lambda r: (order.get(r["status"], 99), r["received_at"] or "", r["id"]))


def new_mail_count(conn, r) -> int:
    return conn.execute("SELECT COUNT(*) FROM eo_mails WHERE eo_id=? AND mail_id > ?",
                        (r["id"], r["seen_mail_max"] or 0)).fetchone()[0]


# ---------- 누락 방지 ----------
def inbox(conn, since: str = "", limit: int = 50) -> list:
    """E.O 태그가 붙었는데 아직 어떤 E.O 항목에도 연결되지 않은 메일 (같은 건은 최신 1통)."""
    ensure(conn)
    rows = conn.execute(
        "SELECT m.* FROM mails m WHERE (',' || m.tags || ',') LIKE '%,E.O,%' AND m.sent_at >= ? "
        "AND m.is_mine = 0 AND NOT EXISTS (SELECT 1 FROM eo_mails em WHERE em.mail_id = m.id) "
        "ORDER BY m.sent_at DESC", (since or "",)).fetchall()
    seen, out = set(), []
    linked_threads = {r[0] for r in conn.execute(
        "SELECT DISTINCT m.thread_key FROM eo_mails em JOIN mails m ON m.id = em.mail_id WHERE m.thread_key != ''")}
    for r in rows:
        k = r["thread_key"] or f"#{r['id']}"
        if k in seen or k in linked_threads:
            continue
        seen.add(k)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def sync(conn) -> list[tuple[int, int, str]]:
    """등록된(진행 중) E.O 의 번호·품번이 나오는 메일, 또는 이미 연결된 메일과 같은 건(RE:/FW:)을 자동 연결."""
    ensure(conn)
    added = []
    for r in conn.execute(f"SELECT * FROM eos WHERE status NOT IN ({', '.join('?' * len(CLOSED))})", CLOSED):
        since = r["received_at"] or ""
        cands: list[tuple[int, str]] = []
        if r["eo_no"] and len(r["eo_no"]) >= 4:
            cands += [(m[0], "E.O번호") for m in conn.execute(
                "SELECT id FROM mails WHERE sent_at >= ? AND (subject LIKE ? OR body LIKE ? OR att_text LIKE ?)",
                (since, *[f"%{r['eo_no']}%"] * 3))]
        for pn in (expand_parts(r["part_no"]) or ([r["part_no"]] if r["part_no"] else [])):
            cands += [(m[0], "품번") for m in conn.execute(
                "SELECT id FROM mails WHERE sent_at >= ? AND (part_nos LIKE ? OR subject LIKE ?) "
                "AND ((',' || tags || ',') LIKE '%,E.O,%' OR (',' || tags || ',') LIKE '%,금형수정,%' "
                "OR (',' || tags || ',') LIKE '%,T/O,%' OR (',' || tags || ',') LIKE '%,검사구,%')",
                (since, f"%{pn}%", f"%{pn}%"))]
        cands += [(m[0], "같은 건") for m in conn.execute(
            "SELECT m2.id FROM eo_mails em JOIN mails m1 ON m1.id = em.mail_id "
            "JOIN mails m2 ON m2.thread_key = m1.thread_key AND m1.thread_key != '' "
            "WHERE em.eo_id = ? AND m2.sent_at >= ?", (r["id"], since))]
        for mid, how in cands:
            if link(conn, r["id"], mid, how):
                added.append((r["id"], mid, how))
    conn.commit()
    return added


def mark_seen(conn, eid: int) -> None:
    mx = conn.execute("SELECT MAX(mail_id) FROM eo_mails WHERE eo_id=?", (eid,)).fetchone()[0] or 0
    conn.execute("UPDATE eos SET seen_mail_max=? WHERE id=?", (mx, eid))
    conn.commit()


# ---------- 내보내기 / 가져오기 ----------
EXPORT_COLS = ["id", "eo_no", "car", "part_no", "part_name", "change_desc", "customer", "received_at", "status",
               "delay", "mold_vendor", "mold_action", "review_due", "reviewed_at", "ordered_at", "mold_due",
               "mold_done_at", "cf_impact", "cf_vendor", "verify_due", "verified_at", "apply_point", "cost",
               "mails", "note"]

STATUS_COLOR = {"접수": "FFF2CC", "검토중": "FFE699", "수정요청": "DDEBF7", "수정중": "BDD7EE", "수정완료": "C6E0B4",
                "검증중": "A9D08E", "완료": "D9D9D9", "수정불필요": "EDEDED", "보류": "F8CBAD", "취소": "EDEDED"}


def _export_rows(conn, rows, today=None):
    out = []
    for r in rows:
        d = {k: r[k] for k in r.keys() if k in HEADERS}
        d["delay"] = delay_of(r, today)
        mails = [str(m[0]) for m in conn.execute("SELECT mail_id FROM eo_mails WHERE eo_id=? ORDER BY mail_id",
                                                  (r["id"],))]
        d["mails"] = ", ".join("#" + m for m in mails[-10:])
        out.append(d)
    return out


def export(conn, path: str | Path, status: str = "all", today: date | None = None, with_images: bool = True) -> Path:
    rows = _export_rows(conn, listing(conn, status), today)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".csv":
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow([HEADERS[c] for c in EXPORT_COLS])
            for d in rows:
                w.writerow([d.get(c, "") for c in EXPORT_COLS])
        return path
    from . import eo_excel, eo_images
    eo_images.ensure(conn)
    return eo_excel.build(conn, listing(conn, status), path,
                          delay_of=lambda r: delay_of(r, today), soon_of=lambda r: soon_of(r, today),
                          images_of=lambda eid: eo_images.listing(conn, eid), with_images=with_images)


def import_csv(conn, path: str | Path, today: date | None = None) -> list[int]:
    """엑셀에서 'CSV UTF-8' 로 저장한 E.O 리스트를 가져온다. 열 이름은 export 와 같은 한글 이름."""
    rev = {v: k for k, v in HEADERS.items()}
    ids = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            kw = {rev[k.strip()]: (v or "").strip() for k, v in row.items() if k and k.strip() in rev}
            st = kw.pop("status", "") or "접수"
            for k in ("id", "delay", "mails"):
                kw.pop(k, None)
            if not any(kw.get(k) for k in ("eo_no", "part_no", "part_name")):
                continue
            ids.append(add(conn, today=today, status=st if st in STATUSES else "접수", **kw))
    return ids
