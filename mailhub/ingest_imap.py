"""그룹웨어 메일 서버(IMAP)에서 직접 메일을 가져온다.

- 폴더를 '읽기 전용(EXAMINE)'으로 열고 본문은 BODY.PEEK 로 가져오므로
  그룹웨어의 '안 읽음' 표시가 바뀌지 않는다.
- 폴더별로 마지막으로 가져온 UID 를 기억해 다음부터는 새 메일만 가져온다.
- 아웃룩 백업(PST)과 같은 메일은 Message-ID 로 자동 중복 제거된다.
- 비밀번호는 config.toml 에 적지 않고 Windows 자격 증명 관리자(keyring)에 저장한다.
"""
from __future__ import annotations

import base64
import getpass
import imaplib
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from . import db
from .classify import Classifier
from .ingest_files import parse_eml_bytes
from .textutil import safe_filename

KEYRING_SERVICE = "mailhub-imap"
SKIP_NAMES = ("trash", "deleted", "junk", "spam", "draft", "휴지통", "스팸", "임시", "삭제", "정크")
LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\)\s+(?P<delim>"[^"]*"|NIL)\s+(?P<name>.+)$')

STATE_SQL = """
CREATE TABLE IF NOT EXISTS imap_state (
    account TEXT, folder TEXT, uidvalidity TEXT, last_uid INTEGER DEFAULT 0,
    PRIMARY KEY (account, folder)
);
"""


# ---------- IMAP 폴더 이름 (modified UTF-7) ----------
def utf7_decode(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        if s[i] == "&":
            j = s.find("-", i)
            if j < 0:
                out.append(s[i:])
                break
            if j == i + 1:
                out.append("&")
            else:
                b64 = s[i + 1:j].replace(",", "/")
                b64 += "=" * (-len(b64) % 4)
                try:
                    out.append(base64.b64decode(b64).decode("utf-16-be"))
                except Exception:
                    out.append(s[i:j + 1])
            i = j + 1
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def parse_list(lines) -> list[dict]:
    """LIST 응답 → [{raw, name, flags, sent}]"""
    out = []
    for line in lines or []:
        if isinstance(line, tuple):  # 리터럴 형태
            line = line[0] + line[1]
        if not line:
            continue
        m = LIST_RE.match(line.strip())
        if not m:
            continue
        flags = m.group("flags").decode("ascii", "ignore").lower()
        raw = m.group("name").decode("utf-8", "replace").strip()
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1].replace('\\"', '"')
        name = utf7_decode(raw)
        low = name.lower()
        sent = "\\sent" in flags or any(k in low for k in ("sent", "보낸"))
        skip = "\\noselect" in flags or "\\trash" in flags or "\\junk" in flags or "\\drafts" in flags \
            or any(k in low for k in SKIP_NAMES)
        out.append({"raw": raw, "name": name, "flags": flags, "sent": sent, "skip": skip})
    return out


# ---------- 비밀번호 ----------
def get_password(user: str, host: str, interactive: bool = True) -> str:
    pw = os.environ.get("MAILHUB_IMAP_PASSWORD")
    if pw:
        return pw
    try:
        import keyring  # pip install keyring
        pw = keyring.get_password(KEYRING_SERVICE, f"{user}@{host}")
        if pw:
            return pw
    except Exception:
        pass
    if not interactive:
        raise RuntimeError("저장된 비밀번호가 없습니다. 먼저 'python -m mailhub imap-setup' 을 실행하세요.")
    return getpass.getpass(f"{user} 메일 비밀번호 (화면에 표시되지 않음): ")


def save_password(user: str, host: str, pw: str) -> bool:
    try:
        import keyring
        keyring.set_password(KEYRING_SERVICE, f"{user}@{host}", pw)
        return True
    except Exception:
        return False


def connect(icfg: dict, password: str):
    host, port = icfg["host"], int(icfg.get("port", 993))
    if icfg.get("ssl", True):
        conn = imaplib.IMAP4_SSL(host, port)
    else:
        conn = imaplib.IMAP4(host, port)
        if icfg.get("starttls"):
            conn.starttls()
    try:
        conn.login(icfg["user"], password)
    except imaplib.IMAP4.error as e:
        msg = str(e)
        low = msg.lower()
        if "permission" in low or "not allowed" in low or "disabled" in low:
            hint = ("서버 접속은 됐지만 이 계정에 IMAP 사용 권한이 없습니다.\n"
                    "  → 전산팀(그룹웨어 관리자)에 '내 계정 IMAP 사용 허용'을 요청하세요.\n"
                    "  → 또는 config.toml 의 user 를 사번만(예: 230419) 으로 바꿔 한 번 더 시도해 보세요.")
        else:
            hint = "아이디(user) 형식 또는 비밀번호를 확인하세요. (메일 주소 / 사번만 둘 다 시도)"
        raise SystemExit(f"[IMAP 로그인 실패] 서버 응답: {msg}\n  {hint}") from None
    return conn


def _check_cfg(cfg: dict) -> dict:
    icfg = cfg.get("imap", {})
    if not icfg.get("host") or not icfg.get("user"):
        raise SystemExit("config.toml 의 [imap] 에 host 와 user 를 먼저 입력하세요. (config.example.toml 참고)")
    return icfg


# ---------- 명령 ----------
def setup(cfg: dict, log=print) -> None:
    icfg = _check_cfg(cfg)
    pw = getpass.getpass(f"{icfg['user']} 메일 비밀번호 (화면에 표시되지 않음): ")
    log(f"{icfg['host']}:{icfg.get('port', 993)} 접속 중...")
    conn = connect(icfg, pw)
    try:
        typ, lines = conn.list()
        folders = parse_list(lines)
        log("\n[메일 폴더]  (✓ = 수집 대상, 보낸 = 보낸 메일 폴더로 인식)")
        for f in folders:
            mark = " " if f["skip"] else "✓"
            log(f"  {mark} {f['name']}{'   ← 보낸' if f['sent'] and not f['skip'] else ''}")
    finally:
        conn.logout()
    if save_password(icfg["user"], icfg["host"], pw):
        log("\n접속 성공. 비밀번호를 Windows 자격 증명 관리자에 안전하게 저장했습니다.")
    else:
        log("\n접속 성공. (keyring 이 없어 비밀번호를 저장하지 못했습니다: pip install keyring)")


def ingest_imap(conn_db: sqlite3.Connection, cfg: dict, since: str | None = None,
                folder_filter: str | None = None, save_eml: bool = False, full: bool = False,
                interactive: bool = True, log=print, _imap=None) -> dict:
    icfg = _check_cfg(cfg)
    conn_db.executescript(STATE_SQL)
    account = f"{icfg['user']}@{icfg['host']}"
    clf = Classifier(cfg)
    eml_dir = Path(cfg["archive"].get("eml_dir", "data/eml"))
    batch = int(icfg.get("batch", 50))
    stats = {"folders": 0, "fetched": 0, "added": 0, "skipped": 0, "errors": 0}

    imap = _imap or connect(icfg, get_password(icfg["user"], icfg["host"], interactive))
    try:
        typ, lines = imap.list()
        folders = [f for f in parse_list(lines) if not f["skip"]]
        if icfg.get("folders"):
            wanted = [w.lower() for w in icfg["folders"]]
            folders = [f for f in folders if any(w in f["name"].lower() for w in wanted)]
        if folder_filter:
            folders = [f for f in folders if folder_filter.lower() in f["name"].lower()]

        for f in folders:
            typ, _ = imap.select('"' + f["raw"].replace('"', '\\"') + '"', readonly=True)  # EXAMINE: 읽기 전용
            if typ != "OK":
                log(f"  [폴더 열기 실패] {f['name']}")
                continue
            stats["folders"] += 1
            uv = (imap.response("UIDVALIDITY")[1] or [b""])[0]
            uv = uv.decode() if isinstance(uv, bytes) else str(uv or "")
            row = conn_db.execute("SELECT uidvalidity, last_uid FROM imap_state WHERE account=? AND folder=?",
                                  (account, f["raw"])).fetchone()
            last_uid = row[1] if (row and row[0] == uv and not full) else 0

            if last_uid:
                typ, data = imap.uid("SEARCH", None, f"UID {last_uid + 1}:*")
            elif since:
                d = datetime.fromisoformat(since).strftime("%d-%b-%Y")
                typ, data = imap.uid("SEARCH", None, f"SINCE {d}")
            else:
                typ, data = imap.uid("SEARCH", None, "ALL")
            uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) > last_uid)
            label = f"IMAP/{f['name']}" + (" [보낸메일]" if f["sent"] else "")
            if not uids:
                continue
            log(f"  {f['name']}: 새 메일 {len(uids):,}건")

            for i in range(0, len(uids), batch):
                chunk = uids[i:i + batch]
                typ, data = imap.uid("FETCH", ",".join(map(str, chunk)), "(UID BODY.PEEK[])")  # PEEK: 읽음 표시 안 함
                for item in data or []:
                    if not isinstance(item, tuple):
                        continue
                    m = re.search(rb"UID (\d+)", item[0])
                    raw = item[1]
                    stats["fetched"] += 1
                    try:
                        mid, rec = parse_eml_bytes(raw)
                        rec.update({"source": "imap", "folder": label})
                        rec.update(clf.classify(rec))
                        key = db.make_key(mid, rec["sender_email"], rec["sent_at"], rec["subject"], rec["body"])
                        new_id = db.insert_mail(conn_db, key, rec)
                        if new_id is None:
                            stats["skipped"] += 1
                            continue
                        stats["added"] += 1
                        if save_eml:
                            ym = (rec["sent_at"][:7] or "unknown").replace("-", "/")
                            p = eml_dir / ym / f"{new_id}_{safe_filename(rec['subject'])}.eml"
                            p.parent.mkdir(parents=True, exist_ok=True)
                            p.write_bytes(raw)
                            conn_db.execute("UPDATE mails SET file_path=? WHERE id=?", (str(p.resolve()), new_id))
                    except Exception as e:
                        stats["errors"] += 1
                        log(f"  [오류] {f['name']} UID {m.group(1).decode() if m else '?'}: {e}")
                # 배치마다 진행 위치 저장 → 중간에 끊겨도 이어서 진행
                conn_db.execute("INSERT OR REPLACE INTO imap_state(account, folder, uidvalidity, last_uid) "
                                "VALUES (?,?,?,?)", (account, f["raw"], uv, chunk[-1]))
                conn_db.commit()
                if len(uids) > batch:
                    log(f"    ... {min(i + batch, len(uids)):,}/{len(uids):,}")
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    conn_db.commit()
    return stats
