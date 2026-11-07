"""규칙 기반 자동 분류: 태그, 품번, 조치 필요 여부."""
from __future__ import annotations

import re


class Classifier:
    def __init__(self, cfg: dict):
        self.tags = {name: [k.lower() for k in kws] for name, kws in cfg.get("tags", {}).items()}
        self.action_kws = [k.lower() for k in cfg.get("action", {}).get("keywords", [])]
        self.part_res = [re.compile(p) for p in cfg.get("part_number", {}).get("patterns", [])]
        self.my_addrs = {a.lower() for a in cfg.get("me", {}).get("addresses", [])}

    @staticmethod
    def _latest_part(body: str) -> str:
        """회신 메일의 인용 부분(이전 메일)을 잘라 최신 본문만 반환."""
        markers = [r"\n-+\s*Original Message\s*-+", r"\n-+\s*원본 메시지\s*-+",
                   r"\nFrom:\s.+\n(Sent|Date):", r"\n보낸 사람:\s", r"\n보낸사람:\s", r"\n발신:\s"]
        cut = len(body)
        for m in markers:
            hit = re.search(m, body, flags=re.IGNORECASE)
            if hit:
                cut = min(cut, hit.start())
        return body[:cut]

    def classify(self, rec: dict) -> dict:
        subject = rec.get("subject", "") or ""
        body = rec.get("body", "") or ""
        attach = rec.get("attachments", "") or ""
        people = " ".join(rec.get(k, "") or "" for k in ("sender_name", "sender_email", "to_addr", "cc_addr"))
        text = f"{subject}\n{body}\n{attach}\n{people}".lower()

        tags = [name for name, kws in self.tags.items() if any(k in text for k in kws)]

        part_nos: list[str] = []
        for rx in self.part_res:
            for m in rx.findall(f"{subject}\n{body}\n{attach}"):
                m = m if isinstance(m, str) else m[0]
                if m not in part_nos:
                    part_nos.append(m)

        # 조치 필요 판정: 내가 보낸 메일이 아니고, 제목 또는 최신 본문에 요청 키워드
        sender = (rec.get("sender_email") or "").lower()
        latest = f"{subject}\n{self._latest_part(body)}".lower()
        needs = int(sender not in self.my_addrs and any(k in latest for k in self.action_kws))

        return {"tags": ",".join(tags), "part_nos": ",".join(part_nos[:30]), "needs_action": needs}
