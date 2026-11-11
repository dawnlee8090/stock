"""주제별 메일 이력 정리 (예: 'CONSOLE 문제점').

검색어로 찾은 메일 + 같은 건(스레드)의 앞뒤 메일(내 회신 포함)을 모아
건별(스레드)·시간순으로 정리하고, 업체에 보낼 '이력 공유 + 개선대책 회신 요청' 메일 초안을 만든다.
초안은 화면에 보여주기만 하고 보내지 않는다 (사용자가 아웃룩 창에서 직접 보냄).
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, timedelta
from email.utils import getaddresses

from .search import search, snippet
from .textutil import readable_body

MAX_MAILS = 400
# '이력을 정리할 주제'에 붙는 일반 단어 - 메일 본문에 꼭 있지는 않으므로 검색 조건에서 뺀다 (다른 단어가 있을 때만)
GENERIC = {"문제점", "문제", "이력", "관련", "건", "메일", "내용", "정리", "요청", "히스토리", "history"}


def search_terms(q: str) -> tuple[str, list[str]]:
    from .search import split_terms
    terms = split_terms(q)
    keep = [t for t in terms if t.lower() not in GENERIC]
    if not keep:
        return q, []
    dropped = [t for t in terms if t.lower() in GENERIC]
    return " ".join(f'"{t}"' if " " in t else t for t in keep), dropped


def _row(r, q: str) -> dict:
    return {"id": r["id"], "sent_at": r["sent_at"], "sender_name": r["sender_name"], "sender_email": r["sender_email"],
            "to_addr": (r["to_addr"] or "")[:400], "cc_addr": (r["cc_addr"] or "")[:400], "subject": r["subject"],
            "is_mine": r["is_mine"], "status": r["status"], "needs_action": r["needs_action"],
            "thread_key": r["thread_key"] or f"#{r['id']}", "attachments": r["attachments"] or "",
            "tags": r["tags"] or "", "cars": r["cars"] or "",
            "snippet": snippet(readable_body(r["body"]), q, width=90, att_text=r["att_text"])}


def _state(mails: list[dict]) -> tuple[str, str]:
    """건의 현재 상태: 마지막 메일이 나 → 업체 회신 대기 / 받은 메일이 회신 필요 → 회신 필요 ..."""
    last = mails[-1]
    if any(m["status"] == "완료" for m in mails[-2:]):
        return "완료", "ok"
    if last["is_mine"]:
        return "업체 회신 대기", "warn"
    if last["needs_action"]:
        return "회신 필요", "crit"
    return "진행", "info"


def build(conn, cfg: dict, q: str, cars: list[str] | None = None, since: str = "", with_threads: bool = True) -> dict:
    """검색 + 같은 건 메일을 모아 건별·시간순 이력."""
    sq, dropped = search_terms(q)
    hits = search(conn, sq, cars=cars or None, since=since or None, limit=MAX_MAILS, sort="date")
    found = {r["id"] for r in hits}
    rows = {r["id"]: r for r in hits}
    keys = sorted({r["thread_key"] for r in hits if r["thread_key"]})
    if with_threads and keys:   # 제목이 같은 앞뒤 메일 (내 회신, 업체 답장)도 이력에 포함
        for i in range(0, len(keys), 300):
            chunk = keys[i:i + 300]
            for r in conn.execute(f"SELECT * FROM mails WHERE thread_key IN ({','.join('?' * len(chunk))})"
                                  + (" AND sent_at >= ?" if since else "") + " ORDER BY sent_at DESC LIMIT ?",
                                  (*chunk, *([since] if since else []), MAX_MAILS)):
                rows.setdefault(r["id"], r)
    mails = sorted((_row(r, q) | {"hit": r["id"] in found} for r in rows.values()), key=lambda m: m["sent_at"])
    if len(mails) > MAX_MAILS:
        mails = mails[-MAX_MAILS:]
    threads: dict[str, list[dict]] = {}
    for m in mails:
        threads.setdefault(m["thread_key"], []).append(m)
    tlist = []
    for k, ms in threads.items():
        st, sev = _state(ms)
        tlist.append({"key": k, "subject": re.sub(r"^\s*((re|fw|fwd|회신|전달|답장)\s*:\s*)+", "", ms[0]["subject"] or "", flags=re.I)
                      or ms[0]["subject"], "first": ms[0]["sent_at"], "last": ms[-1]["sent_at"], "count": len(ms),
                      "mine": sum(1 for m in ms if m["is_mine"]), "ids": [m["id"] for m in ms], "state": st, "sev": sev,
                      "last_from": ms[-1]["sender_name"] or ms[-1]["sender_email"], "last_mine": bool(ms[-1]["is_mine"])})
    tlist.sort(key=lambda t: t["last"], reverse=True)   # 최근에 움직인 건이 위
    people = Counter((m["sender_name"] or m["sender_email"], m["sender_email"]) for m in mails if not m["is_mine"])
    return {"query": q, "search": sq, "ignored": dropped, "mails": mails, "threads": tlist, "hits": len(found),
            "first": mails[0]["sent_at"] if mails else "", "last": mails[-1]["sent_at"] if mails else "",
            "received": sum(1 for m in mails if not m["is_mine"]), "sent": sum(1 for m in mails if m["is_mine"]),
            "people": [{"name": n, "email": e, "count": c} for (n, e), c in people.most_common(8)],
            "vendor": guess_vendor(mails, cfg)}


def _my_domains(cfg: dict) -> set[str]:
    me = cfg.get("me", {})
    return {a.split("@", 1)[1].lower() for a in me.get("addresses", []) if "@" in a and "company.com" not in a}


def guess_vendor(mails: list[dict], cfg: dict) -> dict:
    """초안 받는사람 추정: 고른 메일에서 우리 회사가 아닌 사람 중 가장 많이 주고받은 회사(도메인)의 담당자."""
    mine = _my_domains(cfg)
    auto = [a.lower() for a in cfg.get("folders", {}).get("autoway_senders", ["rpabot"]) if a]
    seen: Counter = Counter()
    names: dict[str, str] = {}
    for m in mails:
        pairs = [(m["sender_name"], m["sender_email"])] if not m["is_mine"] else getaddresses([m["to_addr"], m["cc_addr"]])
        for n, e in pairs:
            e = (e or "").strip().lower()
            if "@" not in e or e.split("@", 1)[1] in mine or any(a in e for a in auto):
                continue
            seen[e] += 1
            names.setdefault(e, (n or "").strip())
    if not seen:
        return {"to": "", "name": "", "domain": ""}
    dom: Counter = Counter()
    for e, c in seen.items():
        dom[e.split("@", 1)[1]] += c
    top = dom.most_common(1)[0][0]
    people = [e for e, _ in seen.most_common() if e.endswith("@" + top)][:3]
    return {"to": "; ".join(people), "name": names.get(people[0], ""), "domain": top}


def business_days(n: int, start: date | None = None) -> date:
    d = start or date.today()
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def _md(iso: str) -> str:
    try:
        d = date.fromisoformat(iso[:10])
        return f"{d.month}/{d.day}"
    except ValueError:
        return iso[:10]


def draft(mails: list[dict], cfg: dict, topic: str, due: str = "", vendor_name: str = "", car: str = "",
          note: str = "") -> dict:
    """업체에 보낼 '이력 공유 + 원인·개선대책 회신 요청' 메일 초안 (틀). 메일 번호(#)는 업체가 모르므로 날짜·제목으로."""
    me = cfg.get("me", {})
    due_d = date.fromisoformat(due) if due else business_days(3)
    wd = "월화수목금토일"[due_d.weekday()]
    mails = sorted(mails, key=lambda m: m["sent_at"])
    lines = []
    for i, m in enumerate(mails, 1):
        who = "당사 요청" if m["is_mine"] else (m["sender_name"] or m["sender_email"])
        snip = re.sub(r"\s+", " ", (m.get("snippet") or "").replace("[첨부내용] ", "")).strip()
        lines.append(f"{i}) {_md(m['sent_at'])} [{who}] {m['subject']}" + (f"\n   - {snip[:90]}" if snip else ""))
    greet = f"{vendor_name}님 안녕하세요." if vendor_name else "안녕하세요."
    intro = (me.get("intro") or "").strip()
    car_txt = f"[{car}] " if car else ""
    subject = f"{car_txt}{topic} 이력 정리 및 개선대책 회신 요청 (~{due_d.month}/{due_d.day})"
    parts = [greet, *([intro] if intro else []), "",
             f"{topic} 관련하여 그동안 메일로 요청드린 내용과 회신 이력을 아래와 같이 정리하여 공유드립니다.", "",
             "■ 진행 이력", *lines, "",
             "■ 요청 사항",
             "1. 미결 문제점별 원인 분석 결과",
             "2. 개선 대책 및 적용 일정 (금형 수정이 필요한 경우 수정 범위와 일정 포함)",
             "3. 대책 적용 후 검증 결과 (샘플 / 측정 데이터)",
             *([f"4. {note.strip()}"] if note.strip() else []), "",
             f"위 내용 {due_d.month}/{due_d.day}({wd})까지 회신 부탁드립니다.",
             "일정 내 회신이 어려운 항목은 가능한 일정을 먼저 회신 부탁드립니다.", "",
             "감사합니다.", (me.get("signature") or "").strip()]
    body = "\n".join(parts)
    body = re.sub(r"\n{3,}", "\n\n", body).strip() + "\n"
    return {"subject": subject, "body": body, "due": due_d.isoformat()}
