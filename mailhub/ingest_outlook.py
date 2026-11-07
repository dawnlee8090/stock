"""실행 중인 아웃룩(Windows)에서 직접 메일을 읽어 DB 로 가져온다.

- 회사 서버 사서함 + 아웃룩에 연결된 PST 백업 파일을 모두 읽는다.
- --pst 로 아직 연결 안 된 PST 파일을 추가로 열 수 있다.
- --save-msg 를 주면 메일 원본을 .msg 로 따로 보관한다
  (서버 용량 때문에 삭제해도 원본이 남는다).
필요: Windows + Outlook 데스크톱 + `pip install pywin32`
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from . import db
from .classify import Classifier
from .textutil import INLINE_IMAGE, clean_body, safe_filename

OL_MAIL = 43            # olMail
# 원본 보관 형식: 회사 보안(DRM) 정책이나 아웃룩 버전에 따라 일부 형식이 막혀 있을 수 있어 차례로 시도
SAVE_FORMATS = [(9, ".msg"), (3, ".msg"), (10, ".mht"), (5, ".html")]  # olMSGUnicode, olMSG, olMHTML, olHTML
PR_MESSAGE_ID = "http://schemas.microsoft.com/mapi/proptag/0x1035001F"
PR_SMTP = "http://schemas.microsoft.com/mapi/proptag/0x39FE001F"
SKIP_FOLDERS = {"삭제된 항목", "deleted items", "정크 메일", "junk email", "junk e-mail",
                "동기화 문제", "sync issues", "rss 피드", "rss feeds", "대화 기록", "conversation history"}


def _get(obj, attr, default=""):
    try:
        v = getattr(obj, attr)
        return default if v is None else v
    except Exception:
        return default


def _dt(v) -> str:
    try:
        if getattr(v, "tzinfo", None) is not None:  # pywin32 는 UTC 로 돌려줌 → PC 현지 시각으로 변환
            v = v.astimezone()
        return datetime(v.year, v.month, v.day, v.hour, v.minute, v.second).isoformat()
    except Exception:
        return ""


def _sender_email(item) -> str:
    addr = _get(item, "SenderEmailAddress")
    if _get(item, "SenderEmailType") == "EX":  # 사내 Exchange 주소 → SMTP 주소로 변환
        try:
            addr = item.Sender.GetExchangeUser().PrimarySmtpAddress or addr
        except Exception:
            try:
                addr = item.Sender.PropertyAccessor.GetProperty(PR_SMTP) or addr
            except Exception:
                pass
    return (addr or "").lower()


def _message_id(item) -> str:
    try:
        return item.PropertyAccessor.GetProperty(PR_MESSAGE_ID) or ""
    except Exception:
        return ""


def _restricted_items(folder, since: datetime | None):
    items = folder.Items
    if since:
        try:
            flt = ('@SQL="urn:schemas:httpmail:datereceived" >= \''
                   + since.strftime("%Y-%m-%d %H:%M") + "'")
            items = items.Restrict(flt)
        except Exception:
            pass
    return items


def ingest_outlook(conn, cfg: dict, store_filter: str | None = None, pst_paths=(),
                   since: datetime | None = None, save_msg: bool = False,
                   save_attachments: bool = False, folder_filter: str | None = None,
                   log=print) -> dict:
    try:
        import win32com.client  # type: ignore
    except ImportError as e:
        raise RuntimeError("아웃룩 연동은 Windows 에서 'pip install pywin32' 후 사용 가능합니다.") from e

    clf = Classifier(cfg)
    msg_dir = Path(cfg["archive"]["msg_dir"])
    att_dir = Path(cfg["archive"]["attach_dir"])
    ns = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
    for p in pst_paths:
        log(f"PST 연결: {p}")
        ns.AddStore(os.path.abspath(p))

    stats = {"folders": 0, "seen": 0, "added": 0, "skipped": 0, "errors": 0, "save_failed": 0}
    save_state = {"formats": list(SAVE_FORMATS), "fail_streak": 0, "enabled": save_msg}

    def save_copy(item, base: Path) -> str:
        """원본 사본 저장. 성공한 형식을 기억해 다음부터 그 형식을 먼저 쓴다."""
        last_err = None
        for fmt, ext in save_state["formats"]:
            path = base.with_suffix(ext)
            try:
                item.SaveAs(str(path.resolve()), fmt)
                save_state["formats"].remove((fmt, ext))
                save_state["formats"].insert(0, (fmt, ext))
                save_state["fail_streak"] = 0
                return str(path.resolve())
            except Exception as e:
                last_err = e
        stats["save_failed"] += 1
        save_state["fail_streak"] += 1
        if save_state["fail_streak"] == 1:
            log(f"  [원본 저장 실패] 메일 내용은 정상 저장됨. 사유: {last_err}")
        if save_state["fail_streak"] >= 20:
            save_state["enabled"] = False
            log("  [안내] 원본 저장이 20건 연속 실패해서 원본 보관을 끕니다 (회사 보안 정책으로 막힌 것으로 보임)."
                " 메일 내용 수집과 검색은 계속 됩니다.")
        return ""

    def handle(item, folder_path: str, store_id: str):
        if _get(item, "Class", 0) != OL_MAIL:
            return
        stats["seen"] += 1
        subject = _get(item, "Subject")
        sender = _sender_email(item)
        sent_at = _dt(_get(item, "ReceivedTime", None)) or _dt(_get(item, "SentOn", None))
        body = clean_body(_get(item, "Body"))
        key = db.make_key(_message_id(item), sender, sent_at, subject, body)
        if db.exists(conn, key):
            # 이미 있음 → 원본 링크 정보만 보충
            db.insert_mail(conn, key, {"entry_id": _get(item, "EntryID"), "store_id": store_id})
            stats["skipped"] += 1
            return
        att_names = []
        try:
            for a in item.Attachments:
                n = _get(a, "FileName")
                if n and not INLINE_IMAGE.match(n):
                    att_names.append(n)
        except Exception:
            pass
        rec = {
            "source": "outlook", "folder": folder_path, "subject": subject,
            "sender_name": _get(item, "SenderName"), "sender_email": sender,
            "to_addr": _get(item, "To"), "cc_addr": _get(item, "CC"),
            "sent_at": sent_at, "body": body, "attachments": "\n".join(att_names),
            "entry_id": _get(item, "EntryID"), "store_id": store_id,
        }
        rec.update(clf.classify(rec))
        new_id = db.insert_mail(conn, key, rec)
        if new_id is None:
            stats["skipped"] += 1
            return
        stats["added"] += 1
        ym = (sent_at[:7] or "unknown").replace("-", "/")
        if save_state["enabled"]:
            base = msg_dir / ym / f"{new_id}_{safe_filename(subject)}"
            base.parent.mkdir(parents=True, exist_ok=True)
            saved = save_copy(item, base)
            if saved:
                conn.execute("UPDATE mails SET file_path=? WHERE id=?", (saved, new_id))
        if save_attachments and att_names:
            d = att_dir / ym / str(new_id)
            d.mkdir(parents=True, exist_ok=True)
            for a in item.Attachments:
                n = _get(a, "FileName")
                if n and not INLINE_IMAGE.match(n):
                    try:
                        a.SaveAsFile(str((d / safe_filename(n, 150)).resolve()))
                    except Exception:
                        pass

    def walk(folder, path: str, store_id: str):
        name = _get(folder, "Name")
        if name.lower() in SKIP_FOLDERS:
            return
        if not folder_filter or folder_filter.lower() in path.lower():
            stats["folders"] += 1
            n_before = stats["seen"]
            try:
                for item in _restricted_items(folder, since):
                    try:
                        handle(item, path, store_id)
                    except Exception as e:
                        stats["errors"] += 1
                        log(f"  [오류] {path}: {e}")
                    if stats["seen"] and stats["seen"] % 500 == 0 and stats["seen"] != save_state.get("last_log"):
                        save_state["last_log"] = stats["seen"]
                        conn.commit()
                        log(f"    ... {path}: 진행 중 (전체 {stats['seen']:,}건 확인, 추가 {stats['added']:,})")
            except Exception as e:
                log(f"  [폴더 읽기 실패] {path}: {e}")
            if stats["seen"] > n_before:
                log(f"  {path}: {stats['seen'] - n_before}건 확인 (누적 추가 {stats['added']})")
        try:
            for sub in folder.Folders:
                walk(sub, f"{path}/{_get(sub, 'Name')}", store_id)
        except Exception:
            pass

    for root in ns.Folders:
        rname = _get(root, "Name")
        if store_filter and store_filter.lower() not in rname.lower():
            continue
        log(f"[사서함] {rname}")
        walk(root, rname, _get(root, "StoreID"))
    conn.commit()
    return stats


def list_stores(log=print):
    import win32com.client  # type: ignore
    ns = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
    for root in ns.Folders:
        log(f"- {root.Name}")


def open_in_outlook(entry_id: str, store_id: str) -> bool:
    try:
        import win32com.client  # type: ignore
        ns = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
        ns.GetItemFromID(entry_id, store_id or None).Display()
        return True
    except Exception:
        return False
