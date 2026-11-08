"""금형 이관 종합장: 현지 법인으로 이관하는 금형별 서류·리스트·선적 일정 관리.

금형 1벌 = 1행. 항목(섹션)별로 미접수 → 요청 → 접수 → 검토중 → 확정 (해당없음) 상태와 파일을 관리한다.
  기본 섹션: 금형사양서, 성형조건표, 부품리스트, 선적일정, KD리스트, 원소재리스트
  (그 밖의 이름도 자유롭게 추가 가능: 도면·3D, 검사구·지그, 이관검수서 ...)
선적: 계획일, ETD/ETA, 실제 출항·도착, B/L, 선박, 포워더
리스트: 부품 / KD / 원소재 (KD·LP 구분) 품목 — 엑셀·CSV 에서 바로 불러오기
"""
from __future__ import annotations

import csv
import email
import io
import re
import sqlite3
from datetime import date, datetime
from email import policy
from pathlib import Path

from .followup import parse_due
from .textutil import safe_filename

SCHEMA = """
CREATE TABLE IF NOT EXISTS molds (
    id INTEGER PRIMARY KEY,
    mold_no TEXT DEFAULT '', car TEXT DEFAULT '', part_no TEXT DEFAULT '', part_name TEXT DEFAULT '',
    mold_vendor TEXT DEFAULT '', dest TEXT DEFAULT '', local_molder TEXT DEFAULT '',
    cavity TEXT DEFAULT '', tonnage TEXT DEFAULT '', mold_size TEXT DEFAULT '', mold_weight TEXT DEFAULT '',
    hot_runner TEXT DEFAULT '', resin TEXT DEFAULT '', target_date TEXT DEFAULT '',
    status TEXT DEFAULT '이관준비', note TEXT DEFAULT '', created_at TEXT, updated_at TEXT, seen_mail_max INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS mold_docs (
    mold_id INTEGER, section TEXT, status TEXT DEFAULT '미접수', doc_date TEXT DEFAULT '', version TEXT DEFAULT '',
    due TEXT DEFAULT '', files TEXT DEFAULT '', mail_id INTEGER, note TEXT DEFAULT '', updated_at TEXT,
    PRIMARY KEY (mold_id, section)
);
CREATE TABLE IF NOT EXISTS mold_ship (
    mold_id INTEGER PRIMARY KEY, plan_date TEXT DEFAULT '', etd TEXT DEFAULT '', eta TEXT DEFAULT '',
    atd TEXT DEFAULT '', ata TEXT DEFAULT '', bl_no TEXT DEFAULT '', vessel TEXT DEFAULT '', port_from TEXT DEFAULT '',
    port_to TEXT DEFAULT '', forwarder TEXT DEFAULT '', container TEXT DEFAULT '', note TEXT DEFAULT '', updated_at TEXT
);
CREATE TABLE IF NOT EXISTS mold_items (
    id INTEGER PRIMARY KEY, mold_id INTEGER, list_type TEXT, part_no TEXT DEFAULT '', name TEXT DEFAULT '',
    spec TEXT DEFAULT '', qty TEXT DEFAULT '', unit TEXT DEFAULT '', supply TEXT DEFAULT '', maker TEXT DEFAULT '',
    note TEXT DEFAULT '', source TEXT DEFAULT '', created_at TEXT
);
CREATE TABLE IF NOT EXISTS mold_mails (mold_id INTEGER, mail_id INTEGER, how TEXT, linked_at TEXT, PRIMARY KEY (mold_id, mail_id));
CREATE TABLE IF NOT EXISTS mold_log (id INTEGER PRIMARY KEY, mold_id INTEGER, at TEXT, what TEXT, note TEXT, mail_id INTEGER);
"""

SECTIONS = ["금형사양서", "성형조건표", "부품리스트", "선적일정", "KD리스트", "원소재리스트"]
DOC_STATUS = ["미접수", "요청", "접수", "검토중", "확정", "해당없음"]
DOC_DONE = ("확정", "해당없음")
MOLD_STATUS = ["제작중", "T/O", "이관준비", "선적대기", "선적", "현지도착", "현지T/O", "이관완료", "보류"]
MOLD_FIELDS = ["mold_no", "car", "part_no", "part_name", "mold_vendor", "dest", "local_molder", "cavity", "tonnage",
               "mold_size", "mold_weight", "hot_runner", "resin", "target_date", "note"]
SHIP_FIELDS = ["plan_date", "etd", "eta", "atd", "ata", "bl_no", "vessel", "port_from", "port_to", "forwarder",
               "container", "note"]
SHIP_DATES = {"plan_date", "etd", "eta", "atd", "ata"}
LIST_TYPES = {"부품": "부품리스트", "KD": "KD리스트", "원소재": "원소재리스트"}
ITEM_FIELDS = ["part_no", "name", "spec", "qty", "unit", "supply", "maker", "note"]
SYMBOL = {"확정": "●", "검토중": "◐", "접수": "◐", "요청": "○", "미접수": "✕", "해당없음": "－"}

# 엑셀·CSV 머리글 자동 인식 (소문자, 공백 제거 후 포함 여부)
HEADER_KEYS = {
    "part_no": ["품번", "partno", "partnumber", "p/no", "pno", "자재코드", "코드", "code"],
    "name": ["품명", "partname", "자재명", "description", "desc", "명칭", "name", "원소재명", "재질명"],
    "spec": ["규격", "spec", "grade", "그레이드", "재질", "material", "사양"],
    "qty": ["수량", "qty", "quantity", "소요량", "usage", "u/s", "중량", "weight"],
    "unit": ["단위", "unit", "uom"],
    "supply": ["kd/lp", "kd", "lp", "조달", "구분", "supply", "source"],
    "maker": ["메이커", "maker", "제조사", "업체", "vendor", "supplier", "공급사"],
    "note": ["비고", "remark", "note", "memo"],
}


def ensure(conn) -> None:
    conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _log(conn, mid: int, what: str, note: str = "", mail_id: int | None = None) -> None:
    conn.execute("INSERT INTO mold_log (mold_id, at, what, note, mail_id) VALUES (?,?,?,?,?)",
                 (mid, _now(), what, note, mail_id))


def mold_dir(cfg: dict, mid: int) -> Path:
    return Path(cfg["archive"].get("mold_dir", "data/molds")) / f"M{mid}"


# ---------- 금형 ----------
def add(conn, status: str = "이관준비", mail_id: int | None = None, today: date | None = None, **kw) -> int:
    ensure(conn)
    if status not in MOLD_STATUS:
        raise ValueError(f"상태는 {', '.join(MOLD_STATUS)} 중 하나")
    vals = {f: (kw.get(f) or "") for f in MOLD_FIELDS}
    if not (vals["mold_no"] or vals["part_no"] or vals["part_name"]):
        raise ValueError("금형번호, 품번, 품명 중 하나는 넣어 주세요")
    if vals["target_date"]:
        vals["target_date"] = parse_due(vals["target_date"], today)
    cur = conn.execute(f"INSERT INTO molds ({', '.join(MOLD_FIELDS)}, status, created_at, updated_at) "
                       f"VALUES ({', '.join('?' * len(MOLD_FIELDS))}, ?, ?, ?)",
                       (*[vals[f] for f in MOLD_FIELDS], status, _now(), _now()))
    mid = cur.lastrowid
    for sec in SECTIONS:
        conn.execute("INSERT OR IGNORE INTO mold_docs (mold_id, section, updated_at) VALUES (?,?,?)", (mid, sec, _now()))
    conn.execute("INSERT OR IGNORE INTO mold_ship (mold_id, updated_at) VALUES (?,?)", (mid, _now()))
    _log(conn, mid, f"등록 ({status})", "", mail_id)
    if mail_id:
        link(conn, mid, mail_id, "등록")
        conn.execute("UPDATE molds SET seen_mail_max=? WHERE id=?", (mail_id, mid))
    conn.commit()
    return mid


def get(conn, mid: int):
    ensure(conn)
    r = conn.execute("SELECT * FROM molds WHERE id=?", (mid,)).fetchone()
    if not r:
        raise ValueError(f"M{mid} 금형이 없습니다")
    return r


def update(conn, mid: int, status: str | None = None, note: str | None = None, mail_id: int | None = None,
           today: date | None = None, **fields):
    r = get(conn, mid)
    today = today or date.today()
    sets = {f: v for f, v in fields.items() if f in MOLD_FIELDS and v is not None and f != "note"}
    if "target_date" in sets and sets["target_date"]:
        sets["target_date"] = parse_due(sets["target_date"], today)
    if status and status != r["status"]:
        if status not in MOLD_STATUS:
            raise ValueError(f"상태는 {', '.join(MOLD_STATUS)} 중 하나")
        sets["status"] = status
    if note:
        sets["note"] = (r["note"] + "\n" if r["note"] else "") + f"[{today.month}/{today.day}] {note}"
    if sets:
        sets["updated_at"] = _now()
        conn.execute(f"UPDATE molds SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", (*sets.values(), mid))
    if status and status != r["status"]:
        _log(conn, mid, f"상태 {r['status']} → {status}", note or "", mail_id)
    elif note:
        _log(conn, mid, "메모", note, mail_id)
    if mail_id:
        link(conn, mid, mail_id, "수동")
    conn.commit()
    return get(conn, mid)


def link(conn, mid: int, mail_id: int, how: str = "수동") -> bool:
    cur = conn.execute("INSERT OR IGNORE INTO mold_mails (mold_id, mail_id, how, linked_at) VALUES (?,?,?,?)",
                       (mid, mail_id, how, _now()))
    return cur.rowcount > 0


# ---------- 첨부파일 꺼내기 ----------
def _mail_attachments(conn, mail_id: int, use_outlook: bool = False) -> list[tuple[str, bytes]]:
    m = conn.execute("SELECT file_path, entry_id, store_id FROM mails WHERE id=?", (mail_id,)).fetchone()
    if not m:
        raise ValueError(f"#{mail_id} 메일이 없습니다")
    fp = m["file_path"] or ""
    out = []
    if fp and Path(fp).exists() and fp.lower().endswith(".eml"):
        msg = email.message_from_bytes(Path(fp).read_bytes(), policy=policy.default)
        for a in msg.iter_attachments():
            if a.get_filename():
                out.append((a.get_filename(), a.get_payload(decode=True) or b""))
        return out
    if fp and Path(fp).exists() and fp.lower().endswith(".msg"):
        import extract_msg
        mm = extract_msg.openMsg(fp)
        try:
            for a in mm.attachments:
                n = getattr(a, "longFilename", None) or getattr(a, "shortFilename", None)
                if n and isinstance(getattr(a, "data", None), bytes):
                    out.append((n, a.data))
        finally:
            mm.close()
        return out
    if use_outlook and m["entry_id"]:
        import tempfile
        import win32com.client  # type: ignore
        ns = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
        item = ns.GetItemFromID(m["entry_id"], m["store_id"] or None)
        with tempfile.TemporaryDirectory() as td:
            for i, a in enumerate(item.Attachments, 1):
                p = Path(td) / f"{i}_{safe_filename(a.FileName or 'file', 100)}"
                a.SaveAsFile(str(p))
                out.append((a.FileName, p.read_bytes()))
        return out
    raise ValueError(f"#{mail_id} 메일의 원본이 없습니다 (PST 메일이면 --outlook 을 붙여 주세요)")


def save_attachments(conn, cfg: dict, mid: int, section: str, mail_id: int, name_part: str = "",
                     use_outlook: bool = False) -> list[str]:
    """메일 첨부 중 파일명에 name_part 가 들어간 것을 data/molds/M<번호>/<섹션>/ 에 저장."""
    d = mold_dir(cfg, mid) / safe_filename(section)
    saved = []
    for fname, data in _mail_attachments(conn, mail_id, use_outlook):
        if name_part and name_part.lower() not in fname.lower():
            continue
        if re.match(r"image\d+\.", fname, re.I):
            continue
        d.mkdir(parents=True, exist_ok=True)
        p = d / safe_filename(fname, 150)
        if p.exists() and p.read_bytes() != data:
            p = d / f"{p.stem}_{datetime.now():%m%d%H%M}{p.suffix}"
        p.write_bytes(data)
        saved.append(str(p.resolve()))
    return saved


# ---------- 서류(섹션) ----------
def set_doc(conn, cfg: dict, mid: int, section: str, status: str | None = None, mail_id: int | None = None,
            attach: str | None = None, files: list[str] | None = None, doc_date: str | None = None,
            version: str | None = None, due: str | None = None, note: str | None = None,
            use_outlook: bool = False, today: date | None = None) -> sqlite3.Row:
    get(conn, mid)
    today = today or date.today()
    section = section.strip()
    if status and status not in DOC_STATUS:
        raise ValueError(f"상태는 {', '.join(DOC_STATUS)} 중 하나")
    conn.execute("INSERT OR IGNORE INTO mold_docs (mold_id, section, updated_at) VALUES (?,?,?)", (mid, section, _now()))
    r = conn.execute("SELECT * FROM mold_docs WHERE mold_id=? AND section=?", (mid, section)).fetchone()
    new_files = list(files or [])
    if mail_id and attach is not None:
        new_files += save_attachments(conn, cfg, mid, section, mail_id, attach, use_outlook)
    elif files:
        d = mold_dir(cfg, mid) / safe_filename(section)
        d.mkdir(parents=True, exist_ok=True)
        new_files = []
        for f in files:
            src = Path(f)
            dst = d / safe_filename(src.name, 150)
            dst.write_bytes(src.read_bytes())
            new_files.append(str(dst.resolve()))
    sets = {}
    if status:
        sets["status"] = status
    elif (mail_id or new_files) and r["status"] in ("미접수", "요청"):
        sets["status"] = "접수"   # 메일·파일을 붙이면 자동으로 '접수'
    if new_files:
        old = [x for x in (r["files"] or "").split("\n") if x]
        sets["files"] = "\n".join(old + [f for f in new_files if f not in old])
    if mail_id:
        sets["mail_id"] = mail_id
        link(conn, mid, mail_id, section)
    if doc_date is not None:
        sets["doc_date"] = parse_due(doc_date, today) if doc_date else ""
    elif (mail_id or new_files) and not r["doc_date"]:
        md = conn.execute("SELECT sent_at FROM mails WHERE id=?", (mail_id,)).fetchone() if mail_id else None
        sets["doc_date"] = (md[0][:10] if md and md[0] else today.isoformat())
    if version is not None:
        sets["version"] = version
    if due is not None:
        sets["due"] = parse_due(due, today) if due else ""
    if note:
        sets["note"] = (r["note"] + "\n" if r["note"] else "") + f"[{today.month}/{today.day}] {note}"
    if sets:
        sets["updated_at"] = _now()
        conn.execute(f"UPDATE mold_docs SET {', '.join(f'{k}=?' for k in sets)} WHERE mold_id=? AND section=?",
                     (*sets.values(), mid, section))
        what = f"{section}: " + (f"{r['status']} → {sets['status']}" if sets.get("status") and sets["status"] != r["status"]
                                 else "갱신")
        _log(conn, mid, what, " / ".join(x for x in (note or "", f"파일 {len(new_files)}개" if new_files else "") if x), mail_id)
    conn.commit()
    return conn.execute("SELECT * FROM mold_docs WHERE mold_id=? AND section=?", (mid, section)).fetchone()


def docs(conn, mid: int) -> list:
    rows = conn.execute("SELECT * FROM mold_docs WHERE mold_id=?", (mid,)).fetchall()
    order = {s: i for i, s in enumerate(SECTIONS)}
    return sorted(rows, key=lambda r: (order.get(r["section"], 99), r["section"]))


def progress(conn, mid: int) -> tuple[int, int]:
    rows = docs(conn, mid)
    return sum(1 for r in rows if r["status"] in DOC_DONE), len(rows)


# ---------- 선적 ----------
def set_ship(conn, mid: int, mail_id: int | None = None, today: date | None = None, **kw):
    get(conn, mid)
    today = today or date.today()
    conn.execute("INSERT OR IGNORE INTO mold_ship (mold_id, updated_at) VALUES (?,?)", (mid, _now()))
    sets = {}
    for f in SHIP_FIELDS:
        v = kw.get(f)
        if v is None:
            continue
        sets[f] = (parse_due(v, today) if v else "") if f in SHIP_DATES else v
    if not sets:
        return conn.execute("SELECT * FROM mold_ship WHERE mold_id=?", (mid,)).fetchone()
    sets["updated_at"] = _now()
    conn.execute(f"UPDATE mold_ship SET {', '.join(f'{k}=?' for k in sets)} WHERE mold_id=?", (*sets.values(), mid))
    s = conn.execute("SELECT * FROM mold_ship WHERE mold_id=?", (mid,)).fetchone()
    # 선적일정 섹션·금형 상태 자동 반영
    doc_status = "확정" if s["atd"] else ("접수" if (s["etd"] or s["plan_date"]) else None)
    if doc_status:
        cur = conn.execute("SELECT status FROM mold_docs WHERE mold_id=? AND section='선적일정'", (mid,)).fetchone()
        if not cur or cur[0] not in ("확정", "해당없음") or doc_status == "확정":
            conn.execute("INSERT OR IGNORE INTO mold_docs (mold_id, section, updated_at) VALUES (?, '선적일정', ?)", (mid, _now()))
            conn.execute("UPDATE mold_docs SET status=?, updated_at=? WHERE mold_id=? AND section='선적일정'",
                         (doc_status, _now(), mid))
    m = get(conn, mid)
    new_status = "현지도착" if s["ata"] else ("선적" if s["atd"] else None)
    if new_status and MOLD_STATUS.index(m["status"]) < MOLD_STATUS.index(new_status) and m["status"] != "보류":
        conn.execute("UPDATE molds SET status=?, updated_at=? WHERE id=?", (new_status, _now(), mid))
        _log(conn, mid, f"상태 {m['status']} → {new_status}", "선적 정보로 자동 변경")
    _log(conn, mid, "선적일정 갱신", ", ".join(f"{k}={v}" for k, v in sets.items() if k != "updated_at"), mail_id)
    if mail_id:
        link(conn, mid, mail_id, "선적일정")
    conn.commit()
    return s


# ---------- 리스트(부품/KD/원소재) ----------
def _norm_head(h: str) -> str:
    return re.sub(r"[\s_\-\.()'’`]", "", str(h or "")).lower()


def _detect_header(rows: list[list]) -> tuple[int, dict]:
    """처음 15줄 중 머리글 줄과 열 위치를 찾는다."""
    best, best_map = -1, {}
    for i, row in enumerate(rows[:15]):
        mp = {}
        for j, cell in enumerate(row):
            h = _norm_head(cell)
            if not h:
                continue
            for key, words in HEADER_KEYS.items():
                if key in mp:
                    continue
                if any(w.replace("/", "") in h.replace("/", "") for w in words):
                    mp[key] = j
                    break
        if len(mp) > len(best_map) and ("part_no" in mp or "name" in mp):
            best, best_map = i, mp
    return best, best_map


def read_table(name: str, data: bytes) -> list[dict]:
    ext = Path(name).suffix.lower()
    rows: list[list] = []
    if ext in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        best_rows, best_score = [], 0
        for ws in wb.worksheets:   # 머리글이 가장 잘 맞는 시트
            rr = [[("" if c is None else str(c).strip()) for c in row] for row in ws.iter_rows(values_only=True)]
            i, mp = _detect_header(rr)
            if i >= 0 and len(mp) > best_score:
                best_rows, best_score = rr, len(mp)
        rows = best_rows
    elif ext in (".csv", ".txt"):
        text = None
        for enc in ("utf-8-sig", "cp949"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text or ""))]
    else:
        raise ValueError(f"{name}: 엑셀(.xlsx) 또는 CSV 만 불러올 수 있습니다 (.xls 는 엑셀에서 .xlsx 로 저장)")
    hi, mp = _detect_header(rows)
    if hi < 0:
        raise ValueError(f"{name}: 머리글(품번/품명 등)을 찾지 못했습니다")
    out = []
    for row in rows[hi + 1:]:
        item = {k: "" for k in ITEM_FIELDS}
        item.update({k: (row[j] if j < len(row) else "") for k, j in mp.items()})
        if not (item.get("part_no") or item.get("name")):
            continue
        sup = (item.get("supply") or "").upper()
        item["supply"] = "KD" if "KD" in sup else ("LP" if "LP" in sup or "현지" in sup else item.get("supply", ""))
        out.append(item)
    return out


def add_items(conn, mid: int, list_type: str, items: list[dict], source: str = "", replace: bool = False) -> int:
    get(conn, mid)
    if list_type not in LIST_TYPES:
        raise ValueError(f"리스트 종류는 {', '.join(LIST_TYPES)} 중 하나")
    if replace:
        conn.execute("DELETE FROM mold_items WHERE mold_id=? AND list_type=?", (mid, list_type))
    for it in items:
        conn.execute(f"INSERT INTO mold_items (mold_id, list_type, {', '.join(ITEM_FIELDS)}, source, created_at) "
                     f"VALUES (?, ?, {', '.join('?' * len(ITEM_FIELDS))}, ?, ?)",
                     (mid, list_type, *[str(it.get(f) or "") for f in ITEM_FIELDS], source, _now()))
    _log(conn, mid, f"{LIST_TYPES[list_type]} {'교체' if replace else '추가'} {len(items)}건", source)
    conn.commit()
    return len(items)


def import_items(conn, cfg: dict, mid: int, list_type: str, path: str | None = None, mail_id: int | None = None,
                 attach: str = "", replace: bool = True, use_outlook: bool = False) -> tuple[int, str]:
    """파일 또는 메일 첨부(엑셀/CSV)에서 리스트를 불러오고, 해당 섹션을 '접수'로 + 파일 보관."""
    if path:
        name, data = Path(path).name, Path(path).read_bytes()
        files = [path]
    elif mail_id:
        cands = [(n, d) for n, d in _mail_attachments(conn, mail_id, use_outlook)
                 if (not attach or attach.lower() in n.lower()) and Path(n).suffix.lower() in (".xlsx", ".xlsm", ".csv")]
        if not cands:
            raise ValueError(f"#{mail_id} 메일에 '{attach}' 가 들어간 엑셀/CSV 첨부가 없습니다")
        name, data = cands[0]
        files = None
    else:
        raise ValueError("--file 또는 --mail 을 넣어 주세요")
    items = read_table(name, data)
    n = add_items(conn, mid, list_type, items, source=(f"#{mail_id} " if mail_id else "") + name, replace=replace)
    set_doc(conn, cfg, mid, LIST_TYPES[list_type], mail_id=mail_id, attach=(Path(name).stem if mail_id else None),
            files=files, note=f"{n}건 불러옴", use_outlook=use_outlook)
    return n, name


def items(conn, mid: int | None = None, list_type: str | None = None) -> list:
    ensure(conn)
    where, params = [], []
    if mid:
        where.append("i.mold_id=?")
        params.append(mid)
    if list_type:
        where.append("i.list_type=?")
        params.append(list_type)
    return conn.execute("SELECT i.*, m.mold_no, m.car, m.part_name AS mold_part FROM mold_items i JOIN molds m ON m.id=i.mold_id"
                        + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY i.mold_id, i.id", params).fetchall()


# ---------- 조회·점검 ----------
def listing(conn, status: str = "open", dest: str | None = None, car: str | None = None, q: str | None = None) -> list:
    ensure(conn)
    where, params = [], []
    if status == "open":
        where.append("status NOT IN ('이관완료')")
    elif status and status != "all":
        where.append("status=?")
        params.append(status)
    if dest:
        where.append("dest LIKE ?")
        params.append(f"%{dest}%")
    if car:
        where.append("car LIKE ?")
        params.append(f"%{car}%")
    if q:
        where.append("(mold_no LIKE ? OR part_no LIKE ? OR part_name LIKE ? OR mold_vendor LIKE ?)")
        params += [f"%{q}%"] * 4
    return conn.execute("SELECT * FROM molds" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id",
                        params).fetchall()


def ship(conn, mid: int):
    return conn.execute("SELECT * FROM mold_ship WHERE mold_id=?", (mid,)).fetchone()


def alerts(conn, m, today: date | None = None, warn_days: int = 14) -> list[str]:
    """선적 전 미완료 서류, 서류 기한 지남, 도착 예정일 지남 등."""
    today = today or date.today()
    out = []
    s = ship(conn, m["id"])
    missing = [d["section"] for d in docs(conn, m["id"]) if d["status"] not in DOC_DONE and d["section"] != "선적일정"]
    if s and s["etd"] and not s["atd"]:
        n = (date.fromisoformat(s["etd"]) - today).days
        if n < 0:
            out.append(f"ETD {s['etd'][5:]} 지남 - 출항 확인 필요")
        elif n <= warn_days and missing:
            out.append(f"선적 D-{n}, 미완료: {', '.join(missing)}")
    if s and s["eta"] and s["atd"] and not s["ata"] and date.fromisoformat(s["eta"]) < today:
        out.append(f"ETA {s['eta'][5:]} 지남 - 현지 도착 확인 필요")
    for d in docs(conn, m["id"]):
        if d["due"] and d["status"] not in DOC_DONE and d["due"] < today.isoformat():
            out.append(f"{d['section']} 기한 {d['due'][5:]} 지남")
    if m["target_date"] and m["status"] != "이관완료" and m["target_date"] < today.isoformat():
        out.append(f"이관 목표 {m['target_date'][5:]} 지남")
    return out


def new_mail_count(conn, m) -> int:
    return conn.execute("SELECT COUNT(*) FROM mold_mails WHERE mold_id=? AND mail_id > ?",
                        (m["id"], m["seen_mail_max"] or 0)).fetchone()[0]


def mark_seen(conn, mid: int) -> None:
    mx = conn.execute("SELECT MAX(mail_id) FROM mold_mails WHERE mold_id=?", (mid,)).fetchone()[0] or 0
    conn.execute("UPDATE molds SET seen_mail_max=? WHERE id=?", (mx, mid))
    conn.commit()


def inbox(conn, since: str = "", limit: int = 50) -> list:
    ensure(conn)
    rows = conn.execute(
        "SELECT m.* FROM mails m WHERE (',' || m.tags || ',') LIKE '%,금형이관,%' AND m.sent_at >= ? AND m.is_mine = 0 "
        "AND NOT EXISTS (SELECT 1 FROM mold_mails x WHERE x.mail_id = m.id) ORDER BY m.sent_at DESC", (since or "",)).fetchall()
    linked = {r[0] for r in conn.execute("SELECT DISTINCT m.thread_key FROM mold_mails x JOIN mails m ON m.id=x.mail_id "
                                         "WHERE m.thread_key != ''")}
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
    """금형번호·품번이 나오는 금형이관 메일, 연결된 메일의 회신을 자동 연결."""
    from .eo import expand_parts
    ensure(conn)
    added = []
    for m in conn.execute("SELECT * FROM molds WHERE status != '이관완료'"):
        since = (m["created_at"] or "")[:10]
        cands = []
        if m["mold_no"] and len(m["mold_no"]) >= 4:
            cands += [(x[0], "금형번호") for x in conn.execute(
                "SELECT id FROM mails WHERE sent_at >= ? AND (subject LIKE ? OR body LIKE ? OR att_text LIKE ?)",
                (since, *[f"%{m['mold_no']}%"] * 3))]
        for pn in expand_parts(m["part_no"]):
            cands += [(x[0], "품번") for x in conn.execute(
                "SELECT id FROM mails WHERE sent_at >= ? AND (part_nos LIKE ? OR subject LIKE ?) "
                "AND (',' || tags || ',') LIKE '%,금형이관,%'", (since, f"%{pn}%", f"%{pn}%"))]
        cands += [(x[0], "같은 건") for x in conn.execute(
            "SELECT m2.id FROM mold_mails x JOIN mails m1 ON m1.id=x.mail_id "
            "JOIN mails m2 ON m2.thread_key=m1.thread_key AND m1.thread_key != '' WHERE x.mold_id=? AND m2.sent_at >= ?",
            (m["id"], since))]
        for mid_, how in cands:
            if link(conn, m["id"], mid_, how):
                added.append((m["id"], mid_, how))
    conn.commit()
    return added
