"""이미 수집된 메일의 첨부파일 내용을 추출해 검색 인덱스에 넣는다.

- 원본(.eml/.msg)이 보관된 메일: 원본에서 첨부를 읽는다.
- 아웃룩(PST)에서 가져온 메일: --outlook 옵션 시 아웃룩에서 첨부를 임시 폴더에 저장해 읽고 바로 지운다.
- 중간에 멈춰도 다시 실행하면 이어서 진행한다 (att_indexed 표시).
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

from . import attach
from .ingest_files import parse_eml_bytes, parse_msg
from .textutil import INLINE_IMAGE, safe_filename


def _supported_names(names: str) -> bool:
    return any(Path(n).suffix.lower() in attach.SUPPORTED for n in (names or "").splitlines())


def _from_outlook(ns, entry_id: str, store_id: str, tmpdir: Path) -> str:
    item = ns.GetItemFromID(entry_id, store_id or None)
    items = []
    for a in item.Attachments:
        fname = a.FileName or ""
        if not fname or INLINE_IMAGE.match(fname) or Path(fname).suffix.lower() not in attach.SUPPORTED:
            continue
        if (a.Size or 0) > attach.MAX_FILE_BYTES:
            continue
        p = tmpdir / safe_filename(fname, 120)
        a.SaveAsFile(str(p))
        try:
            items.append((fname, attach.extract_text(fname, p.read_bytes())))
        finally:
            p.unlink(missing_ok=True)
    return attach.combine(items)


def index_attachments(conn, cfg: dict, since: str | None = None, use_outlook: bool = False,
                      limit: int = 0, log=print) -> dict:
    rows = conn.execute(
        "SELECT id, file_path, entry_id, store_id, attachments FROM mails "
        "WHERE att_indexed = 0 AND attachments != '' AND sent_at >= ? ORDER BY sent_at DESC",
        (since or "",)).fetchall()
    stats = {"target": 0, "done": 0, "with_text": 0, "no_source": 0, "errors": 0, "interrupted": False}

    ns = None
    if use_outlook:
        try:
            import win32com.client  # type: ignore
            ns = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
        except Exception as e:
            raise SystemExit(f"아웃룩 연결 실패: {e}") from None

    todo = []
    for r in rows:
        if _supported_names(r["attachments"]):
            todo.append(r)
        else:  # 읽을 수 있는 형식의 첨부가 없음 (이미지/CAD 등) → 처리 완료로 표시
            conn.execute("UPDATE mails SET att_indexed = 1 WHERE id = ?", (r["id"],))
    conn.commit()
    if limit:
        todo = todo[:limit]
    stats["target"] = len(todo)
    log(f"첨부 내용 추출 대상: {len(todo):,}건 (엑셀/워드/PPT/PDF/텍스트 첨부가 있는 메일)")

    last_log = time.monotonic()
    outlook_fail_streak = 0
    with tempfile.TemporaryDirectory(prefix="mailhub_att_") as td:
        tmpdir = Path(td)
        try:
            for i, r in enumerate(todo, 1):
                text, ok = "", False
                fp = r["file_path"] or ""
                try:
                    if fp and Path(fp).exists() and fp.lower().endswith(".eml"):
                        text = parse_eml_bytes(Path(fp).read_bytes())[1]["att_text"]
                        ok = True
                    elif fp and Path(fp).exists() and fp.lower().endswith(".msg"):
                        text = parse_msg(Path(fp))[1]["att_text"]
                        ok = True
                    elif ns is not None and r["entry_id"]:
                        text = _from_outlook(ns, r["entry_id"], r["store_id"], tmpdir)
                        ok = True
                        outlook_fail_streak = 0
                    else:
                        stats["no_source"] += 1
                except Exception as e:
                    stats["errors"] += 1
                    if ns is not None and r["entry_id"]:
                        outlook_fail_streak += 1
                        if outlook_fail_streak == 1:
                            log(f"  [아웃룩 첨부 읽기 실패] #{r['id']}: {e}")
                        if outlook_fail_streak >= 20:
                            log("  [안내] 아웃룩 첨부 읽기가 20건 연속 실패해서 중단합니다 "
                                "(PST 가 아웃룩에 연결되어 있는지, 회사 보안 정책으로 막혔는지 확인).")
                            break
                if ok:
                    conn.execute("UPDATE mails SET att_text = ?, att_indexed = 1 WHERE id = ?", (text, r["id"]))
                    stats["done"] += 1
                    stats["with_text"] += int(bool(text))
                if i % 20 == 0:
                    conn.commit()
                if time.monotonic() - last_log >= 10:
                    last_log = time.monotonic()
                    log(f"    ... {i:,}/{len(todo):,} (내용 추출 {stats['with_text']:,})")
        except KeyboardInterrupt:
            stats["interrupted"] = True
            log("\n[중단] 여기까지 저장했습니다. 같은 명령을 다시 실행하면 이어서 진행합니다.")
    conn.commit()
    return stats
