"""규칙 기반 자동 분류: 태그, 품번, 내 메일 여부, 조치 필요 여부, 스레드 키."""
from __future__ import annotations

import re

SENT_FOLDERS = ("보낸 편지함", "sent items", "sent mail", "보낸편지함")
QUOTE_MARKERS = [r"\n-+\s*Original Message\s*-+", r"\n-+\s*원본 메시지\s*-+",
                 r"\nFrom:\s.+\n(Sent|Date):", r"\n보낸 사람:\s", r"\n보낸사람:\s", r"\n발신:\s",
                 r"\n-+\s*Forwarded message\s*-+", r"\nOn .{5,80} wrote:"]
SUBJECT_PREFIX = re.compile(r"^\s*((re|fw|fwd|회신|답장|전달|\[ext\]|\[외부\])\s*(\[\d+\])?\s*[:：]?\s*)+",
                            re.IGNORECASE)


def thread_key(subject: str) -> str:
    """RE:/FW:/전달: 등을 떼어낸 제목 → 같은 건(스레드) 묶음 키."""
    s = subject or ""
    for _ in range(5):
        new = SUBJECT_PREFIX.sub("", s)
        if new == s:
            break
        s = new
    return re.sub(r"\s+", " ", s).strip().lower()


def latest_part(body: str) -> str:
    """회신/전달 메일에서 인용된 이전 메일을 잘라 최신 본문만 반환."""
    cut = len(body)
    for m in QUOTE_MARKERS:
        hit = re.search(m, body, flags=re.IGNORECASE)
        if hit:
            cut = min(cut, hit.start())
    return body[:cut]


def _matcher(keywords):
    """영문/숫자 키워드는 단어 단위로만 일치 (예: ECO 가 second 안에서 잡히지 않게), 한글은 부분 일치."""
    plain, rx = [], []
    for k in keywords:
        k = k.lower()
        if re.fullmatch(r"[a-z0-9 ./\-]+", k):
            rx.append(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])")
        else:
            plain.append(k)
    compiled = re.compile("|".join(rx)) if rx else None
    return lambda text: any(k in text for k in plain) or bool(compiled and compiled.search(text))


class Classifier:
    def __init__(self, cfg: dict):
        self.tags = {name: _matcher(kws) for name, kws in cfg.get("tags", {}).items()}
        self.action = _matcher(cfg.get("action", {}).get("keywords", []))
        self.part_res = [re.compile(p) for p in cfg.get("part_number", {}).get("patterns", [])]
        me = cfg.get("me", {})
        self.my_addrs = {a.lower() for a in me.get("addresses", []) if a and "company.com" not in a}
        self.my_names = [n for n in me.get("names", []) if n]

    def _addressed_to_me(self, to_addr: str) -> bool:
        """받는사람(To)에 내가 있는지. 설정이 없으면 판단 불가 → True."""
        if not (self.my_addrs or self.my_names):
            return True
        t = (to_addr or "").lower()
        return any(a in t for a in self.my_addrs) or any(n.lower() in t for n in self.my_names)

    def classify(self, rec: dict) -> dict:
        subject = rec.get("subject", "") or ""
        body = rec.get("body", "") or ""
        attach = rec.get("attachments", "") or ""
        latest = latest_part(body)
        people = " ".join(rec.get(k, "") or "" for k in ("sender_name", "sender_email", "to_addr", "cc_addr"))

        # 태그는 제목 + 최신 본문 + 첨부파일명 + 주소 기준 (인용된 이전 메일·긴 이력은 제외)
        text = f"{subject}\n{latest}\n{attach}\n{people}".lower()
        tags = [name for name, match in self.tags.items() if match(text)]

        # 품번은 전체 본문에서 (이력 추적용)
        part_nos: list[str] = []
        for rx in self.part_res:
            for m in rx.findall(f"{subject}\n{body}\n{attach}"):
                m = m if isinstance(m, str) else m[0]
                if m not in part_nos:
                    part_nos.append(m)

        sender = (rec.get("sender_email") or "").lower()
        folder = (rec.get("folder") or "").lower()
        is_mine = int(sender in self.my_addrs or any(f in folder for f in SENT_FOLDERS))

        # 조치 필요: 내가 보낸 메일 X, 내가 받는사람(To)에 있음 (참조만이면 X), 제목/최신 본문에 요청 키워드
        tkey = thread_key(subject)  # '회신:' 같은 말머리는 빼고 판단
        latest_l = f"{tkey}\n{latest[:1500]}".lower()
        needs = int(not is_mine and self._addressed_to_me(rec.get("to_addr", "")) and self.action(latest_l))

        return {"tags": ",".join(tags), "part_nos": ",".join(part_nos[:30]), "needs_action": needs,
                "is_mine": is_mine, "thread_key": tkey}
