"""T/O 문제점·개선 이력 관리.

- 문제점 1건 = 한 부품의 한 가지 불량 (같은 T/O 에서 불량이 3가지면 3건)
- 진행: 발생 → 원인분석 → 대책수립 → 대책적용 → 효과확인 → 종결 (보류)
- 재발 감지: 같은 품번(또는 같은 품명)·같은 불량 유형의 과거 건이 있으면 '재발' + 과거 대책 표시
- 유사 사례: 불량 유형·품명·재질·부위가 비슷한 과거 사례를 점수순으로 (효과 확인된 대책 우선)
"""
from __future__ import annotations

import csv
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

from .eo import expand_parts
from .followup import parse_due
from .to_knowledge import normalize_defect

SCHEMA = """
CREATE TABLE IF NOT EXISTS to_issues (
    id             INTEGER PRIMARY KEY,
    car            TEXT DEFAULT '',
    part_no        TEXT DEFAULT '',
    part_name      TEXT DEFAULT '',
    to_stage       TEXT DEFAULT '',   -- T0, T1, T2 ... / 시사출 / P1 / M / 양산
    to_date        TEXT DEFAULT '',
    mold_vendor    TEXT DEFAULT '',
    molder         TEXT DEFAULT '',   -- 성형처 (현지 법인·협력사)
    resin          TEXT DEFAULT '',   -- 재질 (PP-TD20, ABS, PC/ABS ...)
    defect_type    TEXT DEFAULT '',   -- 표준 불량 유형 (싱크, 웰드라인, 변형 ...)
    location       TEXT DEFAULT '',   -- 발생 부위
    phenomenon     TEXT DEFAULT '',   -- 현상
    cause_cat      TEXT DEFAULT '',   -- 원인 구분 (성형조건/금형/제품설계/재질/설비/검사구/조립·공정)
    cause          TEXT DEFAULT '',
    action_cat     TEXT DEFAULT '',   -- 대책 구분 (조건변경/금형수정/설계변경(E.O)/재질변경 ...)
    countermeasure TEXT DEFAULT '',
    owner          TEXT DEFAULT '',   -- 담당 (업체/부서)
    due            TEXT DEFAULT '',   -- 대책 적용 목표일
    verify_due     TEXT DEFAULT '',   -- 효과 확인 목표일 (다음 T/O 등)
    result         TEXT DEFAULT '',   -- 효과 확인 결과
    effective      TEXT DEFAULT '',   -- 대책 효과: 유효 / 부분 / 무효
    status         TEXT DEFAULT '발생',
    applied_at     TEXT DEFAULT '',
    closed_at      TEXT DEFAULT '',
    recur_of       INTEGER,           -- 재발이면 이전 문제점 번호
    eo_id          INTEGER,           -- 설계변경으로 이어진 E.O 항목
    mail_id        INTEGER,
    note           TEXT DEFAULT '',
    created_at     TEXT,
    updated_at     TEXT,
    seen_mail_max  INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS to_mails (
    to_id INTEGER, mail_id INTEGER, how TEXT, linked_at TEXT, PRIMARY KEY (to_id, mail_id)
);
CREATE TABLE IF NOT EXISTS to_log (
    id INTEGER PRIMARY KEY, to_id INTEGER, at TEXT, from_status TEXT, to_status TEXT, note TEXT, mail_id INTEGER
);
"""

STATUSES = ["발생", "원인분석", "대책수립", "대책적용", "효과확인", "종결", "보류"]
CLOSED = ("종결",)
STAMP = {"대책적용": "applied_at", "종결": "closed_at"}
FIELDS = ["car", "part_no", "part_name", "to_stage", "to_date", "mold_vendor", "molder", "resin", "defect_type",
          "location", "phenomenon", "cause_cat", "cause", "action_cat", "countermeasure", "owner", "due",
          "verify_due", "result", "effective", "applied_at", "closed_at", "note"]
DATE_FIELDS = {"to_date", "due", "verify_due", "applied_at", "closed_at"}
HEADERS = {"id": "No", "car": "차종", "part_no": "품번", "part_name": "품명", "to_stage": "T/O차수", "to_date": "T/O일",
           "mold_vendor": "금형업체", "molder": "성형처", "resin": "재질", "defect_type": "불량유형", "location": "부위",
           "phenomenon": "현상", "cause_cat": "원인구분", "cause": "원인", "action_cat": "대책구분",
           "countermeasure": "대책", "owner": "담당", "due": "대책목표", "verify_due": "확인목표", "result": "효과확인",
           "effective": "효과", "status": "상태", "applied_at": "대책적용일", "closed_at": "종결일", "note": "메모",
           "recur_of": "재발", "eo_id": "E.O"}


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def find_recurrence(conn, part_no: str, part_name: str, defect_type: str, exclude_id: int | None = None):
    """같은 품번(LH/RH 포함) 또는 같은 품명 + 같은 불량 유형의 과거 건 (최근 것)."""
    if not defect_type:
        return None
    parts = expand_parts(part_no) or ([part_no] if part_no else [])
    rows = conn.execute("SELECT * FROM to_issues WHERE defect_type=? AND id != ? ORDER BY id DESC",
                        (defect_type, exclude_id or -1)).fetchall()
    for r in rows:
        rp = expand_parts(r["part_no"]) or ([r["part_no"]] if r["part_no"] else [])
        if (parts and set(parts) & set(rp)) or (part_name and r["part_name"] and
                                                part_name.strip().upper() == r["part_name"].strip().upper()):
            return r
    return None


def add(conn, today: date | None = None, status: str = "발생", **kw) -> tuple[int, sqlite3.Row | None]:
    """등록 → (번호, 재발이면 이전 건)"""
    ensure(conn)
    today = today or date.today()
    if status not in STATUSES:
        raise ValueError(f"상태는 {', '.join(STATUSES)} 중 하나")
    vals = {f: (kw.get(f) or "") for f in FIELDS}
    if not (vals["part_no"] or vals["part_name"]):
        raise ValueError("품번 또는 품명을 넣어 주세요")
    if not (vals["defect_type"] or vals["phenomenon"]):
        raise ValueError("불량 유형(--defect) 또는 현상(--phenomenon)을 넣어 주세요")
    vals["defect_type"] = normalize_defect(vals["defect_type"] or vals["phenomenon"])
    for f in DATE_FIELDS:
        vals[f] = parse_due(vals[f], today) if vals[f] else ""
    vals["to_date"] = vals["to_date"] or today.isoformat()
    prev = find_recurrence(conn, vals["part_no"], vals["part_name"], vals["defect_type"])
    cur = conn.execute(
        f"INSERT INTO to_issues ({', '.join(FIELDS)}, status, recur_of, eo_id, mail_id, created_at, updated_at) "
        f"VALUES ({', '.join('?' * len(FIELDS))}, ?, ?, ?, ?, ?, ?)",
        (*[vals[f] for f in FIELDS], status, prev["id"] if prev else None, kw.get("eo_id"), kw.get("mail_id"),
         _now(), _now()))
    tid = cur.lastrowid
    conn.execute("INSERT INTO to_log (to_id, at, from_status, to_status, note, mail_id) VALUES (?,?,?,?,?,?)",
                 (tid, _now(), "", status, "등록" + (f" (재발: T{prev['id']})" if prev else ""), kw.get("mail_id")))
    if kw.get("mail_id"):
        link(conn, tid, kw["mail_id"], "등록")
        conn.execute("UPDATE to_issues SET seen_mail_max=? WHERE id=?", (kw["mail_id"], tid))
    conn.commit()
    return tid, prev


def update(conn, tid: int, status: str | None = None, note: str | None = None, mail_id: int | None = None,
           today: date | None = None, **fields) -> sqlite3.Row:
    ensure(conn)
    today = today or date.today()
    r = conn.execute("SELECT * FROM to_issues WHERE id=?", (tid,)).fetchone()
    if not r:
        raise ValueError(f"T{tid} 항목이 없습니다")
    sets = {}
    for f, v in fields.items():
        if f in FIELDS and v is not None:
            if f == "defect_type":
                v = normalize_defect(v)
            sets[f] = (parse_due(v, today) if v else "") if f in DATE_FIELDS else v
    if "eo_id" in fields and fields["eo_id"] is not None:
        sets["eo_id"] = fields["eo_id"]
    if status and status != r["status"]:
        if status not in STATUSES:
            raise ValueError(f"상태는 {', '.join(STATUSES)} 중 하나")
        sets["status"] = status
        stamp = STAMP.get(status)
        if stamp and not r[stamp] and stamp not in sets:
            sets[stamp] = today.isoformat()
    if note:
        sets["note"] = (r["note"] + "\n" if r["note"] else "") + f"[{today.month}/{today.day}] {note}"
    if sets:
        sets["updated_at"] = _now()
        conn.execute(f"UPDATE to_issues SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", (*sets.values(), tid))
    if (status and status != r["status"]) or note:
        conn.execute("INSERT INTO to_log (to_id, at, from_status, to_status, note, mail_id) VALUES (?,?,?,?,?,?)",
                     (tid, _now(), r["status"], sets.get("status", r["status"]), note or "", mail_id))
    if mail_id:
        link(conn, tid, mail_id, "수동")
    conn.commit()
    return conn.execute("SELECT * FROM to_issues WHERE id=?", (tid,)).fetchone()


def link(conn, tid: int, mail_id: int, how: str = "수동") -> bool:
    cur = conn.execute("INSERT OR IGNORE INTO to_mails (to_id, mail_id, how, linked_at) VALUES (?,?,?,?)",
                       (tid, mail_id, how, _now()))
    return cur.rowcount > 0


def delay_of(r, today: date | None = None) -> str:
    today_s = (today or date.today()).isoformat()
    st = r["status"]
    checks = []
    if st in ("발생", "원인분석", "대책수립"):
        checks.append(("대책 적용", r["due"]))
    if st in ("발생", "원인분석", "대책수립", "대책적용", "효과확인"):
        checks.append(("효과 확인", r["verify_due"]))
    for name, due in checks:
        if due and due < today_s:
            return f"{name} {(date.fromisoformat(today_s) - date.fromisoformat(due)).days}일 지연"
    return ""


def soon_of(r, today: date | None = None, days: int = 2) -> str:
    today = today or date.today()
    if r["status"] in CLOSED or r["status"] == "보류":
        return ""
    for name, f, sts in (("대책 적용", "due", ("발생", "원인분석", "대책수립")),
                         ("효과 확인", "verify_due", ("대책적용", "효과확인"))):
        if r["status"] in sts and r[f]:
            n = (date.fromisoformat(r[f]) - today).days
            if 0 <= n <= days:
                return f"{name} {'오늘' if n == 0 else f'D-{n}'}"
    return ""


def listing(conn, status: str = "open", car: str | None = None, q: str | None = None,
            defect: str | None = None) -> list:
    ensure(conn)
    where, params = [], []
    if status == "open":
        where.append("status NOT IN ('종결')")
    elif status and status != "all":
        where.append("status = ?")
        params.append(status)
    if car:
        where.append("car LIKE ?")
        params.append(f"%{car}%")
    if defect:
        where.append("defect_type = ?")
        params.append(normalize_defect(defect))
    if q:
        where.append("(part_no LIKE ? OR part_name LIKE ? OR phenomenon LIKE ? OR cause LIKE ? OR countermeasure LIKE ? "
                     "OR location LIKE ? OR mold_vendor LIKE ? OR molder LIKE ?)")
        params += [f"%{q}%"] * 8
    sql = "SELECT * FROM to_issues" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id"
    return conn.execute(sql, params).fetchall()


def new_mail_count(conn, r) -> int:
    return conn.execute("SELECT COUNT(*) FROM to_mails WHERE to_id=? AND mail_id > ?",
                        (r["id"], r["seen_mail_max"] or 0)).fetchone()[0]


def mark_seen(conn, tid: int) -> None:
    mx = conn.execute("SELECT MAX(mail_id) FROM to_mails WHERE to_id=?", (tid,)).fetchone()[0] or 0
    conn.execute("UPDATE to_issues SET seen_mail_max=? WHERE id=?", (mx, tid))
    conn.commit()


# ---------- 유사 사례 ----------
_STOP = {"FR", "RR", "LH", "RH", "ASSY", "UPR", "LWR", "CTR", "NO", "INR", "OTR", "SIDE", "SUB", "COMPL"}


def _tokens(s: str) -> set[str]:
    out = set()
    for t in re.findall(r"[A-Za-z]{3,}|[가-힣]{2,}", (s or "").upper()):
        if t not in _STOP:
            out.add(t)
    return out


def similar(conn, defect: str = "", part_name: str = "", part_no: str = "", resin: str = "", location: str = "",
            phenomenon: str = "", car: str = "", exclude_id: int | None = None, limit: int = 10) -> list[tuple[int, list, sqlite3.Row]]:
    """[(점수, 이유 목록, 행)] 점수 높은 순. 대책이 있고 효과가 확인된 사례를 우대."""
    ensure(conn)
    d = normalize_defect(defect) if defect else ""
    parts = set(expand_parts(part_no))
    pn_tok, loc_tok, ph_tok = _tokens(part_name), _tokens(location), _tokens(phenomenon)
    out = []
    for r in conn.execute("SELECT * FROM to_issues WHERE id != ?", (exclude_id or -1,)):
        score, why = 0, []
        if d and r["defect_type"] == d:
            score += 5
            why.append(f"같은 불량({d})")
        rp = set(expand_parts(r["part_no"]))
        if parts and parts & rp:
            score += 4
            why.append("같은 품번")
        elif parts and {p[:5] for p in parts} & {p[:5] for p in rp}:
            score += 2
            why.append("유사 품번")
        common = pn_tok & _tokens(r["part_name"])
        if common:
            score += min(3, len(common))
            why.append("품명:" + "/".join(sorted(common)))
        if resin and r["resin"] and resin.replace(" ", "").upper() == r["resin"].replace(" ", "").upper():
            score += 2
            why.append(f"같은 재질({r['resin']})")
        lc = loc_tok & _tokens(r["location"])
        if lc:
            score += min(2, len(lc))
            why.append("부위:" + "/".join(sorted(lc)))
        pc = ph_tok & _tokens(r["phenomenon"] + " " + r["cause"])
        if pc:
            score += min(2, len(pc))
        if car and r["car"] and car.upper() in r["car"].upper():
            score += 1
        if score <= 0:
            continue
        if r["countermeasure"]:
            score += 1
        if r["effective"] == "유효" or (r["status"] == "종결" and r["result"]):
            score += 2
            why.append("효과 확인된 대책")
        elif r["effective"] == "무효":
            why.append("※ 효과 없었던 대책")
        out.append((score, why, r))
    out.sort(key=lambda x: (-x[0], -x[2]["id"]))
    return out[:limit]


# ---------- 관련 메일 ----------
def inbox(conn, since: str = "", limit: int = 50) -> list:
    """T/O·품질문제 태그 메일 중 아직 문제점에 연결 안 된 메일 (같은 건은 최신 1통)."""
    ensure(conn)
    rows = conn.execute(
        "SELECT m.* FROM mails m WHERE ((',' || m.tags || ',') LIKE '%,T/O,%') AND m.sent_at >= ? "
        "AND m.is_mine = 0 AND NOT EXISTS (SELECT 1 FROM to_mails t WHERE t.mail_id = m.id) "
        "ORDER BY m.sent_at DESC", (since or "",)).fetchall()
    linked = {r[0] for r in conn.execute(
        "SELECT DISTINCT m.thread_key FROM to_mails t JOIN mails m ON m.id = t.mail_id WHERE m.thread_key != ''")}
    seen, out = set(), []
    for r in rows:
        k = r["thread_key"] or f"#{r['id']}"
        if k in seen or k in linked:
            continue
        seen.add(k)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def sync(conn) -> list[tuple[int, int, str]]:
    """진행 중 문제점의 품번이 나오는 T/O·품질 메일, 연결된 메일의 회신을 자동 연결."""
    ensure(conn)
    added = []
    for r in conn.execute("SELECT * FROM to_issues WHERE status != '종결'"):
        since = r["to_date"] or ""
        cands = []
        for pn in expand_parts(r["part_no"]):
            cands += [(m[0], "품번") for m in conn.execute(
                "SELECT id FROM mails WHERE sent_at >= ? AND (part_nos LIKE ? OR subject LIKE ?) "
                "AND ((',' || tags || ',') LIKE '%,T/O,%' OR (',' || tags || ',') LIKE '%,품질문제,%' "
                "OR (',' || tags || ',') LIKE '%,금형수정,%')", (since, f"%{pn}%", f"%{pn}%"))]
        cands += [(m[0], "같은 건") for m in conn.execute(
            "SELECT m2.id FROM to_mails t JOIN mails m1 ON m1.id = t.mail_id "
            "JOIN mails m2 ON m2.thread_key = m1.thread_key AND m1.thread_key != '' "
            "WHERE t.to_id = ? AND m2.sent_at >= ?", (r["id"], since))]
        for mid, how in cands:
            if link(conn, r["id"], mid, how):
                added.append((r["id"], mid, how))
    conn.commit()
    return added


# ---------- CSV ----------
EXPORT_COLS = ["id", "car", "part_no", "part_name", "to_stage", "to_date", "mold_vendor", "molder", "resin",
               "defect_type", "location", "phenomenon", "cause_cat", "cause", "action_cat", "countermeasure", "owner",
               "due", "verify_due", "status", "applied_at", "result", "effective", "closed_at", "recur_of", "eo_id",
               "note"]


def export_csv(conn, path: Path, status: str = "all") -> Path:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([HEADERS[c] for c in EXPORT_COLS])
        for r in listing(conn, status):
            w.writerow([r[c] if r[c] is not None else "" for c in EXPORT_COLS])
    return path


def import_csv(conn, path, today: date | None = None) -> list[int]:
    rev = {v: k for k, v in HEADERS.items()}
    ids = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            kw = {rev[k.strip()]: (v or "").strip() for k, v in row.items() if k and k.strip() in rev}
            st = kw.pop("status", "") or "발생"
            for k in ("id", "recur_of", "eo_id"):
                kw.pop(k, None)
            if not (kw.get("part_no") or kw.get("part_name")) or not (kw.get("defect_type") or kw.get("phenomenon")):
                continue
            tid, _ = add(conn, today=today, status=st if st in STATUSES else "발생", **kw)
            ids.append(tid)
    return ids
