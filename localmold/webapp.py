"""현지 금형 육성 관리 - 화면 엔진 (이 PC 에서만: 127.0.0.1).

python -m localmold app  →  http://127.0.0.1:8766 를 앱 창으로 엽니다.
화면은 web/index.html 한 파일. 파일로 그냥 열면(예: 아티팩트) 예시 데이터로 동작한다.
업무 비서(mailhub, 8765)와는 따로 켜고 끈다.
"""
from __future__ import annotations

import base64
import json
import os
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import core as LM
from .config import load_config

WEB = Path(__file__).parent / "web" / "index.html"
# 화면(index.html 의 APP_VERSION)과 같이 올린다. 다르면 화면이 '앱을 다시 켜 주세요' 안내를 띄우고,
# 새로 실행할 때 예전 엔진이 켜져 있으면 끄고 새로 시작한다.
APP_VERSION = "2026-11-12.1"
SKELETON_HEAD = ('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
                 '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
                 '<style>:root{color-scheme:light}body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style>'
                 '</head><body>')


def _b64(v) -> bytes:
    """'data:image/jpeg;base64,....' 또는 base64 문자열 → bytes"""
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


class Data:
    def __init__(self, cfg: dict, cfg_path: str | None = None):
        self.cfg = cfg
        self.cfg_path = cfg_path

    def conn(self):
        return LM.connect(self.cfg["db_path"])

    @staticmethod
    def img_url(path: str) -> str:
        return "/api/img?path=" + quote(str(path))

    def _photo(self, p) -> dict:
        return {"id": p["id"], "grp": p["grp"], "url": self.img_url(p["path"]),
                "marked": self.img_url(p["marked_path"]) if p["marked_path"] else "",
                "marks": json.loads(p["marks"]) if p["marks"] else [], "w": p["w"], "h": p["h"], "source": p["source"]}

    def _row(self, r, thumb=None, n_photo: int = 0) -> dict:
        return dict(r) | {"delay": LM.delay_of(r), "wait": LM.wait_check(r), "photos": n_photo,
                          "thumb": self._photo(thumb) if thumb else None}

    # ---- 전체 현황 (차종별 요약 + 작업 우선순위) ----
    def summary(self, c) -> dict:
        cars = LM.cars(c)
        top = LM.priority(c, limit=15)
        thumbs = LM.first_photos(c, [r["id"] for r in top])
        tot = {k: sum(x[k] for x in cars) for k in ("molds", "issues", "open", "hi_open", "late", "wait", "closed")}
        tot["close_rate"] = round(tot["closed"] / tot["issues"] * 100) if tot["issues"] else 0
        stages = {s: 0 for s in LM.STATUSES}
        for s, n in c.execute("SELECT status, COUNT(*) FROM lm_issues GROUP BY status"):
            stages[s] = n
        by_car = {}
        for car, s, n in c.execute("SELECT car, status, COUNT(*) FROM lm_issues GROUP BY car, status"):
            by_car.setdefault(car, {})[s] = n
        for x in cars:
            x["stages"] = by_car.get(x["car"], {})
        return {"cars": cars, "total": tot, "stages": stages, "phone_page": self.cfg.get("phone_page", ""),
                "top": [r | {"thumb": self._photo(thumbs[r["id"]]) if r["id"] in thumbs else None} for r in top]}

    def top(self, c, q: dict) -> dict:
        car = (q.get("car") or [""])[0] or None
        rows = LM.priority(c, car, limit=min(int((q.get("limit") or ["60"])[0]), 300))
        thumbs = LM.first_photos(c, [r["id"] for r in rows])
        return {"issues": [r | {"thumb": self._photo(thumbs[r["id"]]) if r["id"] in thumbs else None} for r in rows]}

    # ---- 차종 화면 ----
    def board(self, c, q: dict) -> dict:
        cars = LM.cars(c)
        car = (q.get("car") or [""])[0] or (cars[0]["car"] if cars else "")
        vendor = (q.get("vendor") or [""])[0]
        ms = LM.molds(c, car, vendor or None) if car else []
        rows = LM.issues(c, car, vendor or None) if car else []
        thumbs, pc = LM.first_photos(c, [r["id"] for r in rows]), LM.photo_counts(c)
        return {"cars": cars, "car": car, "vendor": vendor, "molds": ms, "cats": LM.CATS, "stages": LM.STATUSES,
                "issues": [self._row(r, thumbs.get(r["id"]), pc.get(r["id"], 0)) for r in rows]}

    def find(self, c, q: dict) -> dict:
        """위 검색창: 모든 차종에서 품번 · 품명 · 문제점 · 대책 · 외주처 · L번호."""
        t = ((q.get("q") or [""])[0]).strip()
        if not t:
            return {"q": t, "issues": []}
        rows = list(LM.issues(c, q=t))
        if t.upper().lstrip("L").isdigit():
            r = LM.get_issue(c, int(t.upper().lstrip("L")))
            if r and all(x["id"] != r["id"] for x in rows):
                rows.insert(0, r)
        rows = rows[:300]
        thumbs, pc = LM.first_photos(c, [r["id"] for r in rows]), LM.photo_counts(c)
        return {"q": t, "issues": [self._row(r, thumbs.get(r["id"]), pc.get(r["id"], 0)) for r in rows]}

    def mold(self, c, mid: int) -> dict:
        m = LM.get_mold(c, mid)
        if not m:
            raise KeyError(f"금형 {mid} 이 없습니다")
        ms = next((x for x in LM.molds(c, m["car"], m["vendor"]) if x["id"] == mid), dict(m))
        out = []
        for r in LM.issues(c, mold_id=mid):
            out.append(self._row(r) | {"photo_list": [self._photo(p) for p in LM.photos(c, r["id"])]})
        return {"mold": ms, "issues": out}

    def issue(self, c, iid: int) -> dict:
        r = LM.get_issue(c, iid)
        if not r:
            raise KeyError(f"L{iid} 문제점이 없습니다")
        prev = LM.get_issue(c, r["recur_of"]) if r["recur_of"] else None
        logs = [{"at": x["at"], "what": _log_what(x), "note": x["note"]} for x in LM.log(c, iid)]
        return {"issue": self._row(r), "mold": dict(LM.get_mold(c, r["mold_id"]) or {}),
                "photos": [self._photo(p) for p in LM.photos(c, iid)], "previous": dict(prev) if prev else None,
                "log": logs, "guide": LM.guide_text(r["defect_type"]) if r["defect_type"] in LM.GUIDE else ""}

    def post(self, c, path: list[str], body: dict) -> dict:
        fields = {k: body[k] for k in LM.ISSUE_FIELDS if k in body}
        if path[0] == "export":
            car = str(body.get("car") or "")
            if not car:
                raise ValueError("차종을 골라 주세요")
            paths = LM.export(c, self.cfg, car, str(body.get("vendor") or "") or None)
            p = (paths[0] if body.get("vendor") else paths[0].parent).resolve()
            if os.name == "nt":
                os.startfile(str(p))  # type: ignore[attr-defined]
            return {"ok": True, "path": str(p), "files": len(paths)}
        if path[0] == "issue" and path[1] == "add":
            iid = LM.add_issue(c, int(body["mold_id"]), **fields)
            return {"ok": True, "id": iid}
        if path[0] == "issue" and path[2] == "photo":
            pid = LM.save_photo(c, self.cfg, int(path[1]), _b64(body.get("data")), str(body.get("grp") or "before"),
                                marks=body.get("marks") or None, flat=_b64(body.get("flat")) if body.get("flat") else None)
            if not pid:
                raise ValueError("같은 사진이 이미 있습니다")
            p = c.execute("SELECT * FROM lm_photos WHERE id=?", (pid,)).fetchone()
            return {"ok": True, "photo": self._photo(p)}
        if path[0] == "issue" and path[2] == "del":
            LM.delete_issue(c, self.cfg, int(path[1]))
            return {"ok": True}
        if path[0] == "issue":
            r = LM.update_issue(c, int(path[1]), note=(body.get("note") or "").strip() or None, **fields)
            return {"ok": True, "issue": self._row(r)}
        if path[0] == "photo":
            pid = int(path[1])
            act = path[2]
            if act == "del":
                LM.remove_photo(c, pid)
            elif act == "move":
                LM.move_photo(c, pid, int(body.get("step") or 1))
            else:
                LM.set_marks(c, self.cfg, pid, body.get("marks") or [], _b64(body.get("flat")) if body.get("flat") else None)
            p = c.execute("SELECT * FROM lm_photos WHERE id=?", (pid,)).fetchone()
            return {"ok": True, "photo": self._photo(p) if p else None}
        if path[0] == "mold" and path[1] == "add":
            kw = {k: str(body.get(k) or "") for k in LM.MOLD_FIELDS}
            mid, new = LM.upsert_mold(c, str(body.get("car") or ""), str(body.get("vendor") or ""), str(body.get("part_no") or ""), **kw)
            c.commit()
            return {"ok": True, "id": mid, "new": new}
        if path[0] == "import":   # 엑셀/CSV 올리기: 외주처 엑셀(갑지+을지+사진) · 통합 문제점 리스트 · 금형 리스트 자동 구분
            car = str(body.get("car") or "").strip()
            if not car:
                raise ValueError("차종을 입력해 주세요")
            name, data = str(body.get("name") or "upload.xlsx"), _b64(body.get("data"))
            vendor = str(body.get("vendor") or "").strip().upper() or LM.vendor_from_name(name)
            return {"ok": True} | LM.import_any(c, self.cfg, car, name, data, vendor or None)
        raise KeyError("/".join(p for p in path if p))


def make_handler(data: Data, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    img_root = Path(data.cfg["photo_dir"]).resolve()

    class H(BaseHTTPRequestHandler):
        server_version = "localmold"

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
                    return self._json({"ok": True, "version": APP_VERSION, "app": "localmold"})
                if parts == ["img"]:
                    p = Path(unquote((q.get("path") or [""])[0])).resolve()
                    if img_root not in p.parents or not p.is_file():
                        return self._send(404, b"not found", "text/plain")
                    ctype = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
                    return self._send(200, p.read_bytes(), ctype)
                c = data.conn()
                try:
                    if parts == ["summary"]:
                        return self._json(data.summary(c))
                    if parts == ["top"]:
                        return self._json(data.top(c, q))
                    if parts == ["board"]:
                        return self._json(data.board(c, q))
                    if parts == ["find"]:
                        return self._json(data.find(c, q))
                    if parts[0] == "mold" and len(parts) == 2:
                        return self._json(data.mold(c, int(parts[1])))
                    if parts[0] == "issue" and len(parts) == 2:
                        return self._json(data.issue(c, int(parts[1])))
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


def _running(port: int) -> dict | None:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as r:
            return json.loads(r.read())
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


def serve(cfg_path: str | None = None, port: int | None = None, open_window: bool = True, log=print) -> None:
    from mailhub.webapp import open_app_window   # Edge/Chrome 앱 창 (업무 비서와 같은 방식)
    cfg = load_config(cfg_path)
    port = port or int(cfg.get("port") or 8766)
    data = Data(cfg, cfg_path)
    data.conn().close()     # 표 준비 확인
    url = f"http://127.0.0.1:{port}/"
    srv = None
    for attempt in range(2):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(data, port))
            break
        except OSError:
            info = _running(port) or {}
            if info and info.get("app") != "localmold":
                log(f"{port} 번 주소를 다른 프로그램이 쓰고 있습니다. localmold.toml 에 port = 8767 처럼 다른 번호를 적어 주세요.")
                return
            running = info.get("version")
            if running == APP_VERSION or attempt == 1:
                if running and running != APP_VERSION:
                    log("예전 버전 엔진이 켜져 있어 끄지 못했습니다. 작은 검은 창 '현지 금형'을 닫고 다시 실행하세요.")
                else:
                    log(f"현지 금형 앱이 이미 실행 중입니다: {url}")
                if open_window:
                    open_app_window(url)
                return
            log(f"예전 버전 엔진({running or '알 수 없음'})을 끄고 새 버전({APP_VERSION})으로 시작합니다...")
            _shutdown_old(port)
    log(f"현지 금형 육성 관리: {url}  (이 PC 에서만 열립니다. 끄려면 이 창에서 Ctrl+C)  · 오늘 {date.today().isoformat()}")
    if open_window:
        threading.Timer(0.6, open_app_window, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("앱을 종료합니다.")
    finally:
        srv.server_close()
