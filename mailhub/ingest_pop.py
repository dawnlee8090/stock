"""그룹웨어 메일 서버에서 POP3 로 가져온다 (IMAP 권한이 없을 때의 대안).

- RETR(내려받기) 대신 TOP(미리보기) 명령을 사용한다. 대부분의 서버는 TOP 으로는
  읽음 처리를 하지 않지만 서버마다 다르므로 먼저 `pop-test` 로 1건 시험한다.
- 메일을 서버에서 지우지 않는다 (DELE 를 절대 보내지 않음).
- 헤더만 먼저 보고(TOP n 0) 이미 있는 메일·기간 밖 메일은 본문을 받지 않는다.
- POP3 는 받은편지함만 볼 수 있다 (보낸 메일은 수집 불가).
"""
from __future__ import annotations

import getpass
import poplib
import sqlite3
from datetime import datetime
from email.parser import BytesHeaderParser
from email.utils import parsedate_to_datetime
from pathlib import Path

from . import db
from .classify import Classifier
from .ingest_files import parse_eml_bytes
from .ingest_imap import get_password, save_password
from .textutil import iso, safe_filename

STATE_SQL = """
CREATE TABLE IF NOT EXISTS pop_seen (
    account TEXT, uidl TEXT, PRIMARY KEY (account, uidl)
);
"""
FULL_LINES = 10_000_000  # TOP 으로 본문 전체를 받기 위한 충분히 큰 줄 수


def _cfg(cfg: dict) -> dict:
    p = dict(cfg.get("pop", {}))
    im = cfg.get("imap", {})
    p.setdefault("host", im.get("host", ""))
    p.setdefault("user", im.get("user", ""))
    p.setdefault("port", 995 if p.get("ssl", True) else 110)
    if not p.get("host") or not p.get("user"):
        raise SystemExit("config.toml 의 [pop] 에 host 와 user 를 먼저 입력하세요.")
    return p


def connect(pcfg: dict, password: str) -> poplib.POP3:
    host, port = pcfg["host"], int(pcfg["port"])
    try:
        if pcfg.get("ssl", True):
            conn = poplib.POP3_SSL(host, port, timeout=60)
        else:
            conn = poplib.POP3(host, port, timeout=60)
            if pcfg.get("starttls"):
                conn.stls()
        conn.user(pcfg["user"])
        conn.pass_(password)
    except (poplib.error_proto, OSError) as e:
        raise SystemExit(f"[POP3 접속 실패] {host}:{port} → {e}\n"
                         "  포트/SSL 설정(995+ssl 또는 110), 아이디 형식, 비밀번호를 확인하세요.") from None
    return conn


def _header(conn: poplib.POP3, n: int):
    resp, lines, _ = conn.top(n, 0)
    return BytesHeaderParser().parsebytes(b"\r\n".join(lines))


def _full(conn: poplib.POP3, n: int) -> bytes:
    resp, lines, _ = conn.top(n, FULL_LINES)
    return b"\r\n".join(lines) + b"\r\n"


def pop_test(cfg: dict, step: str, log=print, ask=input) -> None:
    """메일 1건(가장 최근)으로 읽음 처리 여부를 시험한다."""
    pcfg = _cfg(cfg)
    pw = getpass.getpass(f"{pcfg['user']} 메일 비밀번호 (화면에 표시되지 않음): ")
    conn = connect(pcfg, pw)
    try:
        count, size = conn.stat()
        log(f"접속 성공: 받은편지함 {count:,}건 ({size / 1024 / 1024:,.0f} MB)")
        if not count:
            return
        h = _header(conn, count)
        log(f"가장 최근 메일: {h.get('Subject', '')}  /  {h.get('From', '')}  /  {h.get('Date', '')}")
        if step == "full":
            raw = _full(conn, count)
            log(f"본문 전체 미리보기로 받음: {len(raw):,} bytes")
        else:
            log("머리글(제목·보낸사람)만 미리보기로 받았습니다.")
    finally:
        conn.quit()
    save_password(pcfg["user"], pcfg["host"], pw)
    log("\n→ 이제 그룹웨어 웹메일에서 위 메일이 아직 '안 읽음'(굵은 글씨) 상태인지 확인하세요.")


def ingest_pop(conn_db: sqlite3.Connection, cfg: dict, since: str | None = None, max_new: int = 0,
               save_eml: bool = False, interactive: bool = True, log=print, _pop=None) -> dict:
    pcfg = _cfg(cfg)
    conn_db.executescript(STATE_SQL)
    account = f"{pcfg['user']}@{pcfg['host']}"
    clf = Classifier(cfg)
    eml_dir = Path(cfg["archive"].get("eml_dir", "data/eml"))
    since_dt = datetime.fromisoformat(since) if since else None
    stats = {"total": 0, "checked": 0, "fetched": 0, "added": 0, "skipped": 0, "old": 0, "errors": 0}

    pop = _pop or connect(pcfg, get_password(pcfg["user"], pcfg["host"], interactive))
    try:
        resp, items, _ = pop.uidl()
        pairs = []
        for it in items:
            num, uid = it.decode("ascii", "replace").split(None, 1)
            pairs.append((int(num), uid.strip()))
        stats["total"] = len(pairs)
        seen = {r[0] for r in conn_db.execute("SELECT uidl FROM pop_seen WHERE account=?", (account,))}
        new = [(n, u) for n, u in pairs if u not in seen]
        new.sort(reverse=True)  # 최신 메일부터
        log(f"받은편지함 {len(pairs):,}건 중 처음 보는 메일 {len(new):,}건")

        for n, uid in new:
            if max_new and stats["fetched"] >= max_new:
                break
            stats["checked"] += 1
            try:
                h = _header(pop, n)
                if since_dt and h.get("Date"):
                    try:
                        d = parsedate_to_datetime(h["Date"])
                        if datetime.fromisoformat(iso(d)) < since_dt:
                            stats["old"] += 1
                            conn_db.execute("INSERT OR IGNORE INTO pop_seen VALUES (?,?)", (account, uid))
                            continue
                    except (TypeError, ValueError):
                        pass
                mid = db.normalize_message_id(str(h.get("Message-ID", "")))
                if mid and db.exists(conn_db, "mid:" + mid):  # 이미 PST 등으로 받은 메일 → 본문 생략
                    stats["skipped"] += 1
                    conn_db.execute("INSERT OR IGNORE INTO pop_seen VALUES (?,?)", (account, uid))
                    continue
                raw = _full(pop, n)
                stats["fetched"] += 1
                mid, rec = parse_eml_bytes(raw)
                rec.update({"source": "pop3", "folder": "POP3/받은편지함"})
                rec.update(clf.classify(rec))
                key = db.make_key(mid, rec["sender_email"], rec["sent_at"], rec["subject"], rec["body"])
                new_id = db.insert_mail(conn_db, key, rec)
                if new_id is None:
                    stats["skipped"] += 1
                else:
                    stats["added"] += 1
                    if save_eml:
                        ym = (rec["sent_at"][:7] or "unknown").replace("-", "/")
                        p = eml_dir / ym / f"{new_id}_{safe_filename(rec['subject'])}.eml"
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(raw)
                        conn_db.execute("UPDATE mails SET file_path=? WHERE id=?", (str(p.resolve()), new_id))
                conn_db.execute("INSERT OR IGNORE INTO pop_seen VALUES (?,?)", (account, uid))
            except Exception as e:
                stats["errors"] += 1
                log(f"  [오류] {n}번 메일: {e}")
            if stats["checked"] % 100 == 0:
                conn_db.commit()
                log(f"    ... {stats['checked']:,}/{len(new):,} 확인 (추가 {stats['added']:,})")
    finally:
        try:
            pop.quit()  # DELE 를 보내지 않았으므로 서버 메일은 그대로
        except Exception:
            pass
    conn_db.commit()
    return stats
