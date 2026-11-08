"""SQLite 저장소 + FTS5(trigram) 전문 검색 인덱스."""
from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from .textutil import fix_text

SCHEMA = """
CREATE TABLE IF NOT EXISTS mails (
    id            INTEGER PRIMARY KEY,
    msg_key       TEXT UNIQUE NOT NULL,      -- Message-ID 또는 내용 해시 (중복 방지)
    source        TEXT,                      -- outlook / eml / msg
    folder        TEXT,                      -- 사서함/폴더 경로 또는 파일 경로
    subject       TEXT DEFAULT '',
    sender_name   TEXT DEFAULT '',
    sender_email  TEXT DEFAULT '',
    to_addr       TEXT DEFAULT '',
    cc_addr       TEXT DEFAULT '',
    sent_at       TEXT DEFAULT '',           -- ISO 형식 'YYYY-MM-DDTHH:MM:SS'
    body          TEXT DEFAULT '',
    attachments   TEXT DEFAULT '',           -- 첨부파일명 (줄바꿈 구분)
    entry_id      TEXT DEFAULT '',           -- 아웃룩 원본 열기용
    store_id      TEXT DEFAULT '',
    file_path     TEXT DEFAULT '',           -- 저장된 .msg/.eml 경로
    tags          TEXT DEFAULT '',           -- 자동 분류 태그 (쉼표 구분)
    part_nos      TEXT DEFAULT '',           -- 추출된 품번 (쉼표 구분)
    needs_action  INTEGER DEFAULT 0,         -- 1: 검토/회신 필요로 판단
    is_mine       INTEGER DEFAULT 0,         -- 1: 내가 보낸 메일
    thread_key    TEXT DEFAULT '',           -- RE:/FW: 뗀 제목 (같은 건 묶음)
    quotes_me     INTEGER DEFAULT 0,         -- 1: 내 회신에 대한 답장 (= 내가 회신했다는 증거)
    att_text      TEXT DEFAULT '',           -- 첨부파일(엑셀/워드/PPT/PDF) 안의 글자
    att_indexed   INTEGER DEFAULT 0,         -- 1: 첨부 내용 추출 완료
    status        TEXT DEFAULT '',           -- 사용자가 관리: 진행중/완료/보류 ...
    note          TEXT DEFAULT '',
    ingested_at   TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mails_sent_at ON mails(sent_at);
CREATE INDEX IF NOT EXISTS idx_mails_sender ON mails(sender_email);

"""

FIELDS = ["source", "folder", "subject", "sender_name", "sender_email", "to_addr", "cc_addr",
          "sent_at", "body", "attachments", "entry_id", "store_id", "file_path",
          "tags", "part_nos", "needs_action", "is_mine", "thread_key", "quotes_me", "att_text", "att_indexed"]

# 전문 검색 대상 열 (바뀌면 connect() 가 검색 인덱스를 자동으로 다시 만든다)
FTS_COLS = ["subject", "sender_name", "sender_email", "to_addr", "cc_addr", "body", "attachments",
            "tags", "part_nos", "att_text"]


def _fts_sql() -> str:
    cols = ", ".join(FTS_COLS)
    new = ", ".join(f"new.{c}" for c in FTS_COLS)
    old = ", ".join(f"old.{c}" for c in FTS_COLS)
    return f"""
CREATE VIRTUAL TABLE mails_fts USING fts5({cols}, content='mails', content_rowid='id', tokenize='trigram');
CREATE TRIGGER mails_ai AFTER INSERT ON mails BEGIN
  INSERT INTO mails_fts(rowid, {cols}) VALUES (new.id, {new});
END;
CREATE TRIGGER mails_ad AFTER DELETE ON mails BEGIN
  INSERT INTO mails_fts(mails_fts, rowid, {cols}) VALUES ('delete', old.id, {old});
END;
CREATE TRIGGER mails_au AFTER UPDATE OF {cols} ON mails BEGIN
  INSERT INTO mails_fts(mails_fts, rowid, {cols}) VALUES ('delete', old.id, {old});
  INSERT INTO mails_fts(rowid, {cols}) VALUES (new.id, {new});
END;
"""


def _ensure_fts(conn: sqlite3.Connection) -> None:
    have = [r[1] for r in conn.execute("PRAGMA table_info(mails_fts)")]
    if have == FTS_COLS:
        return
    n = conn.execute("SELECT COUNT(*) FROM mails").fetchone()[0]
    if have and n:
        print(f"[안내] 검색 인덱스를 새 형식으로 다시 만듭니다 (메일 {n:,}건, 수 분 걸릴 수 있음)...", flush=True)
    conn.executescript("""
DROP TRIGGER IF EXISTS mails_ai; DROP TRIGGER IF EXISTS mails_ad; DROP TRIGGER IF EXISTS mails_au;
DROP TABLE IF EXISTS mails_fts;
""" + _fts_sql())
    if n:
        conn.execute("INSERT INTO mails_fts(mails_fts) VALUES ('rebuild')")
    conn.commit()


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    # 이전 버전 DB 업그레이드 (새 열 추가 → 'retag' 로 값 채움)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(mails)")}
    for col, ddl in (("is_mine", "INTEGER DEFAULT 0"), ("thread_key", "TEXT DEFAULT ''"),
                     ("quotes_me", "INTEGER DEFAULT 0"), ("att_text", "TEXT DEFAULT ''"),
                     ("att_indexed", "INTEGER DEFAULT 0")):
        if col not in cols:
            conn.execute(f"ALTER TABLE mails ADD COLUMN {col} {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mails_thread ON mails(thread_key, sent_at)")
    conn.commit()
    _ensure_fts(conn)
    return conn


def normalize_message_id(mid: str | None) -> str:
    return (mid or "").strip().strip("<>").strip().lower()


def make_key(message_id: str | None, sender: str, sent_at: str, subject: str, body: str) -> str:
    """Message-ID 가 있으면 그대로, 없으면 내용 해시로 중복을 판정한다.
    서버 사서함과 PST 백업에 같은 메일이 있어도 한 번만 저장된다."""
    mid = normalize_message_id(fix_text(message_id))
    if mid:
        return "mid:" + mid
    norm_body = re.sub(r"\s+", " ", body or "")[:500]
    raw = "|".join([(sender or "").lower(), (sent_at or "")[:16], (subject or "").strip(), norm_body])
    return "h:" + hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()


def exists(conn: sqlite3.Connection, key: str) -> bool:
    return conn.execute("SELECT 1 FROM mails WHERE msg_key=?", (key,)).fetchone() is not None


def insert_mail(conn: sqlite3.Connection, key: str, rec: dict) -> int | None:
    """새 메일이면 id 반환, 이미 있으면 None (빈 원본 링크 정보만 보충)."""
    rec = {k: fix_text(v) for k, v in rec.items()}
    key = fix_text(key)
    row = conn.execute("SELECT id, entry_id, file_path FROM mails WHERE msg_key=?", (key,)).fetchone()
    if row:
        updates = {}
        if not row["entry_id"] and rec.get("entry_id"):
            updates["entry_id"] = rec["entry_id"]
            updates["store_id"] = rec.get("store_id", "")
        if not row["file_path"] and rec.get("file_path"):
            updates["file_path"] = rec["file_path"]
        if updates:
            sets = ", ".join(f"{k}=?" for k in updates)
            conn.execute(f"UPDATE mails SET {sets} WHERE id=?", (*updates.values(), row["id"]))
        return None
    cols = ["msg_key", *FIELDS, "ingested_at"]
    vals = [key, *[rec.get(f, 0 if f in ("needs_action", "is_mine", "quotes_me", "att_indexed") else "") for f in FIELDS],
            datetime.now().isoformat(timespec="seconds")]
    cur = conn.execute(
        f"INSERT INTO mails ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
    return cur.lastrowid
