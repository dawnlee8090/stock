"""업무 비서 앱 (내 PC 전용 웹 서버).

python -m mailhub app  →  http://127.0.0.1:8765 를 앱 창으로 엽니다.
- 127.0.0.1 에만 열려서 다른 PC 에서는 접속할 수 없습니다. 데이터는 PC 밖으로 나가지 않습니다.
- 화면은 mailhub/web/index.html (아티팩트 예시 화면과 같은 파일), 데이터는 /api/* 로 로컬 DB 에서 읽습니다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import db
from .config import load_config

WEB = Path(__file__).parent / "web" / "index.html"
# 화면(index.html 의 APP_VERSION)과 같이 올린다. 다르면 화면이 '앱을 다시 켜 주세요' 안내를 띄우고,
# 새로 실행할 때 예전 엔진이 켜져 있으면 끄고 새로 시작한다.
APP_VERSION = "2026-11-12.1"
SKELETON_HEAD = ('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
                 '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
                 '<style>:root{color-scheme:light}body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style>'
                 '</head><body>')


def _iso(d: date) -> str:
    return d.isoformat()


def _b64(v) -> bytes:
    """'data:image/jpeg;base64,....' 또는 base64 문자열 → bytes"""
    import base64
    t = str(v or "")
    if t.startswith("data:"):
        t = t.split(",", 1)[1] if "," in t else ""
    if not t:
        raise ValueError("파일 내용이 없습니다")
    return base64.b64decode(t)


def _log_what(x) -> str:
    if not x["from_status"]:
        return x["to_status"]                       # 등록
    if x["from_status"] != x["to_status"]:
        return f"{x['from_status']} → {x['to_status']}"
    return ""                                       # 메모만


# ---------------- 데이터 묶기 ----------------
class Data:
    def __init__(self, cfg: dict, cfg_path: str | None = None):
        self.cfg = cfg
        self.cfg_path = cfg_path

    def conn(self):
        c = db.connect(self.cfg["archive"]["db_path"])
        from . import eo, eo_images, followup, local, mold, to
        followup.ensure(c)
        eo.ensure(c)
        to.ensure(c)
        mold.ensure(c)
        eo_images.ensure(c)
        local.ensure(c)
        return c

    # ---- 공통 ----
    @staticmethod
    def mail_row(r, q: str = "") -> dict:
        from .search import snippet
        from .textutil import readable_body
        return {k: r[k] for k in ("id", "sent_at", "sender_name", "sender_email", "subject", "tags", "part_nos",
                                  "attachments", "needs_action", "status", "is_mine")} | {"to_addr": (r["to_addr"] or "")[:300]} | {
            "snippet": snippet(readable_body(r["body"]), q, att_text=r["att_text"])}

    def img_url(self, path: str) -> str:
        return "/api/img?path=" + quote(path)

    # ---- 오늘 ----
    def summary(self, c) -> dict:
        from . import eo as E, mold as M, to as T
        from .search import todo
        today = date.today()
        since30 = _iso(today - timedelta(days=30))
        todo_rows = todo(c, since30, 10 ** 6)
        reqs = c.execute("SELECT * FROM requests WHERE status='요청' ORDER BY due_date").fetchall()
        req_late = [r for r in reqs if r["due_date"] <= _iso(today)]
        eos = E.listing(c, "open")
        tos = T.listing(c, "open")
        molds = M.listing(c, "open")
        eo_late = [r for r in eos if E.delay_of(r)]
        to_late = [r for r in tos if T.delay_of(r)]
        mold_al = [(m, M.alerts(c, m)) for m in molds]
        mold_al = [(m, a) for m, a in mold_al if a]
        groups, car_of = self._car_resolver(c)
        req_cars = {r["id"]: car_of(r["subject"], self._mail_cars(c, r["mail_id"])) for r in reqs}
        prio = []
        for r in req_late:
            n = (date.fromisoformat(r["due_date"]) - today).days
            prio.append({"kind": "업체 요청", "sev": "crit" if n < 0 else "warn", "title": r["subject"],
                         "sub": r["vendor"] or r["to_addrs"], "alert": "오늘 회신 기한" if n == 0 else f"회신 {-n}일 지남",
                         "go": "#req", "id": r["id"], "cars": req_cars[r["id"]]})
        for r in eo_late:
            prio.append({"kind": "E.O", "sev": "crit", "title": f"{r['car']} {r['part_name'] or r['part_no']}",
                         "sub": r["change_desc"], "alert": E.delay_of(r), "go": "#eo", "id": r["id"], "cars": car_of(r["car"])})
        for r in to_late:
            prio.append({"kind": "T/O", "sev": "crit", "title": f"{r['part_name'] or r['part_no']} · {r['defect_type']}",
                         "sub": r["phenomenon"], "alert": T.delay_of(r), "go": "#to", "id": r["id"], "cars": car_of(r["car"])})
        for m, a in mold_al:
            prio.append({"kind": "금형 이관", "sev": "warn", "title": f"{m['mold_no']} {m['part_name']} → {m['dest']}",
                         "sub": a[0], "alert": "확인 필요", "go": "#mold", "id": m["id"], "cars": car_of(m["car"])})
        from . import local as LM
        lm_cars = LM.cars(c)
        for x in lm_cars:   # 현지 금형: 차종마다 한 줄 (지연·중요도 상·검증 대기)
            if x["late"] or x["hi_open"]:
                prio.append({"kind": "현지 금형", "sev": "crit" if x["late"] else "warn", "title": f"{x['car']} 현지 금형 문제점",
                             "sub": f"진행 {x['open']}건 · 중요도 상 {x['hi_open']}건 · 검증 T/O 필요 {x['wait']}건",
                             "alert": f"수정 {x['late']}건 지연" if x["late"] else "중요도 상 진행", "go": "#local",
                             "id": 0, "cars": car_of(x["car"])})
        for r in todo_rows[:30]:
            prio.append({"kind": "메일", "sev": "info", "title": r["subject"], "sub": r["sender_name"],
                         "alert": "회신 필요", "go": "#mail", "id": r["id"], "cars": car_of(cars=r["cars"])})
        lo, hi = _iso(today - timedelta(days=3)), _iso(today + timedelta(days=14))
        agenda = []
        for r in reqs:
            agenda.append({"date": r["due_date"], "kind": "요청", "text": f"{r['vendor'] or r['to_addrs']} 회신 기한",
                           "src": "req", "sid": r["id"], "cars": req_cars[r["id"]]})
        for r in eos:
            for f, label in (("review_due", "검토 회신"), ("mold_due", "금형 수정 완료"), ("verify_due", "검증")):
                if r[f]:
                    agenda.append({"date": r[f], "kind": "E.O", "text": f"{r['part_name'] or r['part_no']} {label}",
                                   "src": "eo", "sid": r["id"], "cars": car_of(r["car"])})
        for r in tos:
            if r["due"]:
                agenda.append({"date": r["due"], "kind": "T/O", "text": f"{r['part_name'] or r['part_no']} {r['defect_type']} 대책",
                               "src": "to", "sid": r["id"], "cars": car_of(r["car"])})
            if r["verify_due"]:
                agenda.append({"date": r["verify_due"], "kind": "T/O", "text": f"{r['part_name'] or r['part_no']} 효과 확인",
                               "src": "to", "sid": r["id"], "cars": car_of(r["car"])})
        for m in molds:
            s = M.ship(c, m["id"])
            if s and s["etd"] and not s["atd"]:
                agenda.append({"date": s["etd"], "kind": "선적", "text": f"{m['mold_no']} ETD → {m['dest']}",
                               "src": "mold", "sid": m["id"], "cars": car_of(m["car"])})
            if s and s["eta"] and not s["ata"]:
                agenda.append({"date": s["eta"], "kind": "도착", "text": f"{m['mold_no']} ETA {s['port_to']}",
                               "src": "mold", "sid": m["id"], "cars": car_of(m["car"])})
        agenda = sorted([a for a in agenda if lo <= a["date"] <= hi], key=lambda a: a["date"])
        recent = c.execute("SELECT * FROM mails WHERE is_mine=0 ORDER BY sent_at DESC LIMIT 6").fetchall()
        progress = c.execute("SELECT * FROM mails WHERE status IN ('진행중', '업체검토중') ORDER BY sent_at DESC").fetchall()
        last = c.execute("SELECT MAX(ingested_at) FROM mails").fetchone()[0]
        return {
            "counts": {"todo": len(todo_rows), "req_late": len(req_late), "req_open": len(reqs), "eo_late": len(eo_late),
                       "eo_open": len(eos), "to_late": len(to_late), "to_open": len(tos), "mold_alert": len(mold_al),
                       "mold_open": len(molds), "lm_late": sum(x["late"] for x in lm_cars),
                       "lm_open": sum(x["open"] for x in lm_cars), "mail_progress": sum(1 for r in progress if r["status"] == "진행중"),
                       "mail_vendor": sum(1 for r in progress if r["status"] == "업체검토중")},
            "priority": prio, "agenda": agenda, "last_sync": last,
            "recent": [self.mail_row(r) | {"cars": car_of(cars=r["cars"])} for r in recent],
            "progress": [self.mail_row(r) | {"cars": car_of(cars=r["cars"])} for r in progress[:200]],
            # 차종 탭에서 그 차종의 회신 필요 메일을 다 보여주기 위한 목록
            "todo_mails": [{"id": r["id"], "sent_at": r["sent_at"], "subject": r["subject"], "sender_name": r["sender_name"],
                            "status": r["status"], "cars": car_of(cars=r["cars"])} for r in todo_rows[:500]],
            "cars": [{"key": k, "name": g["name"]} for k, g in sorted(groups.items(), key=lambda x: -x[1]["count"])],
        }

    def history(self, c, q: dict) -> dict:
        """주제별 이력 (예: CONSOLE 문제점) - 검색한 메일 + 같은 건의 앞뒤 메일."""
        from . import history as H
        query = (q.get("q") or [""])[0].strip()
        if not query:
            raise ValueError("정리할 주제(검색어)를 넣어 주세요")
        car = (q.get("car") or [""])[0]
        g = self.car_groups(c).get(car) if car else None
        cars = (g["variants"] if g else [car]) if car else None
        days = int((q.get("days") or ["0"])[0] or 0)
        since = _iso(date.today() - timedelta(days=days)) if days else ""
        out = H.build(c, self.cfg, query, cars=cars, since=since)
        out["car_name"] = g["name"] if g else car
        return out

    def _car_resolver(self, c):
        """차종 표기(BC4t CUV, BC4 ...)를 차종 폴더 키(BC4)로. 폴더가 있는 차종만."""
        from .classify import car_key
        groups = self.car_groups(c)
        alias = {v: k for k, vs in (self.cfg.get("cars", {}).get("aliases") or {}).items() for v in [k, *vs]}

        def car_of(*texts, cars: str = "") -> list[str]:
            toks = [t for t in (cars or "").split(",") if t]
            for t in texts:
                toks += re.findall(r"[A-Za-z]{1,3}\d{1,2}[A-Za-z]{0,3}", t or "")
            out = []
            for t in toks:
                k = car_key(alias.get(t, t))
                if k in groups and k not in out:
                    out.append(k)
            return out
        return groups, car_of

    @staticmethod
    def _mail_cars(c, mail_id) -> str:
        if not mail_id:
            return ""
        r = c.execute("SELECT subject, cars FROM mails WHERE id=?", (mail_id,)).fetchone()
        return f"{r['cars'] or ''} {r['subject']}" if r else ""

    def related(self, c, src: str, sid: int) -> dict:
        """오늘 화면 일정을 누르면 띄우는 관련 메일 (E.O·T/O·금형 이관에 연결된 메일, 업체 요청의 요청·회신 메일)."""
        from .search import search
        cols = "m.id, m.sent_at, m.sender_name, m.subject"
        link = {"eo": ("eos", "eo_mails", "eo_id"), "to": ("to_issues", "to_mails", "to_id"),
                "mold": ("molds", "mold_mails", "mold_id")}
        if src in link:
            tbl, lt, fk = link[src]
            r = c.execute(f"SELECT * FROM {tbl} WHERE id=?", (sid,)).fetchone()
            if not r:
                raise KeyError("항목이 없습니다")
            mails = [dict(x) for x in c.execute(f"SELECT {cols} FROM {lt} x JOIN mails m ON m.id=x.mail_id "
                                                f"WHERE x.{fk}=? ORDER BY m.sent_at DESC LIMIT 30", (sid,))]
            if src == "eo":
                title, sub = f"E{sid} {r['car']} {r['part_name'] or r['part_no']}", f"{r['eo_no']} · {r['change_desc']}"
                key = r["part_no"]
            elif src == "to":
                title, sub = f"T{sid} {r['part_name'] or r['part_no']} · {r['defect_type']}", r["phenomenon"]
                key = r["part_no"]
            else:
                title, sub = f"M{sid} {r['mold_no']} {r['part_name']} → {r['dest']}", r["status"]
                key = r["mold_no"]
            guessed = False
            if not mails and key:   # 연결된 메일이 없으면 품번(금형번호)으로 찾은 메일
                mails = [{k: x[k] for k in ("id", "sent_at", "sender_name", "subject")}
                         for x in search(c, "", part_no=key, limit=15)]
                guessed = bool(mails)
            return {"title": title, "sub": sub or "", "go": "#" + src, "mails": mails, "guessed": guessed}
        if src == "req":
            r = c.execute("SELECT * FROM requests WHERE id=?", (sid,)).fetchone()
            if not r:
                raise KeyError("항목이 없습니다")
            ids = [i for i in (r["mail_id"], r["reply_mail_id"]) if i]
            q = f"SELECT {cols} FROM mails m WHERE " + " OR ".join(
                (["m.id IN (" + ",".join("?" * len(ids)) + ")"] if ids else []) + (["m.thread_key=?"] if r["thread_key"] else [])
                or ["0"]) + " ORDER BY m.sent_at DESC LIMIT 30"
            mails = [dict(x) for x in c.execute(q, (*ids, *([r["thread_key"]] if r["thread_key"] else [])))]
            return {"title": f"R{sid} {r['vendor'] or r['to_addrs']} · {r['subject']}",
                    "sub": f"회신 기한 {r['due_date']} · {r['status']}", "go": "#req", "mails": mails, "guessed": False}
        raise KeyError("알 수 없는 항목")

    # ---- 메일 ----
    def mails(self, c, q: dict) -> list:
        from .search import search, todo
        query = (q.get("q") or [""])[0]
        limit = min(int((q.get("limit") or ["150"])[0]), 500)
        off = max(int((q.get("offset") or ["0"])[0]), 0)   # 목록 '더 보기' (150건씩)
        d = (q.get("dir") or [""])[0]   # sent = 보낸 메일만, recv = 받은 메일만 (검색 결과·전체 메일 필터)
        dk = {"mine": True} if d == "sent" else ({"received": True} if d == "recv" else {})
        status = (q.get("status") or [""])[0]
        car = (q.get("car") or [""])[0]
        tag = (q.get("tag") or [None])[0]
        auto = self.autoway_senders()
        if (q.get("special") or [""])[0] == "sent":      # 보낸 메일 (그룹웨어 보낸편지함 → 받은편지함으로 옮겨 수집한 것 포함)
            rows = search(c, query, mine=True, limit=limit, offset=off, sort="date")
        elif (q.get("special") or [""])[0] == "autoway":   # 오토웨이 접수 (RPABOT)
            rows = search(c, query, **dk, senders_any=auto, limit=limit, offset=off, sort="date")
        elif car:      # 차종 폴더 (+ 분류)
            g = self.car_groups(c).get(car)
            rows = search(c, query, **dk, cars=(g["variants"] if g else [car]), tag=tag, exclude_senders=auto, limit=limit, offset=off,
                          sort="date")
        elif tag and not query:   # 분류 폴더
            rows = search(c, query, **dk, tag=tag, exclude_senders=auto, limit=limit, offset=off, sort="date")
        elif status:   # 내가 표시한 진행 상태 (진행중 / 업체검토중 / 완료)
            rows = search(c, query, **dk, status=status, limit=limit, offset=off, sort="date")
        elif (q.get("todo") or [""])[0] == "1" and not query:
            rows = todo(c, _iso(date.today() - timedelta(days=30)), limit, offset=off)
        else:
            rows = search(c, query, **dk, tag=(q.get("tag") or [None])[0], limit=limit, offset=off, sort="date")   # 검색도 최근 메일이 맨 위
        return [self.mail_row(r, query) for r in rows]

    def autoway_senders(self) -> list[str]:
        """오토웨이 접수 메일 보낸사람 (기본: RPABOT). config [folders] autoway_senders"""
        return [x for x in self.cfg.get("folders", {}).get("autoway_senders", ["rpabot"]) if x]

    def _auto_sql(self, neg: bool) -> tuple[str, list]:
        names = self.autoway_senders() or ["\x00"]
        w = " OR ".join("lower(sender_email) LIKE ? OR lower(sender_name) LIKE ?" for _ in names)
        return ("NOT " if neg else "") + f"({w})", [f"%{n.lower()}%" for n in names for _ in (0, 1)]

    def _meta_rows(self, c) -> list:
        """(tags, cars, status, sender_email, sender_name) 전체 - 색인(idx_mails_meta)만 읽어 빠름."""
        return c.execute("SELECT tags, cars, status, is_mine, sender_email, sender_name FROM mails").fetchall()

    def _is_auto(self, email: str, name: str) -> bool:
        who = f"{email or ''} {name or ''}".lower()
        return any(a.lower() in who for a in self.autoway_senders())

    def car_groups(self, c, rows: list | None = None) -> dict:
        """차종 폴더: {키: {name, count, variants}}. 표기가 다른 같은 차종(BC4t/BC4/BC4b)은 한 폴더."""
        from collections import Counter
        from .classify import car_key
        cars_cfg = self.cfg.get("cars", {})
        alias = {v: k for k, vs in (cars_cfg.get("aliases") or {}).items() for v in [k, *vs]}
        var_n: Counter = Counter()
        mails_n: Counter = Counter()
        for r in (rows if rows is not None else self._meta_rows(c)):
            if not r["cars"] or self._is_auto(r["sender_email"], r["sender_name"]):
                continue
            seen = set()
            for v in r["cars"].split(","):
                var_n[v] += 1
                k = car_key(alias.get(v, v))
                if k not in seen:
                    mails_n[k] += 1
                    seen.add(k)
        groups: dict = {}
        for v, n in var_n.items():
            k = car_key(alias.get(v, v))
            g = groups.setdefault(k, {"variants": [], "best": ("", -1)})
            g["variants"].append(v)
            if n > g["best"][1]:
                g["best"] = (alias.get(v, v), n)
        min_n = int(cars_cfg.get("min_mails", 3))
        return {k: {"key": k, "name": g["best"][0], "count": mails_n[k], "variants": sorted(g["variants"])}
                for k, g in groups.items() if mails_n[k] >= min_n}

    TAGS = ("E.O", "T/O", "금형수정", "검사구", "품질문제", "금형이관")

    def mailcounts(self, c) -> dict:
        """왼쪽 메일함 폴더별 건수 (차종별 분류 포함, 오토웨이 접수는 따로). 색인 한 번만 훑음."""
        from collections import Counter
        from .classify import car_key
        from .search import todo
        rows = self._meta_rows(c)
        groups = self.car_groups(c, rows)
        alias = {v: k for k, vs in (self.cfg.get("cars", {}).get("aliases") or {}).items() for v in [k, *vs]}
        status = Counter(r["status"] for r in rows)
        tags: Counter = Counter()
        car_tags: dict = {k: Counter() for k in groups}
        auto = 0
        for r in rows:
            if self._is_auto(r["sender_email"], r["sender_name"]):
                auto += 1
                continue
            ts = [t for t in (r["tags"] or "").split(",") if t in self.TAGS]
            tags.update(ts)
            keys = {car_key(alias.get(v, v)) for v in (r["cars"] or "").split(",") if v}
            for k in keys & set(car_tags):
                car_tags[k].update(ts)
        cars = [{"key": g["key"], "name": g["name"], "count": g["count"],
                 "tags": {t: car_tags[g["key"]][t] for t in self.TAGS}}
                for g in sorted(groups.values(), key=lambda g: -g["count"])]
        return {"all": len(rows), "todo": len(todo(c, _iso(date.today() - timedelta(days=30)), 10 ** 6)),
                "progress": status["진행중"], "vendor": status["업체검토중"], "done": status["완료"],
                "autoway": auto, "tags": {t: tags[t] for t in self.TAGS}, "cars": cars,
                "sent": sum(1 for r in rows if r["is_mine"]), "me_set": self.me_set()}

    def me_set(self) -> bool:
        me = self.cfg.get("me", {})
        return bool([a for a in me.get("addresses", []) if a and "company.com" not in a] or [n for n in me.get("names", []) if n])

    def mail(self, c, mid: int) -> dict:
        r = c.execute("SELECT * FROM mails WHERE id=?", (mid,)).fetchone()
        if not r:
            raise KeyError(f"#{mid} 메일이 없습니다")
        d = self.mail_row(r)
        from .textutil import readable_body
        d.update(body=readable_body(r["body"])[:30000], att_text=(r["att_text"] or "")[:20000])
        return d

    def mail_related(self, c, mid: int) -> dict:
        """메일을 열 때 참고로 보여줄 관련 메일: 같은 건(RE/FW 스레드) + 같은 품번이 들어간 다른 건."""
        r = c.execute("SELECT id, thread_key, part_nos FROM mails WHERE id=?", (mid,)).fetchone()
        if not r:
            raise KeyError(f"#{mid} 메일이 없습니다")
        cols = "id, sent_at, sender_name, sender_email, to_addr, subject, is_mine, status, thread_key"
        pick = lambda x: {k: x[k] for k in ("id", "sent_at", "sender_name", "sender_email", "subject", "is_mine", "status")} | \
            {"to_addr": (x["to_addr"] or "")[:200]}
        thread = [pick(x) for x in c.execute(
            f"SELECT {cols} FROM mails WHERE thread_key=? AND thread_key != '' AND id != ? ORDER BY sent_at DESC LIMIT 40",
            (r["thread_key"] or "", mid))] if r["thread_key"] else []
        seen = {mid, *(x["id"] for x in thread)}
        parts_out, pnos = [], [p for p in (r["part_nos"] or "").split(",") if len(p) >= 5][:3]
        for pn in pnos:   # 품번은 검색 색인(part_nos 칸)으로 빠르게
            try:
                ids = [x[0] for x in c.execute("SELECT rowid FROM mails_fts WHERE mails_fts MATCH ? ORDER BY rowid DESC LIMIT 60",
                                               ('part_nos : "' + pn.replace('"', '') + '"',))]
            except Exception:
                ids = []
            if not ids:
                continue
            for x in c.execute(f"SELECT {cols} FROM mails WHERE id IN ({','.join('?' * len(ids))}) ORDER BY sent_at DESC", ids):
                if x["id"] in seen or (x["thread_key"] and x["thread_key"] == r["thread_key"]):
                    continue
                seen.add(x["id"])
                parts_out.append(pick(x) | {"part_no": pn})
        parts_out.sort(key=lambda x: x["sent_at"], reverse=True)
        return {"thread": thread, "parts": parts_out[:30], "part_nos": pnos}

    def original_html(self, c, mid: int) -> str:
        """메일 원본의 HTML(표·서식·본문 이미지 포함). 스크립트·외부 연결은 응답 헤더(CSP)로 막는다."""
        import base64
        import email
        import html as _html
        from email import policy
        r = c.execute("SELECT file_path, entry_id, store_id, body, subject, sender_name, sender_email, to_addr, cc_addr, "
                      "sent_at, attachments FROM mails WHERE id=?", (mid,)).fetchone()
        if not r:
            raise KeyError(f"#{mid} 메일이 없습니다")
        fp = r["file_path"] or ""
        doc = ""
        if fp.lower().endswith(".eml") and os.path.exists(fp):
            msg = email.message_from_bytes(Path(fp).read_bytes(), policy=policy.default)
            part = msg.get_body(preferencelist=("html", "plain"))
            if part is not None:
                content = part.get_content()
                if part.get_content_type() == "text/html":
                    doc = content
                    for sub in msg.walk():   # 본문에 붙여 넣은 그림(cid:) → data URI
                        cid = (sub.get("Content-ID") or "").strip("<> ")
                        if cid and sub.get_content_maintype() == "image":
                            data = base64.b64encode(sub.get_payload(decode=True) or b"").decode()
                            doc = doc.replace(f"cid:{cid}", f"data:{sub.get_content_type()};base64,{data}")
                else:
                    doc = f"<pre>{_html.escape(content)}</pre>"
        elif r["entry_id"] and os.name == "nt":
            try:
                import win32com.client  # type: ignore
                ns = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
                doc = ns.GetItemFromID(r["entry_id"], r["store_id"] or None).HTMLBody
            except Exception:
                doc = ""
        if not doc:
            doc = f"<pre>{_html.escape(r['body'] or '')}</pre>"
        style = ("<meta charset='utf-8'><style>body{font-family:'Malgun Gothic',sans-serif;font-size:14px;margin:16px;"
                 "background:#fff;color:#111}pre{white-space:pre-wrap}img{max-width:100%;height:auto}"
                 ".mh-head{border-collapse:collapse;width:100%;margin:0 0 14px;font-size:13px;border-bottom:2px solid #1f3864}"
                 ".mh-head th{text-align:left;color:#555;font-weight:600;width:72px;padding:4px 8px 4px 0;vertical-align:top}"
                 ".mh-head td{padding:4px 0;word-break:break-all}.mh-subj{font-size:17px;font-weight:700;margin:0 0 8px}</style>")
        e = _html.escape

        def people(v: str) -> str:   # 받는사람·참조: 사람마다 줄바꿈 없이 ; 로
            return e("; ".join(x.strip() for x in re.split(r"[;\n]", v or "") if x.strip()))

        sender = f"{r['sender_name']} <{r['sender_email']}>" if r["sender_name"] else (r["sender_email"] or "")
        rows = [("보낸사람", e(sender)), ("받는사람", people(r["to_addr"])), ("참조", people(r["cc_addr"])),
                ("날짜", e((r["sent_at"] or "").replace("T", " ")[:16])),
                ("첨부", e(" · ".join(x for x in (r["attachments"] or "").splitlines() if x)))]
        head = (f"<div class='mh-subj'>{e(r['subject'] or '')}</div><table class='mh-head'>"
                + "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows if v) + "</table>")
        return style + head + doc

    # ---- 업체 요청 ----
    def reqs(self, c) -> list:
        today = date.today()
        rows = c.execute("SELECT * FROM requests ORDER BY CASE status WHEN '요청' THEN 0 ELSE 1 END, due_date DESC").fetchall()
        out = []
        for r in rows[:300]:
            d = dict(r)
            if r["status"] == "요청":
                n = (date.fromisoformat(r["due_date"]) - today).days
                d["state"] = f"{-n}일 지남" if n < 0 else ("오늘" if n == 0 else f"D-{n}")
            else:
                d["state"] = ""
            out.append(d)
        return out

    # ---- E.O ----
    def eos(self, c) -> list:
        from . import eo as E, eo_images as I
        out = []
        for r in E.listing(c, "all"):
            d = dict(r)
            d.update(delay=E.delay_of(r), soon=E.soon_of(r), new_mails=E.new_mail_count(c, r),
                     images=len(I.listing(c, r["id"])))
            out.append(d)
        return out

    def eo(self, c, eid: int) -> dict:
        from . import eo as E, eo_images as I
        r = c.execute("SELECT * FROM eos WHERE id=?", (eid,)).fetchone()
        if not r:
            raise KeyError(f"E{eid} 항목이 없습니다")
        d = dict(r)
        d.update(delay=E.delay_of(r), soon=E.soon_of(r))
        logs = [{"at": x["at"], "what": _log_what(x), "note": x["note"]}
                for x in c.execute("SELECT * FROM eo_log WHERE eo_id=? ORDER BY id", (eid,))]
        mails = [dict(m) for m in c.execute(
            "SELECT m.id, m.sent_at, m.sender_name, m.subject FROM eo_mails em JOIN mails m ON m.id=em.mail_id "
            "WHERE em.eo_id=? ORDER BY m.sent_at DESC", (eid,))]
        reqs = [dict(q) for q in c.execute("SELECT * FROM requests WHERE eo_id=? ORDER BY id", (eid,))]
        imgs = [{"url": self.img_url(x["path"]), "caption": x["caption"]} for x in I.listing(c, eid)]
        E.mark_seen(c, eid)
        return {"eo": d, "log": logs, "mails": mails, "requests": reqs, "images": imgs}

    # ---- T/O ----
    def tos(self, c) -> list:
        from . import to as T
        return [dict(r) | {"delay": T.delay_of(r), "soon": T.soon_of(r)} for r in T.listing(c, "all")]

    def to(self, c, tid: int) -> dict:
        from . import eo_images as I, to as T
        from .to_knowledge import GUIDE, guide_text
        r = c.execute("SELECT * FROM to_issues WHERE id=?", (tid,)).fetchone()
        if not r:
            raise KeyError(f"T{tid} 항목이 없습니다")
        prev = c.execute("SELECT * FROM to_issues WHERE id=?", (r["recur_of"],)).fetchone() if r["recur_of"] else None
        sim = T.similar(c, r["defect_type"], r["part_name"], r["part_no"], r["resin"], r["location"], r["phenomenon"],
                        r["car"], exclude_id=r["id"], limit=6)
        logs = [{"at": x["at"], "what": _log_what(x), "note": x["note"]}
                for x in c.execute("SELECT * FROM to_log WHERE to_id=? ORDER BY id", (tid,))]
        imgs = [{"url": self.img_url(x["path"]), "caption": x["caption"]} for x in I.listing(c, tid, kind="to")]
        T.mark_seen(c, tid)
        return {"issue": dict(r) | {"delay": T.delay_of(r)}, "previous": dict(prev) if prev else None,
                "similar": [{"score": s, "why": w, **dict(x)} for s, w, x in sim], "log": logs, "images": imgs,
                "guide": guide_text(r["defect_type"]) if r["defect_type"] in GUIDE else ""}

    # ---- 현지 금형 문제점 ----
    def _photo(self, p) -> dict:
        return {"id": p["id"], "grp": p["grp"], "url": self.img_url(p["path"]),
                "marked": self.img_url(p["marked_path"]) if p["marked_path"] else "",
                "marks": json.loads(p["marks"]) if p["marks"] else [], "w": p["w"], "h": p["h"]}

    def _lm_row(self, r, thumb=None, n_photo: int = 0) -> dict:
        from . import local as LM
        return dict(r) | {"delay": LM.delay_of(r), "wait": LM.wait_check(r), "photos": n_photo,
                          "thumb": self._photo(thumb) if thumb else None}

    def local(self, c, q: dict) -> dict:
        from . import local as LM
        cars = LM.cars(c)
        car = (q.get("car") or [""])[0] or (cars[0]["car"] if cars else "")
        vendor = (q.get("vendor") or [""])[0]
        ms = LM.molds(c, car, vendor or None) if car else []
        rows = LM.issues(c, car, vendor or None) if car else []
        thumbs, pc = LM.first_photos(c, [r["id"] for r in rows]), LM.photo_counts(c)
        return {"cars": cars, "car": car, "vendor": vendor, "molds": ms, "cats": LM.CATS, "stages": LM.STATUSES,
                "issues": [self._lm_row(r, thumbs.get(r["id"]), pc.get(r["id"], 0)) for r in rows]}

    def local_mold(self, c, mid: int) -> dict:
        from . import local as LM
        m = LM.get_mold(c, mid)
        if not m:
            raise KeyError(f"금형 {mid} 이 없습니다")
        ms = next((x for x in LM.molds(c, m["car"], m["vendor"]) if x["id"] == mid), dict(m))
        out = []
        for r in LM.issues(c, mold_id=mid):
            out.append(self._lm_row(r) | {"photo_list": [self._photo(p) for p in LM.photos(c, r["id"])]})
        return {"mold": ms, "issues": out}

    def local_issue(self, c, iid: int) -> dict:
        from . import local as LM
        from .to_knowledge import GUIDE, guide_text
        r = LM.get_issue(c, iid)
        if not r:
            raise KeyError(f"L{iid} 문제점이 없습니다")
        prev = LM.get_issue(c, r["recur_of"]) if r["recur_of"] else None
        logs = [{"at": x["at"], "what": _log_what(x), "note": x["note"]} for x in LM.log(c, iid)]
        return {"issue": self._lm_row(r), "mold": dict(LM.get_mold(c, r["mold_id"]) or {}),
                "photos": [self._photo(p) for p in LM.photos(c, iid)], "previous": dict(prev) if prev else None,
                "log": logs, "guide": guide_text(r["defect_type"]) if r["defect_type"] in GUIDE else ""}

    def local_post(self, c, path: list[str], body: dict) -> dict:
        from . import local as LM
        fields = {k: body[k] for k in LM.ISSUE_FIELDS if k in body}
        if path[1] == "issue" and path[2] == "add":
            iid = LM.add_issue(c, int(body["mold_id"]), **fields)
            return {"ok": True, "id": iid}
        if path[1] == "issue" and len(path) > 3 and path[3] == "photo":
            pid = LM.save_photo(c, self.cfg, int(path[2]), _b64(body.get("data")), str(body.get("grp") or "before"),
                                marks=body.get("marks") or None, flat=_b64(body.get("flat")) if body.get("flat") else None)
            if not pid:
                raise ValueError("같은 사진이 이미 있습니다")
            p = c.execute("SELECT * FROM eo_images WHERE id=?", (pid,)).fetchone()
            return {"ok": True, "photo": self._photo(p)}
        if path[1] == "issue" and len(path) > 3 and path[3] == "del":
            LM.delete_issue(c, self.cfg, int(path[2]))
            return {"ok": True}
        if path[1] == "issue":
            r = LM.update_issue(c, int(path[2]), note=(body.get("note") or "").strip() or None, **fields)
            return {"ok": True, "issue": self._lm_row(r)}
        if path[1] == "photo":
            pid = int(path[2])
            act = path[3] if len(path) > 3 else ""
            if act == "del":
                LM.remove_photo(c, pid)
            elif act == "move":
                LM.move_photo(c, pid, int(body.get("step") or 1))
            else:
                LM.set_marks(c, self.cfg, pid, body.get("marks") or [], _b64(body.get("flat")) if body.get("flat") else None)
            p = c.execute("SELECT * FROM eo_images WHERE id=?", (pid,)).fetchone()
            return {"ok": True, "photo": self._photo(p) if p else None}
        if path[1] == "mold" and path[2] == "add":
            kw = {k: str(body.get(k) or "") for k in LM.MOLD_FIELDS}
            mid, new = LM.upsert_mold(c, str(body.get("car") or ""), str(body.get("vendor") or ""), str(body.get("part_no") or ""), **kw)
            c.commit()
            return {"ok": True, "id": mid, "new": new}
        if path[1] == "import":   # 엑셀/CSV 올리기: 외주처 엑셀(갑지+을지+사진) · 통합 문제점 리스트 · 금형 리스트 자동 구분
            car = str(body.get("car") or "").strip()
            if not car:
                raise ValueError("차종을 입력해 주세요")
            name, data = str(body.get("name") or "upload.xlsx"), _b64(body.get("data"))
            vendor = str(body.get("vendor") or "").strip().upper() or LM.vendor_from_name(name)
            return {"ok": True} | LM.import_any(c, self.cfg, car, name, data, vendor or None)
        raise KeyError("/".join(path))

    # ---- 금형 이관 ----
    def _mold_dict(self, c, m) -> dict:
        from . import mold as M
        d = dict(m)
        d["docs"] = {x["section"]: [x["status"], x["doc_date"]] for x in M.docs(c, m["id"])}
        s = M.ship(c, m["id"])
        d["ship"] = dict(s) if s else {}
        d["alerts"] = M.alerts(c, m)
        return d

    def molds(self, c) -> list:
        from . import mold as M
        return [self._mold_dict(c, m) for m in M.listing(c, "all")]

    def mold(self, c, mid: int) -> dict:
        from . import mold as M
        m = M.get(c, mid)
        logs = [{"at": x["at"], "what": x["what"], "note": x["note"]}
                for x in c.execute("SELECT * FROM mold_log WHERE mold_id=? ORDER BY id", (mid,))]
        mails = [dict(x) for x in c.execute("SELECT ml.id, ml.sent_at, ml.sender_name, ml.subject FROM mold_mails x "
                                            "JOIN mails ml ON ml.id=x.mail_id WHERE x.mold_id=? ORDER BY ml.sent_at DESC", (mid,))]
        M.mark_seen(c, mid)
        return {"mold": self._mold_dict(c, m), "log": logs, "mails": mails,
                "items": {lt: len(M.items(c, mid, lt)) for lt in M.LIST_TYPES}}

    # ---- 쓰기 ----
    def post(self, c, path: list[str], body: dict) -> dict:
        from . import eo as E, followup as F, mold as M, to as T
        if path[0] == "draft" and path[1] == "make":   # 이력 → 업체 메일 초안 (틀)
            from . import history as H
            ids = [int(i) for i in (body.get("ids") or [])][:150]
            if not ids:
                raise ValueError("근거 메일을 하나 이상 골라 주세요")
            rows = c.execute(f"SELECT * FROM mails WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
            mails = [H._row(r, str(body.get("q") or "")) for r in rows]
            return H.draft(mails, self.cfg, str(body.get("q") or "").strip() or "문제점", str(body.get("due") or ""),
                           str(body.get("vendor_name") or ""), str(body.get("car") or ""), str(body.get("note") or ""))
        if path[0] == "draft" and path[1] == "open":   # 아웃룩 새 메일 창 (보내기는 사용자가 창에서)
            to = str(body.get("to") or "")
            res = F.open_draft(to, str(body.get("cc") or ""), str(body.get("subject") or ""), str(body.get("body") or ""),
                               send=False, draft_dir=Path(self.cfg["archive"].get("draft_dir", "data/drafts")))
            return {"ok": True, "message": res}
        if path[0] == "req" and path[1] == "add":   # 화면에서 업체 요청 등록 (회신 추적)
            mid = int(body.get("mail_id") or 0) or None
            rid = F.add(c, str(body.get("to") or ""), str(body.get("subject") or "")[:300], str(body.get("due") or "+3영업일"),
                        cc=str(body.get("cc") or ""), vendor=str(body.get("vendor") or "")[:60],
                        req_type=str(body.get("req_type") or "개선대책")[:30], mail_id=mid, note=str(body.get("note") or "")[:300])
            return {"ok": True, "id": rid}
        if path[0] == "mails" and path[1] == "mark":   # 여러 통 한 번에 (목록에서 선택)
            st = str(body.get("status", ""))[:20]
            ids = [int(i) for i in (body.get("ids") or [])[:500]]
            if ids:
                c.execute(f"UPDATE mails SET status=? WHERE id IN ({','.join('?' * len(ids))})", (st, *ids))
                c.commit()
            return {"ok": True, "count": len(ids)}
        if path[0] == "mail" and path[2] == "mark":
            st = str(body.get("status", ""))[:20]
            c.execute("UPDATE mails SET status=? WHERE id=?", (st, int(path[1])))
            c.commit()
            return {"ok": True}
        if path[0] == "mail" and path[2] == "open":
            r = c.execute("SELECT entry_id, store_id, file_path FROM mails WHERE id=?", (int(path[1]),)).fetchone()
            if r and r["entry_id"]:
                from .ingest_outlook import open_in_outlook
                if open_in_outlook(r["entry_id"], r["store_id"]):
                    return {"ok": True}
            if r and r["file_path"] and os.path.exists(r["file_path"]):
                from .winopen import open_file
                return {"ok": True, "message": open_file(r["file_path"])}
            raise KeyError("원본을 열 수 없습니다 (PST 가 아웃룩에 연결되어 있는지 확인)")
        if path[0] == "req" and path[2] == "":
            rid = int(path[1])
            if body.get("extend"):
                r = c.execute("SELECT due_date FROM requests WHERE id=?", (rid,)).fetchone()
                base = max(date.fromisoformat(r["due_date"]), date.today())
                c.execute("UPDATE requests SET due_date=? WHERE id=?", (F.parse_due(f"+{int(body['extend'])}영업일", base), rid))
            if body.get("status"):
                c.execute("UPDATE requests SET status=? WHERE id=?", (str(body["status"])[:10], rid))
            c.commit()
            return {"ok": True}
        if path[0] == "req" and path[2] == "remind":
            r = c.execute("SELECT * FROM requests WHERE id=?", (int(path[1]),)).fetchone()
            subj, text = F.reminder_mail(r, self.cfg)
            res = F.open_draft(r["to_addrs"], r["cc_addrs"], subj, text, send=False,
                               draft_dir=Path(self.cfg["archive"].get("draft_dir", "data/drafts")))
            F.mark_reminded(c, r["id"])
            return {"ok": True, "message": res}
        if path[0] == "eo" and path[1] == "add":
            mail_id = int(body.get("mail_id") or 0) or None
            ids = []
            for it in body.get("items") or []:
                kw = {k: str(it.get(k) or "").strip() for k in E.FIELDS if k in it}
                if not (kw.get("part_no") or kw.get("part_name") or kw.get("eo_no")):
                    continue
                ids.append(E.add(c, mail_id=mail_id, **kw))
            return {"ok": True, "ids": ids}
        if path[0] == "eo":
            E.update(c, int(path[1]), status=body.get("status") or None, note=(body.get("note") or "").strip() or None)
            return {"ok": True}
        if path[0] == "to":
            T.update(c, int(path[1]), status=body.get("status") or None, note=(body.get("note") or "").strip() or None)
            return {"ok": True}
        if path[0] == "mold" and path[2] == "doc":
            M.set_doc(c, self.cfg, int(path[1]), str(body["section"]), status=str(body["status"]))
            return {"ok": True}
        if path[0] == "local":
            return self.local_post(c, [p for p in path if p != ""] + [""] * 2, body)
        if path[0] == "export":
            kind = path[1]
            out = Path(self.cfg["archive"]["db_path"]).parent
            if kind == "eo":
                p = E.export(c, out / "EO_LIST.xlsx")
            elif kind == "to":
                from . import eo_images as I, to_excel
                p = to_excel.build(c, T.listing(c, "all"), out / "TO_ISSUES.xlsx", T.delay_of, T.soon_of,
                                   lambda i: I.listing(c, i, kind="to"))
            elif kind == "mold":
                from . import mold_excel
                p = mold_excel.build(c, M, M.listing(c, "all"), out / "MOLD_TRANSFER.xlsx")
            elif kind == "local":   # 현지 금형: 외주처를 고르면 그 외주처 양식, 아니면 통합 리스트 + 외주처별
                from . import local as LM
                paths = LM.export(c, self.cfg, str(body.get("car") or ""), str(body.get("vendor") or "") or None)
                p = paths[0] if body.get("vendor") else paths[0].parent
            else:
                raise KeyError(kind)
            if os.name == "nt":
                os.startfile(str(p.resolve()))  # type: ignore[attr-defined]
            return {"ok": True, "path": str(p.resolve())}
        if path[0] == "sync":
            return self.sync()
        raise KeyError("/".join(path))

    def sync(self) -> dict:
        """새 메일 받기 = 매일 자동 수집과 같은 순서 (POP3 미리보기 → 회신 확인 → E.O/T/O/이관 점검)."""
        py = sys.executable
        cfg_args = ["--config", self.cfg_path] if self.cfg_path else []
        steps = [["ingest-pop", "--save-eml", "--no-prompt"], ["req", "check"], ["eo", "check"], ["to", "check"],
                 ["mold", "check"]]
        if not (self.cfg.get("pop", {}).get("host") or self.cfg.get("imap", {}).get("host")):
            steps = steps[1:]
        out = ""
        for st in steps:
            r = subprocess.run([py, "-m", "mailhub", *cfg_args, *st], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=1800, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            out += r.stdout + r.stderr
        mark_my_mails(self, recent_days=30)   # 받은편지함으로 옮겨 온 내 보낸 메일도 '보낸 메일'로
        m = re.search(r"신규 ([\d,]+)", out)
        added = int(m.group(1).replace(",", "")) if m else 0
        return {"ok": True, "added": added, "message": f"새 메일 {added}건을 받았습니다" if m else "점검을 마쳤습니다"}


# ---------------- HTTP ----------------
def make_handler(data: Data, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    img_root = Path(data.cfg["archive"].get("eo_image_dir", "data/eo_images")).resolve()

    class H(BaseHTTPRequestHandler):
        server_version = "mailhub"

        def log_message(self, fmt, *args):  # 콘솔 조용히
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))

        def _guard(self) -> bool:
            # 다른 사이트가 이 PC 의 앱에 접근하지 못하게 (DNS 리바인딩·CSRF 방지)
            if self.headers.get("Host", "") not in allowed_hosts:
                self._send(403, b"forbidden", "text/plain")
                return False
            return True

        def do_GET(self):
            if not self._guard():
                return
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                html = SKELETON_HEAD + WEB.read_text(encoding="utf-8") + "</body></html>"
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if not u.path.startswith("/api/"):
                return self._send(404, b"not found", "text/plain")
            parts = [p for p in u.path[5:].split("/") if p]
            q = parse_qs(u.query)
            try:
                if parts == ["ping"]:
                    return self._json({"ok": True, "version": APP_VERSION})
                if parts == ["img"]:
                    p = Path(unquote((q.get("path") or [""])[0])).resolve()
                    if img_root not in p.parents or not p.is_file():
                        return self._send(404, b"not found", "text/plain")
                    ctype = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
                    return self._send(200, p.read_bytes(), ctype)
                if parts[0] == "ai" and len(parts) == 3 and parts[1] in ("history", "draft"):
                    from . import ai
                    if not re.fullmatch(r"[0-9a-f]{12}", parts[2]):
                        raise KeyError("잘못된 번호")
                    out = {"available": bool(ai.find_claude()), "job": ai.job(f"{parts[1]}:{parts[2]}")}
                    if parts[1] == "history":
                        out["saved"] = ai.saved_history(data.cfg, parts[2])
                    return self._json(out)
                if parts[0] == "ai" and len(parts) == 3:
                    from . import ai
                    key = f"{'review' if parts[1] == 'review' else 'eo'}:{int(parts[2])}"
                    out = {"available": bool(ai.find_claude()), "job": ai.job(key)}
                    if parts[1] == "review":
                        out["saved"] = ai.saved_review(data.cfg, int(parts[2]))
                    return self._json(out)
                c = data.conn()
                try:
                    if parts == ["sentmove"]:
                        from . import sentmove
                        return self._json(sentmove.status(c, data.cfg))
                    if parts == ["mailcounts"]:
                        return self._json(data.mailcounts(c))
                    if parts == ["history"]:
                        return self._json(data.history(c, q))
                    if parts[0] == "related" and len(parts) == 3:
                        return self._json(data.related(c, parts[1], int(parts[2])))
                    if parts == ["summary"]:
                        return self._json(data.summary(c))
                    if parts == ["mails"]:
                        return self._json(data.mails(c, q))
                    if parts[0] == "mail" and len(parts) == 3 and parts[2] == "original":
                        body = data.original_html(c, int(parts[1])).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        # 메일 HTML 안의 스크립트·외부 이미지·폼 차단 (data: 그림과 인라인 서식만 허용)
                        self.send_header("Content-Security-Policy", "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
                                         "font-src data:; form-action 'none'; base-uri 'none'")
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    if parts[0] == "mail" and len(parts) == 3 and parts[2] == "related":
                        return self._json(data.mail_related(c, int(parts[1])))
                    if parts[0] == "mail" and len(parts) == 2:
                        return self._json(data.mail(c, int(parts[1])))
                    if parts == ["reqs"]:
                        return self._json(data.reqs(c))
                    if parts == ["eos"]:
                        return self._json(data.eos(c))
                    if parts[0] == "eo" and len(parts) == 2:
                        return self._json(data.eo(c, int(parts[1])))
                    if parts == ["tos"]:
                        return self._json(data.tos(c))
                    if parts[0] == "to" and len(parts) == 2:
                        return self._json(data.to(c, int(parts[1])))
                    if parts == ["local"]:
                        return self._json(data.local(c, q))
                    if parts[0] == "local" and len(parts) == 3 and parts[1] == "mold":
                        return self._json(data.local_mold(c, int(parts[2])))
                    if parts[0] == "local" and len(parts) == 3 and parts[1] == "issue":
                        return self._json(data.local_issue(c, int(parts[2])))
                    if parts == ["molds"]:
                        return self._json(data.molds(c))
                    if parts[0] == "mold" and len(parts) == 2:
                        return self._json(data.mold(c, int(parts[1])))
                finally:
                    c.close()
                return self._send(404, b"not found", "text/plain")
            except (KeyError, ValueError) as e:
                return self._send(404, str(e).strip("'").encode("utf-8"), "text/plain; charset=utf-8")
            except Exception as e:  # 화면에 원인 표시
                return self._send(500, f"{type(e).__name__}: {e}".encode("utf-8"), "text/plain; charset=utf-8")

        def do_POST(self):
            if not self._guard():
                return
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._send(415, b"json only", "text/plain")
            u = urlparse(self.path)
            parts = [p for p in u.path[5:].split("/") if p] if u.path.startswith("/api/") else []
            if not parts:
                return self._send(404, b"not found", "text/plain")
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, b"bad json", "text/plain")
            try:
                if parts == ["shutdown"]:   # 새 버전 앱이 예전 엔진을 끌 때
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return self._json({"ok": True})
                if parts == ["sync"]:
                    return self._json(data.sync())
                if parts in (["ai", "history"], ["ai", "draft"]):   # 주제별 이력 요약 · 업체 메일 초안 (Claude)
                    from . import ai
                    ids = [int(i) for i in (body.get("ids") or [])][:150]
                    qq = str(body.get("q") or "")[:200]
                    if not ids:
                        raise ValueError("근거 메일을 하나 이상 골라 주세요")
                    if parts[1] == "history":
                        key, job = ai.start_history(data.cfg, qq, ids)
                    else:
                        key, job = ai.start_draft(data.cfg, qq, ids, str(body.get("to") or "")[:300],
                                                  str(body.get("due") or "")[:10], str(body.get("note") or "")[:300])
                    return self._json({"key": key, "job": job})
                if parts[0] == "ai" and len(parts) == 3:
                    from . import ai
                    mid = int(parts[2])
                    if parts[1] == "review":
                        return self._json({"job": ai.start_review(data.cfg, mid)})
                    if parts[1] == "eo":
                        return self._json({"job": ai.start_eo_extract(data.cfg, mid, data.conn)})
                if parts == ["sentmove"]:
                    from . import sentmove
                    r = data.sync()
                    c = data.conn()
                    try:
                        return self._json(sentmove.status(c, data.cfg) | {"message": r.get("message", "")})
                    finally:
                        c.close()
                c = data.conn()
                try:
                    return self._json(data.post(c, parts + [""] * (3 - len(parts)), body))
                finally:
                    c.close()
            except (KeyError, ValueError) as e:
                return self._send(400, str(e).strip("'").encode("utf-8"), "text/plain; charset=utf-8")
            except Exception as e:
                return self._send(500, f"{type(e).__name__}: {e}".encode("utf-8"), "text/plain; charset=utf-8")

    return H


def open_app_window(url: str) -> None:
    """Edge/Chrome 의 '앱 창'(주소창 없는 창)으로 열기. 없으면 기본 브라우저."""
    if os.name == "nt":
        for exe in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")):
            if os.path.exists(exe):
                subprocess.Popen([exe, f"--app={url}", "--window-size=1440,900"])
                return
    exe = shutil.which("msedge") or shutil.which("google-chrome") or shutil.which("chromium")
    if exe:
        subprocess.Popen([exe, f"--app={url}"])
        return
    webbrowser.open(url)


def mark_my_mails(data: "Data", recent_days: int | None = None, log=None) -> int:
    """보낸사람이 나인데 '받은 메일'로 저장된 메일(그룹웨어 보낸편지함 → 받은편지함으로 옮겨 수집)을 '보낸 메일'로 고친다.
    내가 보낸 메일은 회신 필요에서 빠지고, 같은 건의 받은 메일은 '회신함'으로 처리된다."""
    from .classify import Classifier
    clf = Classifier(data.cfg)
    if not (clf.my_addrs or clf.my_names):
        return 0
    c = data.conn()
    try:
        c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        sig = repr((sorted(clf.my_addrs), clf.my_names))
        if recent_days is None:   # 앱을 켤 때: 내 주소·이름 설정이 바뀌었을 때만 전체 확인
            row = c.execute("SELECT value FROM meta WHERE key='mine_backfilled'").fetchone()
            if row and row[0] == sig:
                return 0
        since = _iso(date.today() - timedelta(days=recent_days)) if recent_days else ""
        ids = [r["id"] for r in c.execute("SELECT id, sender_email, sender_name, folder FROM mails WHERE is_mine=0 AND sent_at >= ?",
                                          (since,)) if clf.is_my_mail(r["sender_email"], r["sender_name"], r["folder"])]
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            c.execute(f"UPDATE mails SET is_mine=1, needs_action=0, quotes_me=0 WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        if recent_days is None:
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('mine_backfilled', ?)", (sig,))
        c.commit()
        if ids and log:
            log(f"내가 보낸 메일 {len(ids):,}건을 '보낸 메일'로 분류했습니다.")
        return len(ids)
    finally:
        c.close()


def backfill_cars(data: "Data", log=print, force: bool = False) -> int:
    """차종 폴더용 차종 정보(mails.cars)를 예전 메일에 한 번 채운다 (retag 를 안 해도 되게)."""
    from .classify import Classifier, latest_part
    c = data.conn()
    try:
        c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        if not force and c.execute("SELECT 1 FROM meta WHERE key='cars_backfilled'").fetchone():
            return 0
        clf = Classifier(data.cfg)
        from .classify import find_cars
        n = 0
        rows = c.execute("SELECT id, subject, body FROM mails").fetchall()
        for r in rows:
            cars = ",".join(find_cars(r["subject"] or "", latest_part(r["body"] or ""), clf.car_known, clf.car_ignore))
            c.execute("UPDATE mails SET cars=? WHERE id=?", (cars, r["id"]))
            n += 1
            if n % 2000 == 0:
                c.commit()
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('cars_backfilled', ?)", (str(n),))
        c.commit()
        if n:
            log(f"차종 정보를 메일 {n:,}건에 채웠습니다 (차종 폴더).")
        return n
    finally:
        c.close()


def _running_version(port: int) -> str | None:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as r:
            return json.loads(r.read()).get("version") or "예전 버전"
    except Exception:
        return None


def _shutdown_old(port: int) -> None:
    import time
    import urllib.request
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/shutdown", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass
    time.sleep(1.5)


def serve(cfg_path: str | None = None, port: int = 8765, open_window: bool = True, log=print) -> None:
    cfg = load_config(cfg_path)
    data = Data(cfg, cfg_path)
    c0 = data.conn()     # 표 준비 확인
    try:
        from .db import ensure_meta_index
        if c0.execute("SELECT 1 FROM sqlite_master WHERE name='idx_mails_meta'").fetchone() is None:
            log("메일함 폴더용 색인을 만드는 중입니다 (처음 한 번, 메일이 많으면 1~2분)...")
            ensure_meta_index(c0)
            log("색인 완료")
    finally:
        c0.close()
    url = f"http://127.0.0.1:{port}/"
    srv = None
    for attempt in range(2):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(data, port))
            break
        except OSError:
            running = _running_version(port)
            if running == APP_VERSION or attempt == 1:
                if running and running != APP_VERSION:
                    log("예전 버전 앱 엔진이 켜져 있어 끄지 못했습니다. 작은 검은 창 '업무 비서'를 닫고 다시 실행하거나,\n"
                        f"PowerShell 에서: Get-NetTCPConnection -LocalPort {port} | % {{ Stop-Process -Id $_.OwningProcess -Force }}")
                else:
                    log(f"업무 비서 앱이 이미 실행 중입니다: {url}")
                if open_window:
                    open_app_window(url)
                return
            log(f"예전 버전 앱 엔진({running or '알 수 없음'})을 끄고 새 버전({APP_VERSION})으로 시작합니다...")
            _shutdown_old(port)
    log(f"업무 비서 앱: {url}  (이 PC 에서만 열립니다. 끄려면 이 창에서 Ctrl+C)")
    from . import sentmove
    sentmove.Scheduler(data.conn, cfg, data.sync, log).start()   # 평일 19~22시 보낸메일 옮기기 알림
    threading.Thread(target=backfill_cars, args=(data, log), daemon=True).start()   # 차종 정보가 없는 예전 메일 채우기
    threading.Thread(target=mark_my_mails, args=(data,), kwargs={"log": log}, daemon=True).start()   # 받은편지함으로 옮긴 내 메일
    if open_window:
        threading.Timer(0.6, open_app_window, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("앱을 종료합니다.")
    finally:
        srv.server_close()
