"""현지 금형 문제점 관리 (차종 → 외주처 → 금형 → 문제점).

외주처별 문제점 관리 양식 기준
  - 갑지(MASTER): 외주처의 금형 목록 + 유형별 건수(습합·취출·WELD LINE·SINK·미성형·GAS·기타) + STATUS
  - 을지(금형별 시트): 문제점 · 유형 · 수정 전/후 사진 · 개선 대책 · 일자 · 결과 · 금형수정 대상
진행 단계는 T/O 문제점 대장과 같다: 발생 → 원인분석 → 대책수립 → 대책적용 → 효과확인 → 종결 (보류)
갑지 STATUS = 그 금형 문제점 중 가장 앞 단계 (양식의 'STATUS 자동 산정 기준'과 같은 방식, 문제점 없으면 '-')
사진은 원본을 그대로 두고 표시(수정부위 빨강 · 리워크부위 파랑)를 비율 좌표로 겹쳐 저장한다.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from .to import STATUSES
from .to_knowledge import DEFECTS, normalize_defect

SCHEMA = """
CREATE TABLE IF NOT EXISTS lm_molds (
    id          INTEGER PRIMARY KEY,
    car         TEXT DEFAULT '',
    vendor      TEXT DEFAULT '',   -- 외주처 (현지 성형처)
    part_no     TEXT DEFAULT '',
    part_name   TEXT DEFAULT '',
    item        TEXT DEFAULT '',   -- BUMPER / COCKPIT / CONSOLE / MOVING ...
    mold_type   TEXT DEFAULT '',   -- INJECTION / PRESS / GMT ...
    mold_maker  TEXT DEFAULT '',   -- 금형 제작 업체 (KR 업체)
    weight      TEXT DEFAULT '',
    to_round    TEXT DEFAULT '',   -- T/O 현황 (1st, 2nd ...)
    owner       TEXT DEFAULT '',   -- Responsible
    note        TEXT DEFAULT '',
    src         TEXT DEFAULT '',
    created_at  TEXT,
    updated_at  TEXT,
    UNIQUE (car, vendor, part_no)
);
CREATE TABLE IF NOT EXISTS lm_issues (
    id             INTEGER PRIMARY KEY,
    mold_id        INTEGER,
    car            TEXT DEFAULT '',
    vendor         TEXT DEFAULT '',
    part_no        TEXT DEFAULT '',
    part_name      TEXT DEFAULT '',
    category       TEXT DEFAULT '기타',  -- 양식 유형: 습합/취출/WELD LINE/SINK/미성형/GAS/기타
    defect_type    TEXT DEFAULT '',     -- T/O 대장 표준 불량 유형 (가이드·유사 사례용)
    problem        TEXT DEFAULT '',     -- 발생 문제점 (PROBLEM)
    countermeasure TEXT DEFAULT '',     -- 수리/개선 계획 (MOLD IMPROVEMENT)
    owner          TEXT DEFAULT '',     -- 담당 (사람·업체)
    importance     TEXT DEFAULT '',     -- 상/중/하
    mold_fix       TEXT DEFAULT '',     -- 금형수정 대상 O/X
    occ_date       TEXT DEFAULT '',     -- 발생일
    due            TEXT DEFAULT '',     -- 수정 목표일 (수정 진행 일정)
    to_after       TEXT DEFAULT '',     -- 수정 후 T/O 실시 O/X
    to_result      TEXT DEFAULT '',     -- 수정 후 T/O 결과 OK/NG
    progress       TEXT DEFAULT '',     -- 진행 메모 (예: 금형 수정 완료, T/O 요청 필요)
    status         TEXT DEFAULT '발생',
    src_status     TEXT DEFAULT '',     -- 가져온 양식의 원래 상태 (개선 중 · 검토 중 ...)
    remark         TEXT DEFAULT '',
    note           TEXT DEFAULT '',
    recur_of       INTEGER,
    src_key        TEXT,
    edited         INTEGER DEFAULT 0,   -- 앱에서 고친 건은 다시 가져와도 덮어쓰지 않음
    closed_at      TEXT DEFAULT '',
    created_at     TEXT,
    updated_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_lm_issues_mold ON lm_issues(mold_id);
CREATE INDEX IF NOT EXISTS idx_lm_issues_key ON lm_issues(src_key);
CREATE TABLE IF NOT EXISTS lm_log (
    id INTEGER PRIMARY KEY, issue_id INTEGER, at TEXT, from_status TEXT, to_status TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS lm_mails (
    issue_id INTEGER, mail_id INTEGER, how TEXT, linked_at TEXT, PRIMARY KEY (issue_id, mail_id)
);
"""

CATS = ["습합", "취출", "WELD LINE", "SINK", "미성형", "GAS", "기타"]
IMPORTANCE = ["상", "중", "하"]
OPEN_STAGES = [s for s in STATUSES if s not in ("종결", "보류")]
MOLD_FIELDS = ["part_name", "item", "mold_type", "mold_maker", "weight", "to_round", "owner", "note"]
ISSUE_FIELDS = ["category", "defect_type", "problem", "countermeasure", "owner", "importance", "mold_fix", "occ_date",
                "due", "to_after", "to_result", "progress", "remark", "status"]
DATE_FIELDS = {"occ_date", "due"}
PHOTO_GROUPS = {"before": "수정 전", "after": "수정 후"}
_CAT_FROM_DEFECT = {"버": "습합", "단차·갭": "습합", "취출·이젝터": "취출", "웰드라인": "WELD LINE", "싱크": "SINK",
                    "미성형": "미성형", "가스·탄화": "GAS"}


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    from . import eo_images
    eo_images.ensure(conn)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------- 값 정리 ----------------
def fmt_part_no(s) -> str:
    """'86511BNGA0' → '86511-BNGA0' (형식이 맞을 때만), 나머지는 대문자·공백 정리."""
    t = re.sub(r"\s+", " ", str(s or "")).strip().upper()
    k = re.sub(r"[^0-9A-Z]", "", t)
    if re.fullmatch(r"\d{3}[0-9A-Z]{2}[A-Z0-9]{5}", k):
        return k[:5] + "-" + k[5:]
    return t


def is_part_no(s) -> bool:
    """86511-BNGA0, 847D2-BN200, 8471A-BN010, 83370-BNAA0-INJ ..."""
    t = str(s or "").strip().upper()
    return not t.startswith("=") and bool(re.search(r"\d{3}[0-9A-Z]{2}-?[0-9A-Z]{3,}", t))


def vendor_from_name(name: str) -> str:
    """'BC4t CUV_CAR_NAPLAS_REV.1 (매크로).xlsx' → 'NAPLAS'."""
    m = re.search(r"_CAR_\s*(.+?)\s*_REV", name or "", re.I)
    return m.group(1).strip().upper() if m else ""


def match_vendor(conn, car: str, vendor: str) -> str:
    """양식 제목의 오타(예: ASAH-5 ↔ ASHA-5)는 이미 있는 외주처 이름으로 맞춘다."""
    import difflib
    v = (vendor or "").strip().upper()
    if not v:
        return v
    have = [r[0] for r in conn.execute("SELECT DISTINCT vendor FROM lm_molds WHERE car=? UNION "
                                       "SELECT DISTINCT vendor FROM lm_issues WHERE car=?", (car, car)) if r[0]]
    if v in have:
        return v
    key = lambda x: re.sub(r"[^0-9A-Z가-힣]", "", x)
    for h in have:
        if sorted(key(h)) == sorted(key(v)) or (re.sub(r"\D", "", h) == re.sub(r"\D", "", v) and
                                                difflib.SequenceMatcher(None, key(h), key(v)).ratio() >= 0.8):
            return h
    return v


def norm_category(cat: str, problem: str = "") -> str:
    c = re.sub(r"\s+", " ", str(cat or "")).strip().upper()
    for name in CATS:
        if c == name.upper():
            return name
    if c:
        for keys, name in ((("WELD", "웰드"), "WELD LINE"), (("SINK", "싱크"), "SINK"), (("GAS", "가스"), "GAS"),
                           (("습합", "BURR"), "습합"), (("취출",), "취출"), (("미성형", "SHORT"), "미성형")):
            if any(k in c for k in keys):
                return name
        if c in ("기타", "ETC", "OTHERS"):
            return "기타"
    d = normalize_defect(problem) if problem else ""
    return _CAT_FROM_DEFECT.get(d, "기타")


def yes_no(v) -> str:
    t = str(v or "").strip().upper()
    return "O" if t in ("O", "Y", "YES", "○", "●", "V", "1") else ("X" if t in ("X", "N", "NO", "×", "0") else "")


def ok_ng(v) -> str:
    t = str(v or "").strip().upper()
    return "OK" if t.startswith("OK") or t == "합격" else ("NG" if t.startswith("NG") or t == "불합격" else "")


def norm_importance(v) -> str:
    t = str(v or "").strip()
    return t if t in IMPORTANCE else {"H": "상", "HIGH": "상", "M": "중", "MID": "중", "L": "하", "LOW": "하"}.get(t.upper(), "")


def parse_date(v, today: date | None = None) -> str:
    """'07월 03일', '7/29', '2026. 7. 18', '2026-07-08 00:00:00', datetime → 'YYYY-MM-DD'. 연도 없으면 올해
    (단, 오늘보다 한 달 넘게 뒤면 작년). 못 읽으면 ''."""
    today = today or date.today()
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v or "").strip()
    if not s:
        return ""
    m = re.search(r"(20\d\d)\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", s) or re.search(r"(?<!\d)(\d{1,2})\s*[/.]\s*(\d{1,2})(?!\d)", s)
        if not m:
            return ""
        y, mo, d = today.year, int(m.group(1)), int(m.group(2))
    try:
        dt = date(y, mo, d)
    except ValueError:
        return ""
    if not re.search(r"20\d\d", s) and dt > today + timedelta(days=31):
        dt = dt.replace(year=dt.year - 1)
    return dt.isoformat()


def map_status(src: str, to_result: str = "", progress: str = "", plan: str = "") -> str:
    """양식 상태(검토 중/개선 중/AQL 협의/관망/개선 완) → T/O 대장 단계.
    수정 후 T/O 가 NG 면 대책 재수립, '개선 완'이라도 T/O OK 가 없으면 효과확인(검증 T/O 필요)."""
    s = re.sub(r"\s+", "", str(src or ""))
    if s in STATUSES:
        return s
    fixed = bool(re.search(r"수정\s*완료", progress or ""))
    if to_result == "NG" and s != "관망":
        return "대책수립"
    if s == "검토중":
        return "원인분석"
    if s == "개선중":
        return "대책적용" if fixed else "대책수립"
    if s in ("AQL협의", "AQL"):
        return "효과확인"
    if s == "관망":
        return "보류"
    if s in ("개선완", "개선완료", "완료"):
        return "종결" if to_result == "OK" else "효과확인"
    if fixed:
        return "대책적용"
    return "대책수립" if plan else "발생"


def mold_status(statuses: list[str]) -> str:
    """금형 STATUS = 문제점 중 가장 앞 단계. 진행 중이 없으면 보류 > 종결, 문제점 없으면 '-'."""
    order = OPEN_STAGES + ["보류", "종결"]
    have = [s for s in statuses if s in order]
    return min(have, key=order.index) if have else "-"


def delay_of(r, today: date | None = None) -> str:
    today_s = (today or date.today()).isoformat()
    if r["status"] in ("발생", "원인분석", "대책수립") and r["due"] and r["due"] < today_s:
        return f"수정 {(date.fromisoformat(today_s) - date.fromisoformat(r['due'])).days}일 지연"
    return ""


def wait_check(r) -> str:
    """금형 수정은 끝났는데 검증 T/O 결과가 없는 건."""
    if r["status"] in ("대책적용", "효과확인") and not r["to_result"]:
        return "검증 T/O 필요"
    return ""


# ---------------- 금형 ----------------
def upsert_mold(conn, car: str, vendor: str, part_no: str, src: str = "", overwrite: bool = True, **kw) -> tuple[int, bool]:
    """(번호, 새로 만들었는지). 빈 값은 기존 값을 지우지 않는다."""
    ensure(conn)
    car, vendor, part_no = (car or "").strip(), (vendor or "").strip().upper(), fmt_part_no(part_no)
    if not (car and part_no):
        raise ValueError("차종과 품번이 필요합니다")
    row = conn.execute("SELECT * FROM lm_molds WHERE car=? AND vendor=? AND part_no=?", (car, vendor, part_no)).fetchone()
    vals = {f: str(kw.get(f) or "").strip() for f in MOLD_FIELDS if kw.get(f) not in (None, "")}
    if row:
        sets = {k: v for k, v in vals.items() if overwrite or not row[k]}
        if sets:
            sets["updated_at"] = _now()
            conn.execute(f"UPDATE lm_molds SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", (*sets.values(), row["id"]))
        return row["id"], False
    cur = conn.execute(
        f"INSERT INTO lm_molds (car, vendor, part_no, {', '.join(MOLD_FIELDS)}, src, created_at, updated_at) "
        f"VALUES (?,?,?,{','.join('?' * len(MOLD_FIELDS))},?,?,?)",
        (car, vendor, part_no, *[vals.get(f, "") for f in MOLD_FIELDS], src, _now(), _now()))
    return cur.lastrowid, True


def get_mold(conn, mid: int):
    return conn.execute("SELECT * FROM lm_molds WHERE id=?", (mid,)).fetchone()


def molds(conn, car: str | None = None, vendor: str | None = None) -> list[dict]:
    """갑지: 금형별 유형 건수 · TOTAL · 진행 중 · STATUS · 최근 발생일."""
    ensure(conn)
    sql, args = "SELECT * FROM lm_molds WHERE 1=1", []
    if car:
        sql += " AND car=?"
        args.append(car)
    if vendor:
        sql += " AND vendor=?"
        args.append(vendor.upper())
    rows = conn.execute(sql + " ORDER BY car, vendor, id", args).fetchall()
    iss: dict[int, list] = {}
    q2 = "SELECT mold_id, category, status, occ_date, mold_fix, importance, due, to_result FROM lm_issues"
    for r in conn.execute(q2):
        iss.setdefault(r["mold_id"], []).append(r)
    out = []
    for m in rows:
        li = iss.get(m["id"], [])
        cnt = {c: 0 for c in CATS}
        for r in li:
            cnt[r["category"] if r["category"] in cnt else "기타"] += 1
        opened = [r for r in li if r["status"] not in ("종결", "보류")]
        out.append(dict(m) | {
            "counts": cnt, "total": len(li), "open": len(opened),
            "hi_open": sum(1 for r in opened if r["importance"] == "상"),
            "fix_open": sum(1 for r in opened if r["mold_fix"] == "O"),
            "late": sum(1 for r in li if delay_of(r)),
            "wait": sum(1 for r in li if wait_check(r)),
            "status": mold_status([r["status"] for r in li]),
            "last": max([r["occ_date"] for r in li if r["occ_date"]] or [""]),
        })
    return out


def cars(conn) -> list[dict]:
    """차종별 요약 (금형·외주처·문제점·진행·중요도 상 진행·지연·검증 대기·종결률)."""
    ensure(conn)
    out = []
    for (car,) in conn.execute("SELECT DISTINCT car FROM lm_molds UNION SELECT DISTINCT car FROM lm_issues ORDER BY 1"):
        ms = molds(conn, car)
        vendors: dict[str, dict] = {}
        for m in ms:
            v = vendors.setdefault(m["vendor"], {"vendor": m["vendor"], "molds": 0, "issues": 0, "open": 0, "hi_open": 0})
            v["molds"] += 1
            v["issues"] += m["total"]
            v["open"] += m["open"]
            v["hi_open"] += m["hi_open"]
        total = sum(m["total"] for m in ms)
        closed = conn.execute("SELECT COUNT(*) FROM lm_issues WHERE car=? AND status='종결'", (car,)).fetchone()[0]
        out.append({"car": car, "molds": len(ms), "molds_with_issue": sum(1 for m in ms if m["total"]),
                    "issues": total, "open": sum(m["open"] for m in ms), "hi_open": sum(m["hi_open"] for m in ms),
                    "late": sum(m["late"] for m in ms), "wait": sum(m["wait"] for m in ms),
                    "closed": closed, "close_rate": round(closed / total * 100) if total else 0,
                    "vendors": sorted(vendors.values(), key=lambda v: v["vendor"])})
    return out


# ---------------- 문제점 ----------------
def _find_recur(conn, mold_id: int, category: str, defect_type: str, occ_date: str, exclude: int | None = None):
    """같은 금형 · 같은 유형(기타면 같은 표준 불량)의 더 이른 문제점."""
    if not occ_date:
        return None
    if category != "기타":
        cond, arg = "category=?", category
    elif defect_type in DEFECTS:
        cond, arg = "defect_type=?", defect_type
    else:
        return None
    return conn.execute(f"SELECT id FROM lm_issues WHERE mold_id=? AND {cond} AND occ_date != '' AND occ_date < ? "
                        "AND id != ? ORDER BY occ_date DESC, id DESC LIMIT 1",
                        (mold_id, arg, occ_date, exclude or -1)).fetchone()


def _log(conn, iid: int, frm: str, to: str, note: str = "") -> None:
    conn.execute("INSERT INTO lm_log (issue_id, at, from_status, to_status, note) VALUES (?,?,?,?,?)",
                 (iid, _now(), frm, to, note))


def _clean_issue(kw: dict, today: date | None = None) -> dict:
    v = {}
    for f in ISSUE_FIELDS:
        if f not in kw or kw[f] is None:
            continue
        x = kw[f]
        if f in DATE_FIELDS:
            x = parse_date(x, today)
        elif f == "category":
            x = norm_category(x, kw.get("problem", ""))
        elif f == "importance":
            x = norm_importance(x)
        elif f in ("mold_fix", "to_after"):
            x = yes_no(x)
        elif f == "to_result":
            x = ok_ng(x)
        elif f == "status":
            x = str(x).strip()
            if x not in STATUSES:
                raise ValueError(f"상태는 {', '.join(STATUSES)} 중 하나")
        else:
            x = str(x).strip()
        v[f] = x
    return v


def add_issue(conn, mold_id: int, today: date | None = None, src_key: str | None = None, src_status: str = "",
              commit: bool = True, **kw) -> int:
    ensure(conn)
    m = get_mold(conn, mold_id)
    if not m:
        raise ValueError(f"금형 {mold_id} 이 없습니다")
    v = _clean_issue(kw, today)
    if not v.get("problem"):
        raise ValueError("문제점을 입력해 주세요")
    v.setdefault("category", norm_category("", v["problem"]))
    v["defect_type"] = v.get("defect_type") or normalize_defect(v["problem"])
    v.setdefault("status", "발생")
    v.setdefault("occ_date", (today or date.today()).isoformat())
    prev = _find_recur(conn, mold_id, v["category"], v["defect_type"], v["occ_date"])
    cols = ["mold_id", "car", "vendor", "part_no", "part_name", *v.keys(), "src_status", "recur_of", "src_key",
            "closed_at", "created_at", "updated_at"]
    vals = [mold_id, m["car"], m["vendor"], m["part_no"], m["part_name"], *v.values(), src_status,
            prev["id"] if prev else None, src_key, (today or date.today()).isoformat() if v["status"] == "종결" else "",
            _now(), _now()]
    cur = conn.execute(f"INSERT INTO lm_issues ({', '.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
    iid = cur.lastrowid
    _log(conn, iid, "", v["status"], "등록" + (f" (재발: L{prev['id']})" if prev else "") + (f" · 원래 상태 {src_status}" if src_status else ""))
    if commit:
        conn.commit()
    return iid


def get_issue(conn, iid: int):
    ensure(conn)
    return conn.execute("SELECT * FROM lm_issues WHERE id=?", (iid,)).fetchone()


def update_issue(conn, iid: int, note: str | None = None, today: date | None = None, by_app: bool = True, **kw):
    ensure(conn)
    today = today or date.today()
    r = get_issue(conn, iid)
    if not r:
        raise ValueError(f"L{iid} 문제점이 없습니다")
    v = {k: x for k, x in _clean_issue(kw, today).items() if x != r[k]}
    if "problem" in v and "defect_type" not in kw:
        v["defect_type"] = normalize_defect(v["problem"])
    st = v.get("status")
    if st == "종결" and (v.get("to_result", r["to_result"]) == "NG"):
        raise ValueError("수정 후 T/O 결과가 NG 입니다. 대책을 다시 세우거나 결과를 고친 뒤 종결하세요")
    if st:
        v["closed_at"] = today.isoformat() if st == "종결" else ""
    if note:
        v["note"] = (r["note"] + "\n" if r["note"] else "") + f"[{today.month}/{today.day}] {note.strip()}"
    if v:
        v["updated_at"] = _now()
        if by_app:
            v["edited"] = 1
        conn.execute(f"UPDATE lm_issues SET {', '.join(f'{k}=?' for k in v)} WHERE id=?", (*v.values(), iid))
        if ("category" in v or "occ_date" in v or "defect_type" in v):
            n = get_issue(conn, iid)
            prev = _find_recur(conn, n["mold_id"], n["category"], n["defect_type"], n["occ_date"], exclude=iid)
            conn.execute("UPDATE lm_issues SET recur_of=? WHERE id=?", (prev["id"] if prev else None, iid))
    if st or note:
        _log(conn, iid, r["status"], st or r["status"], (note or "").strip())
    conn.commit()
    return get_issue(conn, iid)


def delete_issue(conn, cfg: dict, iid: int) -> None:
    for p in photos(conn, iid):
        remove_photo(conn, p["id"])
    conn.execute("DELETE FROM lm_issues WHERE id=?", (iid,))
    conn.execute("DELETE FROM lm_log WHERE issue_id=?", (iid,))
    conn.commit()


def issues(conn, car: str | None = None, vendor: str | None = None, mold_id: int | None = None, status: str = "all",
           q: str | None = None, importance: str | None = None, category: str | None = None) -> list:
    ensure(conn)
    sql, a = "SELECT * FROM lm_issues WHERE 1=1", []
    for col, val in (("car", car), ("vendor", (vendor or "").upper() or None), ("mold_id", mold_id),
                     ("importance", importance), ("category", category)):
        if val:
            sql += f" AND {col}=?"
            a.append(val)
    if status == "open":
        sql += " AND status NOT IN ('종결', '보류')"
    elif status == "wait":
        sql += " AND status IN ('대책적용', '효과확인') AND to_result=''"
    elif status and status != "all":
        sql += " AND status=?"
        a.append(status)
    if q:
        sql += " AND (part_no LIKE ? OR part_name LIKE ? OR problem LIKE ? OR countermeasure LIKE ? OR vendor LIKE ? OR note LIKE ?)"
        a += [f"%{q}%"] * 6
    order = "CASE importance WHEN '상' THEN 0 WHEN '중' THEN 1 WHEN '하' THEN 2 ELSE 3 END"
    return conn.execute(sql + f" ORDER BY vendor, part_no, occ_date, id", a).fetchall() if mold_id else \
        conn.execute(sql + f" ORDER BY {order}, occ_date DESC, id DESC", a).fetchall()


def log(conn, iid: int) -> list:
    return conn.execute("SELECT * FROM lm_log WHERE issue_id=? ORDER BY id", (iid,)).fetchall()


# ---------------- 사진 (수정 전/후 + 표시) ----------------
def _img_dir(cfg: dict, iid: int) -> Path:
    from .eo_images import img_dir
    return img_dir(cfg, iid, "lm")


def _to_jpeg(data: bytes, max_side: int = 1800) -> tuple[bytes, int, int]:
    from PIL import Image, ImageOps
    im = Image.open(io.BytesIO(data))
    im = ImageOps.exif_transpose(im)
    if im.mode != "RGB":
        bg = Image.new("RGB", im.size, "white")
        try:
            bg.paste(im, mask=im.convert("RGBA").split()[-1])
        except Exception:
            bg.paste(im.convert("RGB"))
        im = bg
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=88, optimize=True)
    return b.getvalue(), im.width, im.height


def save_photo(conn, cfg: dict, iid: int, data: bytes, grp: str = "before", marks: list | None = None,
               flat: bytes | None = None, source: str = "upload", caption: str = "", commit: bool = True) -> int | None:
    """사진 저장 (JPEG, 긴 변 1800). 같은 사진은 한 번만. marks 가 있으면 표시를 그려 넣은 사본(flat)도 저장."""
    ensure(conn)
    if grp not in PHOTO_GROUPS:
        raise ValueError("사진 칸은 before(수정 전) / after(수정 후)")
    if not get_issue(conn, iid):
        raise ValueError(f"L{iid} 문제점이 없습니다")
    sha = hashlib.sha1(data).hexdigest()
    if conn.execute("SELECT 1 FROM eo_images WHERE eo_id=? AND kind='lm' AND sha1=?", (iid, sha)).fetchone():
        return None
    jpg, w, h = _to_jpeg(data)
    d = _img_dir(cfg, iid)
    d.mkdir(parents=True, exist_ok=True)
    n = (conn.execute("SELECT COALESCE(MAX(ABS(sort)), 0) FROM eo_images WHERE eo_id=? AND kind='lm'", (iid,)).fetchone()[0] or 0) + 1
    p = d / f"{n:02d}_{grp}.jpg"
    p.write_bytes(jpg)
    cur = conn.execute("INSERT INTO eo_images (eo_id, kind, path, source, caption, sha1, sort, grp, marks, w, h, created_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (iid, "lm", str(p.resolve()), source, caption or PHOTO_GROUPS[grp], sha, n, grp, "", w, h, _now()))
    pid = cur.lastrowid
    if marks:
        set_marks(conn, cfg, pid, marks, flat, commit=False)
    if commit:
        conn.commit()
    return pid


def set_marks(conn, cfg: dict, pid: int, marks: list, flat: bytes | None = None, commit: bool = True) -> None:
    """표시 저장. flat = 표시를 그려 넣은 그림 (엑셀·메일용). 표시를 모두 지우면 사본도 지운다."""
    r = conn.execute("SELECT * FROM eo_images WHERE id=? AND kind='lm'", (pid,)).fetchone()
    if not r:
        raise ValueError("사진이 없습니다")
    marks = [m for m in (marks or []) if isinstance(m, dict) and m.get("t") in ("ellipse", "rect", "arrow", "pen")][:60]
    old = r["marked_path"]
    path = ""
    if marks and flat:
        jpg, _, _ = _to_jpeg(flat)
        p = Path(r["path"]).with_name(Path(r["path"]).stem + "_mark.jpg")
        p.write_bytes(jpg)
        path = str(p.resolve())
    elif marks and old:
        path = old
    if old and old != path:
        Path(old).unlink(missing_ok=True)
    conn.execute("UPDATE eo_images SET marks=?, marked_path=? WHERE id=?",
                 (json.dumps(marks, ensure_ascii=False) if marks else "", path, pid))
    if commit:
        conn.commit()


def move_photo(conn, pid: int, step: int) -> None:
    r = conn.execute("SELECT * FROM eo_images WHERE id=? AND kind='lm'", (pid,)).fetchone()
    if not r:
        raise ValueError("사진이 없습니다")
    sibs = conn.execute("SELECT id, sort FROM eo_images WHERE eo_id=? AND kind='lm' AND grp=? ORDER BY sort, id",
                        (r["eo_id"], r["grp"])).fetchall()
    ids = [s["id"] for s in sibs]
    i = ids.index(pid)
    j = i + (1 if step > 0 else -1)
    if 0 <= j < len(ids):
        a, b = sibs[i], sibs[j]
        conn.execute("UPDATE eo_images SET sort=? WHERE id=?", (b["sort"], a["id"]))
        conn.execute("UPDATE eo_images SET sort=? WHERE id=?", (a["sort"], b["id"]))
        conn.commit()


def remove_photo(conn, pid: int) -> None:
    r = conn.execute("SELECT * FROM eo_images WHERE id=? AND kind='lm'", (pid,)).fetchone()
    if not r:
        return
    for p in (r["path"], r["marked_path"]):
        if p:
            Path(p).unlink(missing_ok=True)
    conn.execute("DELETE FROM eo_images WHERE id=?", (pid,))
    conn.commit()


def photos(conn, iid: int, grp: str | None = None) -> list:
    ensure(conn)
    sql, a = "SELECT * FROM eo_images WHERE eo_id=? AND kind='lm'", [iid]
    if grp:
        sql += " AND grp=?"
        a.append(grp)
    return conn.execute(sql + " ORDER BY grp, sort, id", a).fetchall()


def first_photos(conn, ids: list[int]) -> dict[int, sqlite3.Row]:
    """카드 썸네일: 문제점마다 수정 전 첫 장 (없으면 수정 후)."""
    out: dict[int, sqlite3.Row] = {}
    if not ids:
        return out
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in conn.execute(f"SELECT * FROM eo_images WHERE kind='lm' AND eo_id IN ({','.join('?' * len(chunk))}) "
                              "ORDER BY CASE grp WHEN 'before' THEN 0 ELSE 1 END, sort, id", chunk):
            out.setdefault(r["eo_id"], r)
    return out


def photo_counts(conn) -> dict[int, int]:
    return {r[0]: r[1] for r in conn.execute("SELECT eo_id, COUNT(*) FROM eo_images WHERE kind='lm' GROUP BY eo_id")}


# ---------------- 표 읽기 (엑셀 · CSV) ----------------
def _hkey(h) -> str:
    t = str(h or "").lower()
    t = re.sub(r"\(.*?\)", "", t)          # (엑셀탭 생성) (Problem) 같은 설명 제거
    return re.sub(r"[\s_]+", "", t)


MOLD_HEADERS = {
    "vendor": ["외주처", "성형처", "vendor", "supplier"],
    "part_no": ["partno", "품번"],
    "part_name": ["partname", "품명"],
    "item": ["item", "아이템", "구분"],
    "mold_type": ["moldtype", "성형방식", "금형구분"],
    "mold_maker": ["kr업체", "금형제작업체", "금형제작사", "금형업체", "moldmaker"],
    "weight": ["중량", "weight"],
    "to_round": ["t/o현황", "t/o차수", "t/o", "to현황"],
    "owner": ["responsible", "담당", "담당자"],
    "note": ["비고", "remark", "note"],
}
ISSUE_HEADERS = {
    "vendor": ["외주처", "성형처", "vendor"],
    "part_no": ["품번", "partno"],
    "part_name": ["품명", "partname"],
    "mold_maker": ["금형제작업체", "금형제작사", "금형업체", "kr업체"],
    "category": ["카테고리", "category", "유형"],
    "problem": ["발생문제점", "문제점", "problem"],
    "countermeasure": ["수리/개선계획", "개선계획", "개선대책", "moldimprovement", "대책"],
    "occ_date": ["발생일정", "발생일", "date"],
    "due": ["수정진행일정", "수정일정", "수정목표일"],
    "progress": ["작업자이름", "진행", "진행현황"],
    "importance": ["중요도"],
    "to_after": ["수정후t/o현황", "수정후t/o"],
    "to_result": ["t/o결과"],
    "status": ["현재상태", "status", "result", "상태"],
    "mold_fix": ["금형수정대상"],
    "remark": ["remark", "비고"],
}


def _map_header(row: list, spec: dict) -> dict[str, int]:
    keys = [_hkey(h) for h in row]
    out: dict[str, int] = {}
    for field, names in spec.items():
        for nm in names:            # 이름 순서 = 우선순위 (정확히 같은 것 먼저, 그다음 앞부분이 같은 것)
            hit = next((i for i, k in enumerate(keys) if k == nm and i not in out.values()), None)
            if hit is None:
                hit = next((i for i, k in enumerate(keys) if k.startswith(nm) and i not in out.values()), None)
            if hit is not None:
                out[field] = hit
                break
    return out


def _find_header(rows: list[list], spec: dict, must: str) -> tuple[int, dict]:
    for i, row in enumerate(rows[:15]):
        cols = _map_header(row, spec)
        if must in cols and len(cols) >= 3:
            return i, cols
    raise ValueError(f"머리글을 찾지 못했습니다 ({'/'.join(spec[must])} 열 필요)")


def read_rows(name: str, data: bytes, sheet: str | None = None) -> list[list]:
    """엑셀(.xlsx/.xlsm)은 첫 시트(또는 sheet), CSV 는 UTF-8/CP949."""
    if name.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        ws = wb[sheet] if sheet else wb.worksheets[0]
        return [list(r) for r in ws.iter_rows(values_only=True)]
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("CSV 글자 인코딩을 읽지 못했습니다 (UTF-8 또는 CP949)")
    return [r for r in csv.reader(io.StringIO(text))]


def _cell(row: list, i: int | None):
    if i is None or i >= len(row) or row[i] is None:
        return ""
    v = row[i]
    return v if isinstance(v, (date, datetime)) else str(v).strip()


def _title_vendor(rows: list[list]) -> str:
    for row in rows[:2]:
        for v in row:
            m = re.search(r"ISSUE LIST\s*[—\-:]+\s*(.+)$", str(v or ""), re.I)
            if m:
                return m.group(1).strip().upper()
    return ""


def _weight(text: str) -> str:
    m = re.search(r"(\d[\d.,]*(?:\s*~\s*\d[\d.,]*)?)\s*g\b", text or "", re.I)
    return (m.group(1).replace(" ", "") + "g") if m else ""


def import_molds(conn, car: str, rows: list[list], vendor: str | None = None, src: str = "") -> dict:
    """금형 리스트(엑셀) 또는 외주처 갑지(MASTER) → 금형 등록·갱신. 바뀐 칸만 고친다."""
    ensure(conn)
    hi, cols = _find_header(rows, MOLD_HEADERS, "part_no")
    vendor_default = (vendor or "").upper() or match_vendor(conn, car, _title_vendor(rows))
    if "vendor" not in cols and not vendor_default:
        raise ValueError("외주처 열이 없습니다. --vendor 로 외주처를 지정해 주세요")
    alt_pn = [i for i, h in enumerate(rows[hi]) if _hkey(h).startswith("partno") and i != cols["part_no"]]
    stats = {"new": 0, "updated": 0, "skipped": 0}
    for row in rows[hi + 1:]:
        pn = str(_cell(row, cols["part_no"]))
        if not is_part_no(pn):        # 품번이 비면 하이퍼링크 열 값으로
            pn = next((str(_cell(row, i)) for i in alt_pn if is_part_no(_cell(row, i))), "")
        if not is_part_no(pn):
            stats["skipped"] += 1 if sum(1 for x in row if str(x or "").strip() not in ("", "-")) > 1 else 0
            continue
        v = (str(_cell(row, cols.get("vendor"))) or vendor_default).upper()
        kw = {f: str(_cell(row, cols.get(f))) for f in MOLD_FIELDS if f in cols}
        if "weight" not in cols:
            w = _weight(" ".join(str(x or "") for x in row[cols["part_no"] + 1:]))
            if w:
                kw["weight"] = w
        _, new = upsert_mold(conn, car, v, pn, src=src, **kw)
        stats["new" if new else "updated"] += 1
    conn.commit()
    return stats


def _key(car, vendor, part_no, problem, occ, n) -> str:
    p = re.sub(r"\s+", " ", str(problem or "")).strip().lower()
    return hashlib.sha1(f"{car}|{vendor}|{part_no}|{p}|{occ}|{n}".encode("utf-8")).hexdigest()[:20]


def _upsert_issue(conn, car: str, vendor: str, pn: str, mold_kw: dict, kw: dict, src_status: str, seen: dict,
                  today: date | None, src: str) -> tuple[int | None, str]:
    """('new'|'updated'|'kept') — 앱에서 고친 건(edited)은 그대로 둔다."""
    mid, _ = upsert_mold(conn, car, vendor, pn, src=src, overwrite=False, **mold_kw)
    occ = parse_date(kw.get("occ_date"), today)
    base = (car, vendor, fmt_part_no(pn), kw.get("problem", ""), occ)
    seen[base] = seen.get(base, 0) + 1
    key = _key(*base, seen[base])
    row = conn.execute("SELECT * FROM lm_issues WHERE src_key=?", (key,)).fetchone()
    kw = dict(kw)
    # 을지처럼 진행·T/O 결과 열이 없는 양식이면 이미 가져온 값으로 단계를 정한다 (단계가 거꾸로 가지 않게)
    have = lambda f: kw[f] if f in kw else (row[f] if row else "")
    kw["status"] = map_status(src_status, ok_ng(have("to_result")), str(have("progress")), str(have("countermeasure")))
    if not row:
        return add_issue(conn, mid, today=today, src_key=key, src_status=src_status, commit=False, **kw), "new"
    if row["edited"]:
        return row["id"], "kept"
    v = {k: x for k, x in _clean_issue(kw, today).items() if x != row[k]}
    if src_status != row["src_status"]:
        v["src_status"] = src_status
    if v:
        if "status" in v:
            _log(conn, row["id"], row["status"], v["status"], f"가져오기 · 원래 상태 {src_status}")
            v["closed_at"] = (today or date.today()).isoformat() if v["status"] == "종결" else ""
        v["updated_at"] = _now()
        conn.execute(f"UPDATE lm_issues SET {', '.join(f'{k}=?' for k in v)} WHERE id=?", (*v.values(), row["id"]))
        return row["id"], "updated"
    return row["id"], "same"


def import_issues(conn, car: str, rows: list[list], vendor: str | None = None, today: date | None = None,
                  src: str = "") -> dict:
    """통합 문제점 리스트(외주처·품번·품명·금형 제작 업체·카테고리·발생 문제점 …) → 문제점 등록·갱신."""
    ensure(conn)
    hi, cols = _find_header(rows, ISSUE_HEADERS, "problem")
    if "part_no" not in cols:
        raise ValueError("품번 열이 없습니다")
    stats = {"new": 0, "updated": 0, "same": 0, "kept": 0, "skipped": 0}
    seen: dict = {}
    for row in rows[hi + 1:]:
        pn, prob = str(_cell(row, cols["part_no"])), str(_cell(row, cols["problem"]))
        v = (str(_cell(row, cols.get("vendor"))) or vendor or "").upper()
        if not (is_part_no(pn) and prob and prob not in ("-", "↑")):
            stats["skipped"] += 1 if any(str(x or "").strip() for x in row) else 0
            continue
        kw = {f: _cell(row, cols.get(f)) for f in ISSUE_FIELDS if f in cols and f != "status"}
        mold_kw = {f: str(_cell(row, cols.get(f))) for f in ("part_name", "mold_maker") if f in cols}
        _, how = _upsert_issue(conn, car, v, pn, mold_kw, kw, str(_cell(row, cols.get("status"))), seen, today, src)
        stats[how] += 1
    conn.commit()
    return stats


def import_workbook(conn, cfg: dict, car: str, data: bytes, vendor: str | None = None, today: date | None = None,
                    src: str = "", with_photos: bool = True) -> dict:
    """외주처 문제점 관리 엑셀(갑지 MASTER + 금형별 을지 시트, 사진 포함) 한 번에 가져오기.
    을지의 D열(PART VISUALS Before) 사진 → 수정 전, G열(After) → 수정 후."""
    import openpyxl
    ensure(conn)
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    stats = {"molds_new": 0, "molds_updated": 0, "new": 0, "updated": 0, "same": 0, "kept": 0, "photos": 0, "sheets": 0}
    master = next((ws for ws in wb.worksheets if ws.title.strip().upper() in ("MASTER", "갑지")), None)
    v = (vendor or "").upper()
    if master is not None:
        rows = [list(r) for r in master.iter_rows(values_only=True)]
        v = v or match_vendor(conn, car, _title_vendor(rows))
        st = import_molds(conn, car, rows, vendor=v, src=src)
        stats["molds_new"], stats["molds_updated"] = st["new"], st["updated"]
    if not v:
        raise ValueError("외주처를 알 수 없습니다. 외주처를 지정해 주세요")
    seen: dict = {}
    for ws in wb.worksheets:
        if ws is master or ws.title.strip() in ("양식", "STATUS_기준"):
            continue
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        hi = next((i for i, r in enumerate(rows[:8]) if any(_hkey(x) == "problem" for x in r)), None)
        if hi is None:
            continue
        cols = _map_header(rows[hi], {"problem": ["problem"], "category": ["category"], "countermeasure": ["moldimprovement"],
                                      "occ_date": ["date"], "remark": ["remark"], "status": ["result"],
                                      "mold_fix": ["금형수정대상"]})
        title = " ".join(str(x) for x in rows[0] if x) if rows else ""
        parts = [p.strip() for p in title.split("|")]
        pn = parts[0] if parts and is_part_no(parts[0]) else ws.title.strip()
        if not is_part_no(pn):
            continue
        mold_kw = {k: parts[i] for i, k in ((1, "part_name"), (2, "item"), (3, "mold_type"), (4, "mold_maker"))
                   if len(parts) > i and parts[i] and not parts[i].startswith("#")}
        stats["sheets"] += 1
        row_issue: dict[int, int] = {}
        for ri in range(hi + 1, len(rows)):
            r = rows[ri]
            prob = str(_cell(r, cols.get("problem")))
            if not prob or prob in ("-",):
                continue
            kw = {f: _cell(r, cols.get(f)) for f in ("problem", "category", "countermeasure", "occ_date", "remark", "mold_fix")
                  if f in cols}
            iid, how = _upsert_issue(conn, car, v, pn, mold_kw, kw, str(_cell(r, cols.get("status"))), seen, today, src)
            stats[how] += 1
            row_issue[ri] = iid
        if not (with_photos and row_issue):
            continue
        first = min(row_issue)
        for im in getattr(ws, "_images", []):
            fr = getattr(im.anchor, "_from", None)
            if fr is None:
                continue
            grp = "after" if fr.col >= 5 else "before"
            ri = max(fr.row, first)
            cand = [k for k in row_issue if k <= ri]
            iid = row_issue[max(cand)] if cand else row_issue[first]
            try:
                if save_photo(conn, cfg, iid, im._data(), grp, source="import", commit=False):
                    stats["photos"] += 1
            except Exception:
                continue
    conn.commit()
    return stats


def import_any(conn, cfg: dict, car: str, name: str, data: bytes, vendor: str | None = None,
               today: date | None = None) -> dict:
    """올린 파일 종류를 알아서 구분: 외주처 엑셀(을지 시트 있음) → 통합 문제점 리스트(문제점 열) → 금형 리스트."""
    if name.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        is_wb = False
        for ws in wb.worksheets[:60]:
            if ws.title.strip() in ("양식", "STATUS_기준"):
                continue
            head = [list(r) for r in ws.iter_rows(min_row=1, max_row=8, values_only=True)]
            if any(_hkey(x) == "problem" for r in head for x in r):
                is_wb = True
                break
        wb.close()
        if is_wb:
            return {"kind": "외주처 엑셀"} | import_workbook(conn, cfg, car, data, vendor, today, src=name)
    rows = read_rows(name, data)
    try:
        _find_header(rows, ISSUE_HEADERS, "problem")
    except ValueError:
        return {"kind": "금형 리스트"} | import_molds(conn, car, rows, vendor, src=name)
    return {"kind": "문제점 리스트"} | import_issues(conn, car, rows, vendor, today, src=name)


# ---------------- 엑셀 (양식 그대로 내보내기) ----------------
def _safe_sheet(name: str, used: set) -> str:
    base = re.sub(r"[\[\]:*?/\\]", "_", name)[:28] or "금형"
    n, s = 1, base
    while s.upper() in used:
        n += 1
        s = f"{base[:25]}_{n}"
    used.add(s.upper())
    return s


def export_vendor(conn, cfg: dict, car: str, vendor: str, path: Path, with_photos: bool = True) -> Path:
    """외주처 1곳 = 엑셀 1개: 갑지(MASTER) + 금형별 을지(수정 전/후 사진은 표시가 들어간 그림)."""
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    ensure(conn)
    vendor = vendor.upper()
    ms = molds(conn, car, vendor)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "MASTER"
    thin = Side(style="thin", color="9AA5AB")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_fill = PatternFill("solid", fgColor="1F3A68")
    white_b = Font(bold=True, color="FFFFFF")
    wrap = Alignment(wrap_text=True, vertical="center")
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    hdr = ["NO", "Part No", "PART NAME", "ITEM", "MOLD TYPE", "금형 제작 업체", *CATS, "TOTAL", "진행", "담당",
           "LAST Date", "STATUS", "금형수정 대상", "T/O 현황", "중량"]
    ws.cell(1, 1, f"MOLD ISSUE LIST — {vendor}").font = Font(bold=True, size=14)
    ws.cell(1, 8, f"{car} · {date.today().isoformat()} 기준").font = Font(color="586477")
    for c, h in enumerate(hdr, 1):
        x = ws.cell(2, c, h)
        x.font, x.fill, x.alignment, x.border = white_b, head_fill, center, border
    used = {"MASTER"}
    names = {}
    for m in ms:
        names[m["id"]] = _safe_sheet(m["part_no"], used)
    for i, m in enumerate(ms, 1):
        r = i + 2
        vals = [i, m["part_no"], m["part_name"], m["item"], m["mold_type"], m["mold_maker"],
                *[m["counts"][c] for c in CATS], m["total"], m["open"], m["owner"], m["last"], m["status"], m["fix_open"],
                m["to_round"], m["weight"]]
        for c, val in enumerate(vals, 1):
            x = ws.cell(r, c, val)
            x.border, x.alignment = border, center if c not in (3,) else wrap
        link = ws.cell(r, 2)
        link.hyperlink = f"#'{names[m['id']]}'!A1"
        link.font = Font(color="0E6A86", underline="single")
    widths = [5, 15, 34, 10, 11, 13, *[8] * len(CATS), 7, 6, 10, 11, 9, 9, 8, 9]
    for c, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = "C3"
    tmp_imgs = []
    ihdr = ["NO", "PROBLEM", "CATEGORY", "PART VISUALS (Before)", "MOLD IMPROVEMENT", "DATE", "PART VISUALS (After)",
            "Remark", "RESULT", "금형수정 대상", "중요도", "수정 목표일", "수정 후 T/O", "T/O 결과", "진행 메모"]
    for m in ms:
        sh = wb.create_sheet(names[m["id"]])
        sh.cell(1, 1, " | ".join(x for x in (m["part_no"], m["part_name"], m["item"], m["mold_type"], m["mold_maker"]) if x)).font = Font(bold=True, size=12)
        back = sh.cell(2, 1, "◀ 갑지로 돌아가기")
        back.hyperlink = "#'MASTER'!A1"
        back.font = Font(color="0E6A86", underline="single")
        for c, h in enumerate(ihdr, 1):
            x = sh.cell(3, c, h)
            x.font, x.fill, x.alignment, x.border = white_b, head_fill, center, border
        for c, w in enumerate([5, 30, 11, 34, 34, 11, 34, 16, 10, 8, 7, 11, 9, 9, 24], 1):
            sh.column_dimensions[get_column_letter(c)].width = w
        rows = issues(conn, mold_id=m["id"])
        for k, it in enumerate(rows, 1):
            r = k + 3
            vals = [k, it["problem"], it["category"], "", it["countermeasure"], it["occ_date"], "", it["remark"],
                    it["status"], it["mold_fix"], it["importance"], it["due"], it["to_after"], it["to_result"], it["progress"]]
            for c, val in enumerate(vals, 1):
                x = sh.cell(r, c, val)
                x.border, x.alignment = border, center if c in (1, 3, 6, 9, 10, 11, 12, 13, 14) else wrap
            ph = photos(conn, it["id"]) if with_photos else []
            if ph:
                sh.row_dimensions[r].height = 130
                for grp, col in (("before", "D"), ("after", "G")):
                    g = [p for p in ph if p["grp"] == grp]
                    if not g:
                        continue
                    _place_image(sh, f"{col}{r}", g[0]["marked_path"] or g[0]["path"], tmp_imgs, extra=len(g) - 1)
            else:
                sh.row_dimensions[r].height = 36
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


def _place_image(sh, anchor: str, src: str, keep: list, extra: int = 0) -> None:
    from openpyxl.drawing.image import Image as XImage
    from PIL import Image
    try:
        im = Image.open(src)
    except Exception:
        return
    max_w, max_h = 240, 165          # 셀 안 크기 (px)
    r = min(max_w / im.width, max_h / im.height, 1)
    b = io.BytesIO()
    im.convert("RGB").resize((max(1, int(im.width * r * 2)), max(1, int(im.height * r * 2)))).save(b, "JPEG", quality=85)
    b.seek(0)
    keep.append(b)
    x = XImage(b)
    x.width, x.height = int(im.width * r), int(im.height * r)
    from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, OneCellAnchor
    from openpyxl.drawing.xdr import XDRPositiveSize2D
    from openpyxl.utils.cell import coordinate_from_string, column_index_from_string
    from openpyxl.utils.units import pixels_to_EMU
    col, row = coordinate_from_string(anchor)
    x.anchor = OneCellAnchor(_from=AnchorMarker(col=column_index_from_string(col) - 1, colOff=pixels_to_EMU(4),
                                                row=row - 1, rowOff=pixels_to_EMU(4)),
                             ext=XDRPositiveSize2D(pixels_to_EMU(x.width), pixels_to_EMU(x.height)))
    sh.add_image(x)
    if extra:
        sh[anchor].value = f"+{extra}장"
        from openpyxl.styles import Alignment
        sh[anchor].alignment = Alignment(vertical="bottom", horizontal="right")


def export_list(conn, car: str, path: Path) -> Path:
    """통합 금형 문제점 전체 관리 리스트 (드라이브 통합 리스트와 같은 열 + 진행 단계)."""
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    ensure(conn)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "통합 문제점 리스트"
    hdr = ["No", "외주처", "품번", "품명", "금형 제작 업체", "카테고리", "발생 문제점(Problem)", "수리/개선 계획(Improvement)",
           "발생일", "수정 목표일", "진행 메모", "중요도", "수정 후 T/O 현황", "T/O 결과", "진행 단계", "원래 상태", "금형수정대상",
           "재발", "사진"]
    for c, h in enumerate(hdr, 1):
        x = ws.cell(1, c, h)
        x.font, x.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F3A68")
        x.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    mk = {m["id"]: m for m in molds(conn, car)}
    pc = photo_counts(conn)
    rows = conn.execute("SELECT * FROM lm_issues WHERE car=? ORDER BY vendor, part_no, occ_date, id", (car,)).fetchall()
    for i, r in enumerate(rows, 2):
        m = mk.get(r["mold_id"]) or {}
        vals = [f"L{r['id']}", r["vendor"], r["part_no"], r["part_name"], m.get("mold_maker", ""), r["category"], r["problem"],
                r["countermeasure"], r["occ_date"], r["due"], r["progress"], r["importance"], r["to_after"], r["to_result"],
                r["status"], r["src_status"], r["mold_fix"], f"L{r['recur_of']}" if r["recur_of"] else "", pc.get(r["id"], 0)]
        for c, val in enumerate(vals, 1):
            ws.cell(i, c, val).alignment = Alignment(wrap_text=c in (4, 7, 8, 11), vertical="top")
    for c, w in enumerate([7, 13, 15, 30, 13, 11, 40, 40, 11, 11, 22, 7, 9, 8, 10, 10, 8, 7, 6], 1):
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(hdr))}{max(2, len(rows) + 1)}"
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


def export_dir(cfg: dict, car: str) -> Path:
    base = Path(cfg["archive"].get("local_dir") or Path(cfg["archive"]["db_path"]).parent / "local")
    return base / re.sub(r"[^\w가-힣.-]+", "_", car).strip("_")


def export(conn, cfg: dict, car: str, vendor: str | None = None, out_dir: Path | None = None) -> list[Path]:
    """외주처를 고르면 그 외주처 엑셀 1개, 아니면 통합 리스트 + 외주처별 엑셀 전부."""
    d = Path(out_dir) if out_dir else export_dir(cfg, car)
    stamp = date.today().strftime("%Y%m%d")
    if vendor:
        return [export_vendor(conn, cfg, car, vendor, d / f"{car}_{vendor.upper()}_문제점_{stamp}.xlsx")]
    out = [export_list(conn, car, d / f"{car}_통합 문제점 리스트_{stamp}.xlsx")]
    for v in sorted({m["vendor"] for m in molds(conn, car)}):
        out.append(export_vendor(conn, cfg, car, v, d / f"{car}_{v}_문제점_{stamp}.xlsx"))
    return out
