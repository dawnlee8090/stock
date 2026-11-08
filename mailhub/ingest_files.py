"""폴더 안의 .eml / .msg 파일을 DB 로 가져온다 (아웃룩에서 끌어다 저장한 메일 등)."""
from __future__ import annotations

import email
from email import policy
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path

from . import attach, db
from .classify import Classifier
from .textutil import INLINE_IMAGE, clean_body, html_to_text, iso, safe_filename


def parse_eml_bytes(data: bytes, with_att_text: bool = True) -> tuple[str, dict]:
    """메일 원문(RFC822 바이트) → (Message-ID, 레코드). .eml 파일과 IMAP 수집에서 공통 사용."""
    msg = email.message_from_bytes(data, policy=policy.default)

    def header(h: str) -> str:
        try:
            return str(msg.get(h, "") or "")
        except Exception:  # 깨진 헤더
            return ""

    name, addr = parseaddr(header("From"))
    try:
        sent = iso(parsedate_to_datetime(header("Date"))) if header("Date") else ""
    except (TypeError, ValueError, IndexError):
        sent = ""
    body = ""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is not None:
            try:
                content = part.get_content()
            except (LookupError, UnicodeDecodeError, AssertionError):
                content = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
            body = html_to_text(content) if part.get_content_type() == "text/html" else content
    except Exception:
        body = ""
    attachments, att_items = [], []
    try:
        for a in msg.iter_attachments():
            fname = a.get_filename() or ""
            attachments.append(fname)
            if with_att_text and fname and Path(fname).suffix.lower() in attach.SUPPORTED:
                try:
                    att_items.append((fname, attach.extract_text(fname, a.get_payload(decode=True) or b"")))
                except Exception:
                    pass
    except Exception:
        pass

    def addrs(h: str) -> str:
        try:
            return "; ".join(f"{n} <{a}>" if n else a for n, a in getaddresses([str(v) for v in msg.get_all(h, [])]))
        except Exception:
            return ""

    rec = {
        "subject": header("Subject"), "sender_name": name, "sender_email": addr.lower(),
        "to_addr": addrs("To"), "cc_addr": addrs("Cc"), "sent_at": sent, "body": clean_body(body),
        "attachments": "\n".join(a for a in attachments if a and not INLINE_IMAGE.match(a)),
        "att_text": attach.combine(att_items), "att_indexed": int(with_att_text),
    }
    return header("Message-ID"), rec


def parse_eml(path: Path) -> tuple[str, dict]:
    mid, rec = parse_eml_bytes(path.read_bytes())
    rec.update({"source": "eml", "folder": str(path.parent), "file_path": str(path.resolve())})
    return mid, rec


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
        attachments, att_items = [], []
        for a in m.attachments:
            fname = getattr(a, "longFilename", None) or getattr(a, "shortFilename", None) or ""
            attachments.append(fname)
            data = getattr(a, "data", None)
            if fname and isinstance(data, bytes) and Path(fname).suffix.lower() in attach.SUPPORTED:
                att_items.append((fname, attach.extract_text(fname, data)))
        rec = {
            "source": "msg", "folder": str(path.parent), "subject": m.subject or "",
            "sender_name": name, "sender_email": addr.lower(), "to_addr": m.to or "", "cc_addr": m.cc or "",
            "sent_at": iso(m.date) if m.date else "", "body": clean_body(body),
            "attachments": "\n".join(a for a in attachments if a and not INLINE_IMAGE.match(a)),
            "file_path": str(path.resolve()),
            "att_text": attach.combine(att_items), "att_indexed": 1,
        }
        return (m.messageId or ""), rec
    finally:
        m.close()


def extract_zip(zip_path: Path, out_root: Path, log=print) -> Path:
    """그룹웨어에서 내려받은 메일 zip 의 .eml/.msg 를 원본 보관 폴더로 풀어놓는다."""
    import zipfile
    dest = out_root / "imported" / safe_filename(zip_path.stem)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = info.filename
            if not info.flag_bits & 0x800:  # UTF-8 표시가 없으면 한글 윈도우(cp949) 파일명
                try:
                    name = name.encode("cp437").decode("cp949")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
            base = safe_filename(Path(name.replace("\\", "/")).name, 150)  # 폴더 경로는 버리고 파일명만
            if Path(base).suffix.lower() not in (".eml", ".msg") or info.is_dir():
                continue
            target = dest / base
            n = 1
            while target.exists():
                target = dest / f"{Path(base).stem}_{n}{Path(base).suffix}"
                n += 1
            target.write_bytes(z.read(info))
    log(f"  zip 풀기: {zip_path.name} → {dest}")
    return dest


def ingest_folder(conn, cfg: dict, root: str | Path, log=print) -> dict:
    clf = Classifier(cfg)
    root = Path(root)
    if root.is_file() and root.suffix.lower() == ".zip":
        root = extract_zip(root, Path(cfg["archive"].get("eml_dir", "data/eml")), log)
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
