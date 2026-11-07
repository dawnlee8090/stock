from __future__ import annotations

import html
import re
from datetime import datetime


def html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", s or "")
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


def clean_body(s: str) -> str:
    s = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t\xa0]+\n", "\n", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def iso(dt: datetime | None) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)  # 로컬 시각으로 통일
    return dt.replace(microsecond=0).isoformat()


INLINE_IMAGE = re.compile(r"^image\d{3}\.(png|jpe?g|gif|emz|wmz)$", re.IGNORECASE)


def safe_filename(s: str, maxlen: int = 80) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", s or "").strip(" .")
    return (s or "noname")[:maxlen]
