"""E.O 항목별 참고 이미지 (도면 캡처, PPT 슬라이드, 사진) 모으기.

출처
  - 연결된 메일의 원본(.eml/.msg): 본문에 붙여 넣은 캡처, 첨부 이미지
  - 첨부 PPT: 파워포인트가 있으면 슬라이드를 그림으로 저장, 없으면 PPT 안의 그림만 추출
  - 첨부 엑셀: 엑셀 안에 붙여 넣은 그림
  - 아웃룩(PST) 메일: --outlook 시 아웃룩에서 첨부를 읽음
  - 직접 캡처: Win+Shift+S 로 캡처 후 `eo img <번호> --paste` (클립보드), 또는 --add 파일
작은 이미지(서명 로고·아이콘)는 건너뛰고, 같은 그림은 한 번만 저장한다.
"""
from __future__ import annotations

import email
import hashlib
import io
import os
import re
import tempfile
import zipfile
from datetime import datetime
from email import policy
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS eo_images (
    id INTEGER PRIMARY KEY, eo_id INTEGER, kind TEXT DEFAULT 'eo', path TEXT, source TEXT, caption TEXT, mail_id INTEGER,
    sha1 TEXT, sort INTEGER DEFAULT 0, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_eo_images ON eo_images(eo_id, sort);
"""

IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff"}
MIN_BYTES = 12 * 1024          # 이보다 작은 그림은 로고·아이콘으로 보고 제외
MIN_W, MIN_H = 300, 180
MAX_SIDE = 1600                # 저장 시 긴 변 최대 픽셀
MAX_PER_RUN = 20
PPT_MAX_SLIDES = 10


# 종류별: (번호 앞글자, 메일 연결 테이블, 연결 테이블의 항목 열)
KINDS = {"eo": ("E", "eo_mails", "eo_id"), "to": ("T", "to_mails", "to_id"),
         "lm": ("L", "lm_mails", "issue_id")}   # lm = 현지 금형 문제점 (local.py)


def ensure(conn) -> None:
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(eo_images)")}
    if "kind" not in cols:  # E.O 전용이던 이전 버전 → T/O 문제점 사진도 같이 저장
        conn.execute("ALTER TABLE eo_images ADD COLUMN kind TEXT DEFAULT 'eo'")
    # 현지 금형 문제점: 수정 전/후 칸(grp), 표시(marks, 비율 좌표 JSON), 표시를 그려 넣은 사본, 크기
    for col, typ in (("grp", "TEXT DEFAULT ''"), ("marks", "TEXT DEFAULT ''"), ("marked_path", "TEXT DEFAULT ''"),
                     ("w", "INTEGER DEFAULT 0"), ("h", "INTEGER DEFAULT 0")):
        if col not in cols:
            conn.execute(f"ALTER TABLE eo_images ADD COLUMN {col} {typ}")
    conn.commit()


def img_dir(cfg: dict, eid: int, kind: str = "eo") -> Path:
    return Path(cfg["archive"].get("eo_image_dir", "data/eo_images")) / f"{KINDS[kind][0]}{eid}"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _normalize(data: bytes):
    """이미지 바이트 → (PIL.Image, sha1) 또는 None (작거나 열 수 없음)."""
    if len(data) < MIN_BYTES:
        return None
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        return None
    if im.width < MIN_W or im.height < MIN_H:
        return None
    if im.mode not in ("RGB", "L"):
        bg = Image.new("RGB", im.size, "white")
        try:
            bg.paste(im, mask=im.convert("RGBA").split()[-1])
        except Exception:
            bg.paste(im.convert("RGB"))
        im = bg
    if max(im.size) > MAX_SIDE:
        im.thumbnail((MAX_SIDE, MAX_SIDE))
    return im, hashlib.sha1(data).hexdigest()


def save_image(conn, cfg: dict, eid: int, data: bytes, source: str, caption: str, mail_id: int | None = None,
               force: bool = False, kind: str = "eo") -> int | None:
    ensure(conn)
    norm = _normalize(data) if not force else None
    if force:
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(data))
            im.load()
            im = im.convert("RGB")
            if max(im.size) > MAX_SIDE:
                im.thumbnail((MAX_SIDE, MAX_SIDE))
            norm = (im, hashlib.sha1(data).hexdigest())
        except Exception:
            return None
    if not norm:
        return None
    im, sha = norm
    if conn.execute("SELECT 1 FROM eo_images WHERE eo_id=? AND kind=? AND sha1=?", (eid, kind, sha)).fetchone():
        return None
    d = img_dir(cfg, eid, kind)
    d.mkdir(parents=True, exist_ok=True)
    n = (conn.execute("SELECT COALESCE(MAX(ABS(sort)), 0) FROM eo_images WHERE eo_id=? AND kind=?",
                      (eid, kind)).fetchone()[0] or 0) + 1
    p = d / f"{n:02d}_{source}.png"
    im.save(p, "PNG", optimize=True)
    cur = conn.execute("INSERT INTO eo_images (eo_id, kind, path, source, caption, mail_id, sha1, sort, created_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?)", (eid, kind, str(p.resolve()), source, caption, mail_id, sha, n,
                                                      _now()))
    conn.commit()
    return cur.lastrowid


# ---------- 첨부 안의 그림 ----------
def _zip_media(data: bytes, prefix: str) -> list[tuple[str, bytes]]:
    out = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for n in sorted(z.namelist()):
                if n.startswith(prefix) and Path(n).suffix.lower() in IMG_EXT:
                    out.append((Path(n).name, z.read(n)))
    except Exception:
        pass
    return out


def _render_ppt(data: bytes, fname: str) -> list[tuple[str, bytes]]:
    """파워포인트(Windows)로 슬라이드를 PNG 로. 실패하면 빈 목록."""
    if os.name != "nt":
        return []
    try:
        import win32com.client  # type: ignore
    except ImportError:
        return []
    out = []
    with tempfile.TemporaryDirectory(prefix="mailhub_ppt_") as td:
        src = Path(td) / ("src" + Path(fname).suffix.lower())
        src.write_bytes(data)
        app = None
        pres = None
        try:
            app = win32com.client.Dispatch("PowerPoint.Application")
            was_open = app.Presentations.Count
            pres = app.Presentations.Open(str(src), True, False, False)  # ReadOnly, Untitled, WithWindow
            for i in range(1, min(pres.Slides.Count, PPT_MAX_SLIDES) + 1):
                p = Path(td) / f"s{i}.png"
                pres.Slides(i).Export(str(p), "PNG", 1600, 900)
                out.append((f"{fname} 슬라이드{i}", p.read_bytes()))
        except Exception:
            return out
        finally:
            try:
                if pres is not None:
                    pres.Close()
                if app is not None and not was_open:
                    app.Quit()
            except Exception:
                pass
    return out


def images_from_attachment(fname: str, data: bytes, use_powerpoint: bool = True) -> list[tuple[str, bytes, str]]:
    """첨부 1개 → [(설명, 이미지바이트, 출처)]"""
    ext = Path(fname or "").suffix.lower()
    if ext in IMG_EXT:
        return [(fname, data, "mail")]
    if ext in (".pptx", ".ppt"):
        slides = _render_ppt(data, fname) if use_powerpoint else []
        if slides:
            return [(n, b, "ppt") for n, b in slides]
        return [(f"{fname} 그림:{n}", b, "ppt") for n, b in _zip_media(data, "ppt/media/")] if ext == ".pptx" else []
    if ext in (".xlsx", ".xlsm"):
        return [(f"{fname} 그림:{n}", b, "excel") for n, b in _zip_media(data, "xl/media/")]
    if ext == ".docx":
        return [(f"{fname} 그림:{n}", b, "word") for n, b in _zip_media(data, "word/media/")]
    return []


def _from_eml(raw: bytes, use_powerpoint: bool) -> list[tuple[str, bytes, str]]:
    msg = email.message_from_bytes(raw, policy=policy.default)
    out = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        fname = part.get_filename() or ""
        if not fname and ctype.startswith("image/"):
            fname = "본문그림." + ctype.split("/")[1]
        if not fname:
            continue
        try:
            data = part.get_payload(decode=True) or b""
        except Exception:
            continue
        for cap, b, src in images_from_attachment(fname, data, use_powerpoint):
            out.append(("본문 캡처" if fname.startswith("본문그림") or re.match(r"image\d+\.", fname, re.I) else cap,
                        b, src))
    return out


def _from_msg(path: Path, use_powerpoint: bool) -> list[tuple[str, bytes, str]]:
    try:
        import extract_msg
    except ImportError:
        return []
    out = []
    m = extract_msg.openMsg(str(path))
    try:
        for a in m.attachments:
            fname = getattr(a, "longFilename", None) or getattr(a, "shortFilename", None) or ""
            data = getattr(a, "data", None)
            if fname and isinstance(data, bytes):
                out += images_from_attachment(fname, data, use_powerpoint)
    finally:
        m.close()
    return out


def _from_outlook(ns, entry_id: str, store_id: str, use_powerpoint: bool) -> list[tuple[str, bytes, str]]:
    item = ns.GetItemFromID(entry_id, store_id or None)
    out = []
    with tempfile.TemporaryDirectory(prefix="mailhub_img_") as td:
        for i, a in enumerate(item.Attachments, 1):
            fname = a.FileName or ""
            ext = Path(fname).suffix.lower()
            if ext not in IMG_EXT | {".pptx", ".ppt", ".xlsx", ".xlsm", ".docx"}:
                continue
            p = Path(td) / f"{i}{ext}"
            try:
                a.SaveAsFile(str(p))
                out += images_from_attachment(fname, p.read_bytes(), use_powerpoint)
            except Exception:
                continue
    return out


def collect(conn, cfg: dict, eid: int, use_outlook: bool = False, use_powerpoint: bool = True,
            log=print, kind: str = "eo") -> int:
    """E.O 항목에 연결된 메일에서 그림을 모은다. 새로 저장한 개수 반환."""
    ensure(conn)
    prefix, link_table, link_col = KINDS[kind]
    mails = conn.execute(
        f"SELECT m.id, m.sent_at, m.subject, m.file_path, m.entry_id, m.store_id FROM {link_table} em "
        f"JOIN mails m ON m.id = em.mail_id WHERE em.{link_col} = ? ORDER BY m.sent_at", (eid,)).fetchall()
    ns = None
    if use_outlook:
        try:
            import win32com.client  # type: ignore
            ns = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
        except Exception as e:
            log(f"  아웃룩 연결 실패: {e}")
    added = 0
    for m in mails:
        fp = m["file_path"] or ""
        try:
            if fp and Path(fp).exists() and fp.lower().endswith(".eml"):
                items = _from_eml(Path(fp).read_bytes(), use_powerpoint)
            elif fp and Path(fp).exists() and fp.lower().endswith(".msg"):
                items = _from_msg(Path(fp), use_powerpoint)
            elif ns is not None and m["entry_id"]:
                items = _from_outlook(ns, m["entry_id"], m["store_id"], use_powerpoint)
            else:
                continue
        except Exception as e:
            log(f"  #{m['id']} 그림 읽기 실패: {e}")
            continue
        for cap, data, src in items:
            caption = f"#{m['id']} {m['sent_at'][5:10].replace('-', '/')} {cap}"
            if save_image(conn, cfg, eid, data, src, caption, m["id"], kind=kind):
                added += 1
                if added >= MAX_PER_RUN:
                    log(f"  {prefix}{eid}: 그림이 많아 {MAX_PER_RUN}장까지만 저장 (필요 없는 그림은 --remove)")
                    return added
    return added


def add_files(conn, cfg: dict, eid: int, files: list[str], caption: str = "", kind: str = "eo") -> int:
    n = 0
    for f in files:
        p = Path(f)
        if not p.exists():
            continue
        items = images_from_attachment(p.name, p.read_bytes())
        for cap, data, src in items:
            if save_image(conn, cfg, eid, data, "file", caption or cap, force=(src == "mail"), kind=kind):
                n += 1
    return n


def paste(conn, cfg: dict, eid: int, caption: str = "", kind: str = "eo") -> bool:
    """클립보드의 캡처 이미지 (Win+Shift+S) 를 저장."""
    try:
        from PIL import ImageGrab
        clip = ImageGrab.grabclipboard()
    except Exception as e:
        raise RuntimeError(f"클립보드를 읽을 수 없습니다: {e}") from e
    if clip is None:
        raise RuntimeError("클립보드에 그림이 없습니다. Win+Shift+S 로 캡처한 뒤 다시 실행하세요.")
    if isinstance(clip, list):  # 탐색기에서 파일을 복사한 경우
        return add_files(conn, cfg, eid, clip, caption, kind) > 0
    buf = io.BytesIO()
    clip.convert("RGB").save(buf, "PNG")
    return save_image(conn, cfg, eid, buf.getvalue(), "capture",
                      caption or f"캡처 {datetime.now():%m/%d %H:%M}", force=True, kind=kind) is not None


def listing(conn, eid: int, kind: str = "eo") -> list:
    ensure(conn)
    return conn.execute("SELECT * FROM eo_images WHERE eo_id=? AND kind=? AND sort > 0 ORDER BY sort",
                        (eid, kind)).fetchall()


def remove(conn, eid: int, sort_no: int, kind: str = "eo") -> bool:
    ensure(conn)
    r = conn.execute("SELECT * FROM eo_images WHERE eo_id=? AND kind=? AND sort=?", (eid, kind, sort_no)).fetchone()
    if not r:
        return False
    try:
        Path(r["path"]).unlink(missing_ok=True)
    except Exception:
        pass
    # 기록은 숨김으로 남겨서 다시 모을 때 같은 그림이 또 들어오지 않게 한다
    conn.execute("UPDATE eo_images SET sort = -sort, path = '' WHERE id=?", (r["id"],))
    conn.commit()
    return True
