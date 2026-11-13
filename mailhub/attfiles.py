"""메일 첨부파일 꺼내기 (원본 보기 화면에서 열기·저장).

저장된 메일 파일(.eml / .msg)에서 꺼내고, 파일이 없는 예전 메일(PST·아웃룩에서 백업)은
아웃룩(Windows)에서 꺼낸다. 같은 메일을 여러 번 열면 다시 읽지 않도록 최근 몇 통만 메모리에 둔다.
"""
from __future__ import annotations

import os
import re
import threading
from collections import OrderedDict
from pathlib import Path

_cache: "OrderedDict[int, dict]" = OrderedDict()
_lock = threading.Lock()
CACHE_N = 6
INLINE = re.compile(r"^(image\d{3}|~WRD\d+|oledata)\.\w+$", re.I)   # 본문에 붙여 넣은 그림 (첨부 아님)


def _safe(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name or "file").strip(" .") or "file"
    return name[:150]


def load(conn, mail_id: int, use_outlook: bool | None = None) -> dict:
    """{'source': eml|msg|outlook|none, 'items': [(이름, 바이트, 본문그림여부)], 'message': 사유}"""
    with _lock:
        if mail_id in _cache:
            _cache.move_to_end(mail_id)
            return _cache[mail_id]
    from .mold import _mail_attachments
    m = conn.execute("SELECT file_path, entry_id FROM mails WHERE id=?", (mail_id,)).fetchone()
    if not m:
        raise KeyError(f"#{mail_id} 메일이 없습니다")
    fp = m["file_path"] or ""
    has_file = bool(fp) and Path(fp).exists() and fp.lower().endswith((".eml", ".msg"))
    if use_outlook is None:
        use_outlook = os.name == "nt"
    out = {"source": "none", "items": [], "message": ""}
    try:
        if has_file:
            items = _mail_attachments(conn, mail_id, False)
            out["source"] = Path(fp).suffix.lower().lstrip(".")
        elif m["entry_id"] and use_outlook:
            items = _mail_attachments(conn, mail_id, True)
            out["source"] = "outlook"
        else:
            items = []
            out["message"] = ("저장된 메일 파일이 없습니다. 이 PC 의 아웃룩에 해당 PST 가 연결되어 있으면 아웃룩에서 열어 주세요"
                              if m["entry_id"] else "저장된 메일 파일이 없어 첨부를 꺼낼 수 없습니다")
        out["items"] = [(n, d, bool(INLINE.match(n or ""))) for n, d in items]
    except Exception as e:  # 아웃룩·PST 연결 문제 등은 화면에 사유로
        out["message"] = f"첨부를 꺼내지 못했습니다: {str(e)[:200]}"
    with _lock:
        _cache[mail_id] = out
        while len(_cache) > CACHE_N:
            _cache.popitem(last=False)
    return out


def listing(conn, mail_id: int) -> dict:
    r = load(conn, mail_id)
    return {"source": r["source"], "message": r["message"],
            "items": [{"n": i, "name": n, "size": len(d), "inline": inline} for i, (n, d, inline) in enumerate(r["items"])]}


def get(conn, mail_id: int, n: int) -> tuple[str, bytes]:
    items = load(conn, mail_id)["items"]
    if not 0 <= n < len(items):
        raise KeyError("첨부파일이 없습니다")
    name, data, _ = items[n]
    return name, data


def open_attachment(conn, cfg: dict, mail_id: int, n: int) -> str:
    """임시 폴더(data/attach_open/<메일번호>/)에 저장하고 이 PC 의 기본 프로그램(엑셀·PPT·PDF …)으로 연다."""
    name, data = get(conn, mail_id, n)
    d = Path(cfg["archive"]["db_path"]).parent / "attach_open" / str(mail_id)
    d.mkdir(parents=True, exist_ok=True)
    p = d / _safe(name)
    p.write_bytes(data)
    if os.name == "nt":
        os.startfile(str(p))  # type: ignore[attr-defined]
        return f"'{name}' 을(를) 열었습니다"
    return f"저장했습니다: {p}"
