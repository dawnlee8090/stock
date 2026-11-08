"""규칙 기반 자동 분류: 태그, 품번, 내 메일 여부, 조치 필요 여부, 스레드 키."""
from __future__ import annotations

import re

SENT_FOLDERS = ("보낸 편지함", "sent items", "sent mail", "보낸편지함", "[보낸메일]")
QUOTE_MARKERS = [r"\n-+\s*Original Message\s*-+", r"\n-+\s*원본 메시지\s*-+",
                 r"\nFrom:\s.+\n(Sent|Date):", r"\n보낸 사람:\s", r"\n보낸사람:\s", r"\n발신:\s",
                 r"\n-+\s*Forwarded message\s*-+", r"\nOn .{5,80} wrote:"]
# 인용된 이전 메일의 머리글 줄 (From: / 보낸 사람: / 발신: ...)
QUOTED_FROM = re.compile(r"^[\s>]*(?:from|sender|보낸\s*사람|발신자?|보낸이)\s*:\s*(.+)$",
                         re.IGNORECASE | re.MULTILINE)
SUBJECT_PREFIX = re.compile(r"^\s*((re|fw|fwd|회신|답장|전달|\[ext\]|\[외부\])\s*(\[\d+\])?\s*[:：]?\s*)+",
                            re.IGNORECASE)


# 차종 코드: 영문 대문자 2 + 숫자 1~2 + (소문자 1)  예) BC4t, BC4, GN7, NX4a, SG2
CAR_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2}\d{1,2}[a-z]?)(?![A-Za-z0-9])")
CAR_NEXT = re.compile(r"\s*(?:CUV|SUV|[2-5]DR|HEV|PHEV|EV|N|N-LINE|NLINE|PE|FL|HB|CKD|ICE|차종|차량|파생|프로젝트)(?![A-Za-z0-9])",
                      re.IGNORECASE)
# 차종이 아닌 2글자 약어 (이벤트·재질·부서 등)
CAR_IGNORE = {"LP", "SP", "AP", "PP", "TD", "GF", "CF", "KD", "EO", "TO", "PV", "DV", "MP", "PR", "QA", "QC", "RR", "FR",
              "LH", "RH", "MS", "ES", "TS", "IP", "DR", "CP", "PC", "PA", "PE", "UV", "EV", "BL", "PO", "SR", "NG", "OK"}


def car_key(code: str) -> str:
    """BC4t / BC4b / BC4 → BC4 (끝 소문자만 다른 표기는 같은 차종으로)."""
    return re.sub(r"[a-z]+$", "", code).upper()


def find_cars(subject: str, body: str = "", known: set[str] | None = None, ignore: set[str] | None = None) -> list[str]:
    """제목(없으면 본문 앞부분)에서 차종 코드. 대괄호 안·제목 맨 앞·차종 단어(CUV/5DR/HEV/PE/차종…) 앞일 때만 인정."""
    known = known or set()
    ignore = (ignore or set()) | CAR_IGNORE

    def scan(text: str, strict: bool) -> list[str]:
        out = []
        for m in CAR_RE.finditer(text):
            code = m.group(1)
            if code[:2] in ignore or code in ignore:
                continue
            pre = text[:m.start()]
            ok = (code in known or car_key(code) in known or CAR_NEXT.match(text[m.end():])
                  or (not strict and (pre.rstrip().endswith(("[", "(", "【")) or not pre.strip(" [(【-_")))
                  or (out and pre.rstrip().endswith(("/", ",", "·", "&"))))   # BC4t / GN7 처럼 이어 쓴 차종
            if ok and code not in out:
                out.append(code)
        return out

    s = SUBJECT_PREFIX.sub("", subject or "")
    s = re.sub(r"^\s*(\[[^\]]*(?:공지|긴급|중요|요청|update|협조|정정|reminder|warning)[^\]]*\]|\([^)]*warning[^)]*\))\s*", "",
               s, flags=re.I)
    s = SUBJECT_PREFIX.sub("", s)
    return scan(s, False) or scan((body or "")[:300], True)


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
        act = cfg.get("action", {})
        self.action = _matcher(act.get("keywords", []))
        self.ignore_senders = [x.lower() for x in act.get("ignore_senders", [])]
        self.ignore_subjects = [x.lower() for x in act.get("ignore_subjects", [])]
        self.max_to = int(act.get("max_to", 0) or 0)
        self.part_res = [re.compile(p) for p in cfg.get("part_number", {}).get("patterns", [])]
        me = cfg.get("me", {})
        self.my_addrs = {a.lower() for a in me.get("addresses", []) if a and "company.com" not in a}
        self.my_names = [n for n in me.get("names", []) if n]
        cars = cfg.get("cars", {})
        self.car_known = {k for k, v in (cars.get("aliases") or {}).items()} | \
            {x for v in (cars.get("aliases") or {}).values() for x in v} | set(cars.get("known", []))
        self.car_ignore = set(cars.get("ignore", []))

    def _addressed_to_me(self, to_addr: str) -> bool:
        """받는사람(To)에 내가 있는지. 설정이 없으면 판단 불가 → True."""
        if not (self.my_addrs or self.my_names):
            return True
        t = (to_addr or "").lower()
        return any(a in t for a in self.my_addrs) or any(n.lower() in t for n in self.my_names)

    def _is_bulk(self, rec: dict) -> bool:
        """시스템 알림 · 전사 공지 · 단체 메일 여부."""
        who = f"{rec.get('sender_email') or ''} {rec.get('sender_name') or ''}".lower()
        if any(x in who for x in self.ignore_senders):
            return True
        subj = (rec.get("subject") or "").lower()
        if any(x in subj for x in self.ignore_subjects):
            return True
        to = [t for t in re.split(r"[;,]", rec.get("to_addr") or "") if t.strip()]
        return bool(self.max_to and len(to) > self.max_to)

    def _quotes_me(self, body: str) -> int:
        """바로 앞 인용 메일(첫 번째 인용 머리글)이 내가 보낸 것이면 1 → 상대가 '내 회신'에 답한 메일.
        보낸메일함을 못 가져와도(POP3) 이것으로 '이미 회신한 건'을 알 수 있다."""
        if not (self.my_addrs or self.my_names):
            return 0
        m = QUOTED_FROM.search(body or "")
        if not m:
            return 0
        who = m.group(1).lower()
        return int(any(a in who for a in self.my_addrs) or any(n.lower() in who for n in self.my_names))

    def classify(self, rec: dict) -> dict:
        subject = rec.get("subject", "") or ""
        body = rec.get("body", "") or ""
        attach = rec.get("attachments", "") or ""
        latest = latest_part(body)
        people = " ".join(rec.get(k, "") or "" for k in ("sender_name", "sender_email", "to_addr", "cc_addr"))

        # 태그는 제목 + 최신 본문 + 첨부파일명 + 주소 기준 (인용된 이전 메일·긴 이력은 제외)
        text = f"{subject}\n{latest}\n{attach}\n{people}".lower()
        tags = [name for name, match in self.tags.items() if match(text)]

        # 품번은 전체 본문 + 첨부 내용에서 (이력 추적용)
        part_nos: list[str] = []
        for rx in self.part_res:
            for m in rx.findall(f"{subject}\n{body}\n{attach}\n{rec.get('att_text', '') or ''}"):
                m = m if isinstance(m, str) else m[0]
                if m not in part_nos:
                    part_nos.append(m)

        sender = (rec.get("sender_email") or "").lower()
        folder = (rec.get("folder") or "").lower()
        is_mine = int(sender in self.my_addrs or any(f in folder for f in SENT_FOLDERS))

        # 조치 필요: 내가 보낸 메일 X, 내가 받는사람(To)에 있음 (참조만이면 X), 공지·알림·단체메일 X,
        #            제목/최신 본문에 요청 키워드
        tkey = thread_key(subject)  # '회신:' 같은 말머리는 빼고 판단
        is_reply = bool(SUBJECT_PREFIX.match(subject or ""))
        # 답장/전달 메일은 제목이 처음 요청 그대로 반복되므로 새로 쓴 본문만 본다
        latest_l = (latest[:1500] if is_reply else f"{tkey}\n{latest[:1500]}").lower()
        needs = int(not is_mine and self._addressed_to_me(rec.get("to_addr", ""))
                    and not self._is_bulk(rec) and self.action(latest_l))

        return {"tags": ",".join(tags), "part_nos": ",".join(part_nos[:30]), "needs_action": needs,
                "is_mine": is_mine, "thread_key": tkey,
                "quotes_me": 0 if is_mine else self._quotes_me(body),
                "cars": ",".join(find_cars(subject, latest, self.car_known, self.car_ignore))}
