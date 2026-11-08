"""첨부파일 안의 글자 추출 (엑셀/워드/파워포인트/PDF/텍스트).

E.O LIST(엑셀), T/O 결과 보고서(PPT/PDF), 도면 변경 내역 등 첨부 안의 품번·문제점까지 검색하기 위함.
- 오피스 파일(xlsx/docx/pptx)은 추가 설치 없이 읽는다 (zip 안의 XML).
- PDF 는 pypdf 가 설치되어 있을 때만 읽는다 (pip install pypdf).
- 옛날 형식(xls/doc/ppt), 이미지, CAD 파일은 읽지 않는다 (파일명으로만 검색).
"""
from __future__ import annotations

import html
import io
import re
import zipfile
from pathlib import PurePath

MAX_FILE_BYTES = 30 * 1024 * 1024   # 이보다 큰 첨부는 건너뜀
MAX_CHARS_PER_FILE = 100_000
MAX_CHARS_PER_MAIL = 300_000
PDF_MAX_PAGES = 40

SUPPORTED = {".xlsx", ".xlsm", ".docx", ".pptx", ".pdf", ".txt", ".csv"}

_TAG = re.compile(r"<[^>]+>")


def _xml_text(xml: bytes, tag_pattern: bytes) -> list[str]:
    out = []
    for m in re.finditer(tag_pattern, xml, flags=re.DOTALL):
        t = html.unescape(m.group(1).decode("utf-8", "replace"))
        t = _TAG.sub("", t).strip()
        if t:
            out.append(t)
    return out


def _xlsx(data: bytes) -> str:
    parts = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        if "xl/sharedStrings.xml" in names:
            parts += _xml_text(z.read("xl/sharedStrings.xml"), rb"<t[^>]*>(.*?)</t>")
        for n in sorted(x for x in names if x.startswith("xl/worksheets/sheet") and x.endswith(".xml")):
            xml = z.read(n)
            parts += _xml_text(xml, rb"<is>(.*?)</is>")  # 셀에 직접 입력된 글자
            parts += [m.decode() for m in re.findall(rb"<v>([^<]{4,40})</v>", xml)
                      if re.search(rb"[A-Za-z]", m)]   # 숫자 아닌 값(품번 등)
            if sum(len(p) for p in parts) > MAX_CHARS_PER_FILE:
                break
    return "\n".join(parts)


def _docx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml")
    xml = re.sub(rb"</w:p>", b"\n</w:p>", xml)
    return "".join(_xml_text(xml, rb"<w:t[^>]*>(.*?)</w:t>"))


def _pptx(data: bytes) -> str:
    parts = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        slides = [n for n in z.namelist() if re.match(r"ppt/(slides/slide|notesSlides/notesSlide)\d+\.xml$", n)]
        slides.sort(key=lambda n: (("notes" in n), int(re.findall(r"\d+", n)[-1])))
        for n in slides:
            parts.append(" ".join(_xml_text(z.read(n), rb"<a:t>(.*?)</a:t>")))
    return "\n".join(p for p in parts if p)


def _pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    reader = PdfReader(io.BytesIO(data))
    out = []
    for page in reader.pages[:PDF_MAX_PAGES]:
        try:
            out.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(out)


def _plain(data: bytes) -> str:
    for enc in ("utf-8", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def extract_text(filename: str, data: bytes) -> str:
    ext = PurePath(filename or "").suffix.lower()
    if ext not in SUPPORTED or not data or len(data) > MAX_FILE_BYTES:
        return ""
    try:
        text = {".xlsx": _xlsx, ".xlsm": _xlsx, ".docx": _docx, ".pptx": _pptx,
                ".pdf": _pdf, ".txt": _plain, ".csv": _plain}[ext](data)
    except Exception:
        return ""
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text[:MAX_CHARS_PER_FILE]


def combine(items: list[tuple[str, str]]) -> str:
    """[(파일명, 내용)] → '[첨부: 파일명]\n내용' 묶음 (메일당 최대 길이 제한)."""
    out, total = [], 0
    for name, text in items:
        if not text:
            continue
        block = f"[첨부: {name}]\n{text}"
        if total + len(block) > MAX_CHARS_PER_MAIL:
            block = block[: max(0, MAX_CHARS_PER_MAIL - total)]
        out.append(block)
        total += len(block)
        if total >= MAX_CHARS_PER_MAIL:
            break
    return "\n\n".join(out)
