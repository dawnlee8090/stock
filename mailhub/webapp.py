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
SKELETON_HEAD = ('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
                 '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
                 '<style>:root{color-scheme:light}body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style>'
                 '</head><body>')


def _iso(d: date) -> str:
    return d.isoformat()


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
        from . import eo, eo_images, followup, mold, to
        followup.ensure(c)
        eo.ensure(c)
        to.ensure(c)
        mold.ensure(c)
        eo_images.ensure(c)
        return c

    # ---- 공통 ----
    @staticmethod
    def mail_row(r, q: str = "") -> dict:
        from .search import snippet
        from .textutil import readable_body
        return {k: r[k] for k in ("id", "sent_at", "sender_name", "sender_email", "subject", "tags", "part_nos",
                                  "attachments", "needs_action", "status")} | {
            "snippet": snippet(readable_body(r["body"]), q, att_text=r["att_text"])}

    def img_url(self, path: str) -> str:
        return "/api/img?path=" + quote(path)

    # ---- 오늘 ----
    def summary(self, c) -> dict:
        from . import eo as E, mold as M, to as T
        from .search import todo
        today = date.today()
        since30 = _iso(today - timedelta(days=30))
        todo_rows = todo(c, since30, 200)
        reqs = c.execute("SELECT * FROM requests WHERE status='요청' ORDER BY due_date").fetchall()
        req_late = [r for r in reqs if r["due_date"] <= _iso(today)]
        eos = E.listing(c, "open")
        tos = T.listing(c, "open")
        molds = M.listing(c, "open")
        eo_late = [r for r in eos if E.delay_of(r)]
        to_late = [r for r in tos if T.delay_of(r)]
        mold_al = [(m, M.alerts(c, m)) for m in molds]
        mold_al = [(m, a) for m, a in mold_al if a]
        prio = []
        for r in req_late:
            n = (date.fromisoformat(r["due_date"]) - today).days
            prio.append({"kind": "업체 요청", "sev": "crit" if n < 0 else "warn", "title": r["subject"],
                         "sub": r["vendor"] or r["to_addrs"], "alert": "오늘 회신 기한" if n == 0 else f"회신 {-n}일 지남",
                         "go": "#req", "id": r["id"]})
        for r in eo_late:
            prio.append({"kind": "E.O", "sev": "crit", "title": f"{r['car']} {r['part_name'] or r['part_no']}",
                         "sub": r["change_desc"], "alert": E.delay_of(r), "go": "#eo", "id": r["id"]})
        for r in to_late:
            prio.append({"kind": "T/O", "sev": "crit", "title": f"{r['part_name'] or r['part_no']} · {r['defect_type']}",
                         "sub": r["phenomenon"], "alert": T.delay_of(r), "go": "#to", "id": r["id"]})
        for m, a in mold_al:
            prio.append({"kind": "금형 이관", "sev": "warn", "title": f"{m['mold_no']} {m['part_name']} → {m['dest']}",
                         "sub": a[0], "alert": "확인 필요", "go": "#mold", "id": m["id"]})
        for r in todo_rows[:30]:
            prio.append({"kind": "메일", "sev": "info", "title": r["subject"], "sub": r["sender_name"],
                         "alert": "회신 필요", "go": "#mail", "id": r["id"]})
        lo, hi = _iso(today - timedelta(days=3)), _iso(today + timedelta(days=14))
        agenda = []
        for r in reqs:
            agenda.append({"date": r["due_date"], "kind": "요청", "text": f"{r['vendor'] or r['to_addrs']} 회신 기한"})
        for r in eos:
            for f, label in (("review_due", "검토 회신"), ("mold_due", "금형 수정 완료"), ("verify_due", "검증")):
                if r[f]:
                    agenda.append({"date": r[f], "kind": "E.O", "text": f"{r['part_name'] or r['part_no']} {label}"})
        for r in tos:
            if r["due"]:
                agenda.append({"date": r["due"], "kind": "T/O", "text": f"{r['part_name'] or r['part_no']} {r['defect_type']} 대책"})
            if r["verify_due"]:
                agenda.append({"date": r["verify_due"], "kind": "T/O", "text": f"{r['part_name'] or r['part_no']} 효과 확인"})
        for m in molds:
            s = M.ship(c, m["id"])
            if s and s["etd"] and not s["atd"]:
                agenda.append({"date": s["etd"], "kind": "선적", "text": f"{m['mold_no']} ETD → {m['dest']}"})
            if s and s["eta"] and not s["ata"]:
                agenda.append({"date": s["eta"], "kind": "도착", "text": f"{m['mold_no']} ETA {s['port_to']}"})
        agenda = sorted([a for a in agenda if lo <= a["date"] <= hi], key=lambda a: a["date"])
        recent = c.execute("SELECT * FROM mails WHERE is_mine=0 ORDER BY sent_at DESC LIMIT 6").fetchall()
        last = c.execute("SELECT MAX(ingested_at) FROM mails").fetchone()[0]
        return {
            "counts": {"todo": len(todo_rows), "req_late": len(req_late), "req_open": len(reqs), "eo_late": len(eo_late),
                       "eo_open": len(eos), "to_late": len(to_late), "to_open": len(tos), "mold_alert": len(mold_al),
                       "mold_open": len(molds)},
            "priority": prio, "agenda": agenda, "recent": [self.mail_row(r) for r in recent], "last_sync": last,
        }

    # ---- 메일 ----
    def mails(self, c, q: dict) -> list:
        from .search import search, todo
        query = (q.get("q") or [""])[0]
        limit = int((q.get("limit") or ["60"])[0])
        if (q.get("todo") or [""])[0] == "1" and not query:
            rows = todo(c, _iso(date.today() - timedelta(days=30)), limit)
        else:
            rows = search(c, query, tag=(q.get("tag") or [None])[0], limit=limit,
                          sort="relevance" if query else "date")
        return [self.mail_row(r, query) for r in rows]

    def mail(self, c, mid: int) -> dict:
        r = c.execute("SELECT * FROM mails WHERE id=?", (mid,)).fetchone()
        if not r:
            raise KeyError(f"#{mid} 메일이 없습니다")
        d = self.mail_row(r)
        from .textutil import readable_body
        d.update(body=readable_body(r["body"])[:30000], att_text=(r["att_text"] or "")[:20000])
        return d

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
                    return self._json({"ok": True})
                if parts == ["img"]:
                    p = Path(unquote((q.get("path") or [""])[0])).resolve()
                    if img_root not in p.parents or not p.is_file():
                        return self._send(404, b"not found", "text/plain")
                    ctype = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
                    return self._send(200, p.read_bytes(), ctype)
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
                if parts == ["sync"]:
                    return self._json(data.sync())
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


def serve(cfg_path: str | None = None, port: int = 8765, open_window: bool = True, log=print) -> None:
    cfg = load_config(cfg_path)
    data = Data(cfg, cfg_path)
    data.conn().close()  # 표 준비 확인
    url = f"http://127.0.0.1:{port}/"
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(data, port))
    except OSError:
        # 이미 실행 중이면 창만 다시 연다
        log(f"업무 비서 앱이 이미 실행 중입니다: {url}")
        if open_window:
            open_app_window(url)
        return
    log(f"업무 비서 앱: {url}  (이 PC 에서만 열립니다. 끄려면 이 창에서 Ctrl+C)")
    from . import sentmove
    sentmove.Scheduler(data.conn, cfg, data.sync, log).start()   # 평일 19~22시 보낸메일 옮기기 알림
    if open_window:
        threading.Timer(0.6, open_app_window, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("앱을 종료합니다.")
    finally:
        srv.server_close()
