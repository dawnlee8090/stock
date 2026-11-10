"""검색: 3글자 이상 단어는 FTS(trigram) 인덱스, 2글자 이하(예: '금형', 'EO')는 LIKE 로 찾는다."""
from __future__ import annotations

import re
import shlex
import sqlite3

TEXT_COLS = ["subject", "sender_name", "sender_email", "to_addr", "cc_addr", "body", "attachments", "tags",
             "part_nos", "att_text"]

FALLBACK_COLS = ["subject", "sender_name", "sender_email", "attachments", "part_nos"]


def split_terms(query: str) -> list[str]:
    try:
        terms = shlex.split(query or "")
    except ValueError:
        terms = (query or "").split()
    return [t for t in terms if t.strip()]


def search(conn: sqlite3.Connection, query: str = "", sender: str | None = None,
           since: str | None = None, until: str | None = None, tag: str | None = None,
           has_attach: bool = False, attach_name: str | None = None, subject_only: bool = False,
           needs_action: bool = False, status: str | None = None, part_no: str | None = None,
           sort: str = "date", limit: int = 50, mine: bool = False,
           to: str | None = None, cars: list[str] | None = None, senders_any: list[str] | None = None,
           exclude_senders: list[str] | None = None, like_only: bool = False, offset: int = 0) -> list[sqlite3.Row]:
    where, params = [], []
    use_fts = False
    for raw in split_terms(query):
        neg = raw.startswith("-") and len(raw) > 1
        t = raw[1:] if neg else raw
        cols = ["subject"] if subject_only else (FALLBACK_COLS if like_only else TEXT_COLS)
        if len(t) >= 3 and not neg and not subject_only and not like_only:
            use_fts = True
            where.append("mails_fts MATCH ?")
            params.append('"' + t.replace('"', '""') + '"')
        else:
            like = " OR ".join(f"m.{c} LIKE ?" for c in cols)
            where.append(f"{'NOT ' if neg else ''}({like})")
            params += [f"%{t}%"] * len(cols)
    where, params = _merge_fts(where, params)

    if sender:
        where.append("(m.sender_email LIKE ? OR m.sender_name LIKE ?)")
        params += [f"%{sender}%"] * 2
    if since:
        where.append("m.sent_at >= ?")
        params.append(since)
    if until:
        where.append("m.sent_at < ?")
        params.append(until + "T99")  # 해당 날짜 포함
    if tag:
        where.append("(',' || m.tags || ',') LIKE ?")
        params.append(f"%,{tag},%")
    if has_attach:
        where.append("m.attachments != ''")
    if attach_name:
        where.append("m.attachments LIKE ?")
        params.append(f"%{attach_name}%")
    if needs_action:
        where.append("m.needs_action = 1")
    if status is not None:
        where.append("m.status = ?")
        params.append(status)
    if part_no:
        where.append("(m.part_nos LIKE ? OR m.subject LIKE ? OR m.body LIKE ?)")
        params += [f"%{part_no}%"] * 3
    if cars:   # 차종 폴더: 같은 차종의 여러 표기(BC4t, BC4 ...) 중 하나라도
        where.append("(" + " OR ".join("(',' || m.cars || ',') LIKE ?" for _ in cars) + ")")
        params += [f"%,{c},%" for c in cars]
    def _who(names):   # 보낸사람 주소·이름에 포함
        return "(" + " OR ".join("lower(m.sender_email) LIKE ? OR lower(m.sender_name) LIKE ?" for _ in names) + ")", \
            [f"%{n.lower()}%" for n in names for _ in (0, 1)]
    if senders_any:      # 예: 오토웨이 접수 (RPABOT)
        w, p = _who(senders_any)
        where.append(w)
        params += p
    if exclude_senders:  # 차종·분류 폴더에서는 시스템 알림 메일 제외
        w, p = _who(exclude_senders)
        where.append("NOT " + w)
        params += p
    if mine:
        where.append("m.is_mine = 1")
    if to:
        where.append("(m.to_addr LIKE ? OR m.cc_addr LIKE ?)")
        params += [f"%{to}%"] * 2

    covered = not use_fts and not params_touch_text(where)
    if covered:   # 폴더 목록: 색인(idx_mails_meta)만 훑어 번호를 고르고 본문은 고른 메일만 읽음
        ids_sql = "SELECT m.id FROM mails m" + (" WHERE " + " AND ".join(where) if where else "") + \
            " ORDER BY m.sent_at DESC LIMIT ? OFFSET ?"
        ids = [r[0] for r in conn.execute(ids_sql, [*params, limit, offset])]
        if not ids:
            return []
        rows = {r["id"]: r for r in conn.execute(
            f"SELECT * FROM mails WHERE id IN ({','.join('?' * len(ids))})", ids)}
        return [rows[i] for i in ids if i in rows]
    sql = "SELECT m.* FROM mails m"
    if use_fts:
        sql += " JOIN mails_fts ON mails_fts.rowid = m.id"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY " + ("bm25(mails_fts)" if (use_fts and sort == "relevance") else "m.sent_at DESC")
    sql += " LIMIT ? OFFSET ?"
    params += [limit, offset]
    rows = conn.execute(sql, params).fetchall()
    if not rows and use_fts and not offset:
        # 검색 색인에서 못 찾으면 (색인이 어긋났거나 특수한 띄어쓰기) 제목·보낸사람·첨부명·품번을 직접 훑어 한 번 더 찾는다
        rows = search(conn, query, sender=sender, since=since, until=until, tag=tag, has_attach=has_attach,
                      attach_name=attach_name, subject_only=subject_only, needs_action=needs_action, status=status,
                      part_no=part_no, sort="date", limit=limit, mine=mine, to=to, cars=cars,
                      senders_any=senders_any, exclude_senders=exclude_senders, like_only=True, offset=offset)
    return rows


_META_COLS = ("m.sent_at", "m.tags", "m.cars", "m.status", "m.needs_action", "m.is_mine", "m.sender_email", "m.sender_name")


def params_touch_text(where: list[str]) -> bool:
    """조건에 색인에 없는 열(본문·제목 등)이 들어 있으면 True."""
    for w in where:
        cols = set(re.findall(r"m\.[a-z_]+", w))
        if cols - set(_META_COLS):
            return True
    return False


def _merge_fts(where: list[str], params: list) -> tuple[list[str], list]:
    """'mails_fts MATCH ?' 조건 여러 개를 AND 로 묶은 하나의 MATCH 로 합친다."""
    out_w, out_p, fts = [], [], []
    pi = 0
    for w in where:
        n = w.count("?")
        chunk = params[pi:pi + n]
        pi += n
        if w == "mails_fts MATCH ?":
            fts.append(chunk[0])
        else:
            out_w.append(w)
            out_p += chunk
    if fts:
        out_w.insert(0, "mails_fts MATCH ?")
        out_p.insert(0, " AND ".join(fts))
    return out_w, out_p


def _find(text: str, query: str, width: int) -> str:
    text = re.sub(r"\s+", " ", text or "")
    for t in split_terms(query):
        if t.startswith("-"):
            continue
        i = text.lower().find(t.lower())
        if i >= 0:
            s, e = max(0, i - width), min(len(text), i + len(t) + width)
            return ("…" if s else "") + text[s:e] + ("…" if e < len(text) else "")
    return ""


def snippet(body: str, query: str, width: int = 70, att_text: str = "") -> str:
    """검색어가 나온 부분 미리보기. 본문에 없고 첨부 내용에 있으면 [첨부내용] 으로 표시."""
    hit = _find(body, query, width)
    if hit:
        return hit
    hit = _find(att_text, query, width)
    if hit:
        return "[첨부내용] " + hit
    body = re.sub(r"\s+", " ", body or "")
    return body[: width * 2] + ("…" if len(body) > width * 2 else "")


TODO_SQL = """
SELECT m.* FROM mails m
WHERE m.needs_action = 1 AND m.status NOT IN ('완료', '제외') AND m.sent_at >= ?
  {replied}
ORDER BY m.sent_at DESC LIMIT ? OFFSET ?
"""
# 같은 건(스레드)에 내가 나중에 보낸 메일이 있거나, 나중에 온 메일이 '내 회신에 대한 답장'이면 회신한 것으로 보고 제외
NOT_REPLIED = """AND NOT EXISTS (SELECT 1 FROM mails r WHERE r.thread_key = m.thread_key AND r.thread_key != ''
                    AND (r.is_mine = 1 OR r.quotes_me = 1) AND r.sent_at > m.sent_at)"""


def todo(conn: sqlite3.Connection, since: str = "", limit: int = 50,
         include_replied: bool = False, offset: int = 0) -> list[sqlite3.Row]:
    sql = TODO_SQL.format(replied="" if include_replied else NOT_REPLIED)
    return conn.execute(sql, (since or "", limit, offset)).fetchall()
