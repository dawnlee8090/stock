"""폴더 안의 .eml / .msg 파일을 DB 로 가져온다 (아웃룩에서 끌어다 저장한 메일 등)."""
from __future__ import annotations

import email
from email import policy
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path

from . import db
from .classify import Classifier
from .textutil import INLINE_IMAGE, clean_body, html_to_text, iso


def parse_eml(path: Path) -> tuple[str, dict]:
    with open(path, "rb") as f:
        msg = email.message_from_binary_file(f, policy=policy.default)
    name, addr = parseaddr(str(msg.get("From", "")))
    try:
        sent = iso(parsedate_to_datetime(str(msg.get("Date")))) if msg.get("Date") else ""
    except (TypeError, ValueError):
        sent = ""
    body = ""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is not None:
        try:
            content = part.get_content()
        except (LookupError, UnicodeDecodeError):
            content = part.get_payload(decode=True).decode("utf-8", "replace")
        body = html_to_text(content) if part.get_content_type() == "text/html" else content
    attachments = [a.get_filename() or "" for a in msg.iter_attachments()]

    def addrs(h: str) -> str:
        return "; ".join(f"{n} <{a}>" if n else a for n, a in getaddresses(msg.get_all(h, [])))

    rec = {
        "source": "eml", "folder": str(path.parent), "subject": str(msg.get("Subject", "")),
        "sender_name": name, "sender_email": addr.lower(), "to_addr": addrs("To"), "cc_addr": addrs("Cc"),
        "sent_at": sent, "body": clean_body(body),
        "attachments": "\n".join(a for a in attachments if a and not INLINE_IMAGE.match(a)),
        "file_path": str(path.resolve()),
    }
    return str(msg.get("Message-ID", "")), rec


def parse_msg(path: Path) -> tuple[str, dict]:
    try:
        import extract_msg  # pip install extract-msg
    except ImportError as e:
        raise RuntimeError(".msg 파일을 읽으려면 'pip install extract-msg' 가 필요합니다.") from e
    m = extract_msg.openMsg(str(path))
    try:
        name, addr = parseaddr(m.sender or "")
        body = m.body or ""
        if not body.strip() and getattr(m, "htmlBody", None):
            hb = m.htmlBody
            body = html_to_text(hb.decode("utf-8", "replace") if isinstance(hb, bytes) else hb)
        attachments = [getattr(a, "longFilename", None) or getattr(a, "shortFilename", None) or ""
                       for a in m.attachments]
        rec = {
            "source": "msg", "folder": str(path.parent), "subject": m.subject or "",
            "sender_name": name, "sender_email": addr.lower(), "to_addr": m.to or "", "cc_addr": m.cc or "",
            "sent_at": iso(m.date) if m.date else "", "body": clean_body(body),
            "attachments": "\n".join(a for a in attachments if a and not INLINE_IMAGE.match(a)),
            "file_path": str(path.resolve()),
        }
        return (m.messageId or ""), rec
    finally:
        m.close()


def ingest_folder(conn, cfg: dict, root: str | Path, log=print) -> dict:
    clf = Classifier(cfg)
    root = Path(root)
    files = [p for p in ([root] if root.is_file() else sorted(root.rglob("*")))
             if p.suffix.lower() in (".eml", ".msg")]
    stats = {"files": len(files), "added": 0, "skipped": 0, "errors": 0}
    for i, p in enumerate(files, 1):
        try:
            mid, rec = parse_eml(p) if p.suffix.lower() == ".eml" else parse_msg(p)
            rec.update(clf.classify(rec))
            key = db.make_key(mid, rec["sender_email"], rec["sent_at"], rec["subject"], rec["body"])
            if db.insert_mail(conn, key, rec):
                stats["added"] += 1
            else:
                stats["skipped"] += 1
        except Exception as e:  # 한 파일 오류로 전체가 멈추지 않도록
            stats["errors"] += 1
            log(f"  [오류] {p}: {e}")
        if i % 200 == 0:
            conn.commit()
            log(f"  ... {i}/{len(files)}")
    conn.commit()
    return stats
