import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

from mailhub import db, eo as E, eo_images as I, followup as F, mold as M, to as T
from mailhub.classify import thread_key
from mailhub.config import load_config
from mailhub.webapp import Data, make_handler


def seed(d: Path):
    cfg = load_config(d / "none.toml")
    cfg["archive"].update(db_path=str(d / "data" / "m.db"), eo_image_dir=str(d / "data" / "img"),
                          mold_dir=str(d / "data" / "molds"))
    cfg["me"] = {"addresses": ["me@corp.com"], "names": ["나"]}
    c = db.connect(cfg["archive"]["db_path"])
    today = date.today()
    ts = (today - timedelta(days=1)).isoformat() + "T09:00:00"
    m1 = db.insert_mail(c, "a", {"subject": "범퍼 E.O 검토 요청", "sender_name": "고객", "sender_email": "c@oem.com",
                                 "body": "보스 높이 변경 검토 부탁드립니다", "sent_at": ts, "tags": "E.O", "to_addr": "나",
                                 "needs_action": 1, "thread_key": thread_key("범퍼 E.O 검토 요청"), "part_nos": "86511-BN200"})
    E.ensure(c)
    eid = E.add(c, car="CUV", part_no="86511-BN200", part_name="FR BUMPER", change_desc="보스 높이",
                review_due=(today - timedelta(days=2)).isoformat(), mail_id=m1)
    from PIL import Image
    import io
    im = Image.new("RGB", (800, 500), "white")
    b = io.BytesIO()
    im.save(b, "PNG")
    I.save_image(c, cfg, eid, b.getvalue(), "capture", "캡처", force=True)
    F.ensure(c)
    F.add(c, "kim@mold.co.kr", "보스 높이 금형수정 검토 요청", (today - timedelta(days=1)).isoformat(), vendor="A금형",
          requested=(today - timedelta(days=5)).isoformat(), eo_id=eid)
    T.ensure(c)
    T.add(c, part_no="86511-BN200", part_name="FR BUMPER", defect_type="싱크", phenomenon="보스 싱크",
          due=(today - timedelta(days=1)).isoformat())
    M.ensure(c)
    mid = M.add(c, mold_no="MD-1", part_name="FR BUMPER", dest="멕시코")
    M.set_ship(c, mid, etd=(today + timedelta(days=5)).isoformat())
    c.close()
    return cfg, eid, mid


class WebAppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.d = Path(cls.tmp.name)
        cls.cfg, cls.eid, cls.mid = seed(cls.d)
        data = Data(cls.cfg)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
        port = cls.srv.server_address[1]
        cls.srv.RequestHandlerClass = make_handler(data, port)
        cls.base = f"http://127.0.0.1:{port}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        with urllib.request.urlopen(req) as r:
            body = r.read()
            return json.loads(body) if r.headers.get_content_type() == "application/json" else body

    def post(self, path, obj):
        req = urllib.request.Request(self.base + path, data=json.dumps(obj).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    def test_pages_and_reads(self):
        html = self.get("/").decode()
        self.assertTrue(html.startswith("<!doctype html>") and "<title>개발 업무 비서</title>" in html)
        from mailhub.webapp import APP_VERSION
        self.assertEqual(self.get("/api/ping"), {"ok": True, "version": APP_VERSION})
        s = self.get("/api/summary")
        self.assertEqual((s["counts"]["todo"], s["counts"]["req_late"], s["counts"]["eo_late"], s["counts"]["to_late"],
                          s["counts"]["mold_alert"]), (1, 1, 1, 1, 1))
        self.assertEqual({p["kind"] for p in s["priority"]}, {"업체 요청", "E.O", "T/O", "금형 이관", "메일"})
        self.assertTrue(any(a["kind"] == "선적" for a in s["agenda"]))
        mails = self.get("/api/mails?todo=1")
        self.assertEqual(len(mails), 1)
        self.assertEqual(len(self.get("/api/mails?q=" + urllib.request.quote("보스 높이"))), 1)
        m = self.get(f"/api/mail/{mails[0]['id']}")
        self.assertIn("보스 높이", m["body"])
        eo = self.get(f"/api/eo/{self.eid}")
        self.assertEqual(eo["eo"]["delay"][:5], "검토 회신"[:5])
        self.assertEqual(len(eo["images"]), 1)
        self.assertEqual(eo["requests"][0]["vendor"], "A금형")
        img = self.get(eo["images"][0]["url"])
        self.assertEqual(img[:4], b"\x89PNG")
        self.assertIn("싱크", self.get("/api/to/1")["guide"])
        mo = self.get(f"/api/mold/{self.mid}")
        self.assertEqual(mo["mold"]["docs"]["선적일정"][0], "접수")
        self.assertEqual(len(self.get("/api/reqs")), 1)
        self.assertEqual(len(self.get("/api/eos")), 1)
        self.assertEqual(len(self.get("/api/tos")), 1)
        self.assertEqual(len(self.get("/api/molds")), 1)
        # 오늘 화면: 일정을 누르면 관련 메일, 차종 탭용 정보
        self.assertIn("todo_mails", s)
        self.assertTrue(all("cars" in x for x in s["priority"] + s["agenda"]))
        srcs = {a["src"] for a in s["agenda"]}
        self.assertTrue({"req", "eo"} <= srcs, srcs)
        for a in s["agenda"]:
            rel = self.get(f"/api/related/{a['src']}/{a['sid']}")
            self.assertTrue(rel["title"] and isinstance(rel["mails"], list))
        eo_a = next(a for a in s["agenda"] if a["src"] == "eo")
        self.assertTrue(self.get(f"/api/related/eo/{eo_a['sid']}")["mails"])
        mc = self.get("/api/mailcounts")
        self.assertEqual((mc["all"], mc["todo"], mc["tags"]["E.O"]), (1, 1, 1))

    def test_writes(self):
        self.post("/api/mail/1/mark", {"status": "업체검토중"})
        self.assertEqual([m["id"] for m in self.get("/api/mails?status=" + urllib.request.quote("업체검토중"))], [1])
        s = self.get("/api/summary")
        self.assertEqual((s["counts"]["mail_vendor"], [m["id"] for m in s["progress"]]), (1, [1]))
        self.post("/api/mail/1/mark", {"status": ""})                       # 해제
        self.assertEqual(self.get("/api/summary")["progress"], [])
        r = self.post("/api/mails/mark", {"ids": [1, 99999], "status": "진행중"})   # 목록에서 여러 통 선택
        self.assertEqual((r["count"], self.get("/api/mail/1")["status"]), (2, "진행중"))
        self.post("/api/mails/mark", {"ids": [1], "status": ""})
        self.assertEqual(self.get("/api/mail/1")["status"], "")
        self.assertEqual(self.post("/api/mails/mark", {"ids": [], "status": "완료"})["count"], 0)
        self.post("/api/mail/1/mark", {"status": "완료"})
        self.assertEqual(self.get("/api/mail/1")["status"], "완료")
        self.post(f"/api/eo/{self.eid}", {"status": "검토중", "note": "업체 검토 요청"})
        e = self.get(f"/api/eo/{self.eid}")
        self.assertEqual(e["eo"]["status"], "검토중")
        self.assertIn("업체 검토 요청", e["eo"]["note"])
        self.post("/api/to/1", {"status": "원인분석"})
        self.assertEqual(self.get("/api/to/1")["issue"]["status"], "원인분석")
        self.post(f"/api/mold/{self.mid}/doc", {"section": "KD리스트", "status": "확정"})
        self.assertEqual(self.get(f"/api/mold/{self.mid}")["mold"]["docs"]["KD리스트"][0], "확정")
        r0 = self.get("/api/reqs")[0]
        self.post(f"/api/req/{r0['id']}", {"extend": 2})
        self.assertGreater(self.get("/api/reqs")[0]["due_date"], r0["due_date"])
        self.post(f"/api/req/{r0['id']}", {"status": "완료"})
        self.assertEqual(self.get("/api/reqs")[0]["status"], "완료")

    def test_security(self):
        with self.assertRaises(urllib.error.HTTPError) as e:   # 다른 호스트 이름 (DNS 리바인딩)
            self.get("/api/summary", headers={"Host": "evil.example:80"})
        self.assertEqual(e.exception.code, 403)
        req = urllib.request.Request(self.base + "/api/mail/1/mark", data=b"status=x", method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with self.assertRaises(urllib.error.HTTPError) as e:   # 폼 전송(CSRF) 차단
            urllib.request.urlopen(req)
        self.assertEqual(e.exception.code, 415)
        with self.assertRaises(urllib.error.HTTPError) as e:   # 그림 폴더 밖 파일 차단
            self.get("/api/img?path=" + urllib.request.quote(str(self.d / "data" / "m.db")))
        self.assertEqual(e.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.get("/api/eo/9999")
        self.assertEqual(e.exception.code, 404)


if __name__ == "__main__":
    unittest.main()


class OriginalViewTest(unittest.TestCase):
    def test_original_html_is_sandboxed(self):
        import email.message
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            cfg = load_config(d / "none.toml")
            cfg["archive"]["db_path"] = str(d / "m.db")
            m = email.message.EmailMessage()
            m["Subject"], m["From"], m["Date"] = "WIR'G 件", "a@b.c", "Mon, 02 Nov 2026 09:00:00 +0900"
            m.set_content("plain")
            m.add_alternative('<table><tr><td>결재 문서</td></tr></table><img src="cid:img1"><script>alert(1)</script>',
                              subtype="html")
            m.get_payload()[1].add_related(b"\x89PNG fake", maintype="image", subtype="png", cid="<img1>")
            (d / "a.eml").write_bytes(bytes(m))
            c = db.connect(cfg["archive"]["db_path"])
            db.insert_mail(c, "k", {"subject": "x", "file_path": str(d / "a.eml"), "body": "plain"})
            c.commit()
            c.close()
            srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
            port = srv.server_address[1]
            srv.RequestHandlerClass = make_handler(Data(cfg), port)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/mail/1/original") as r:
                    html = r.read().decode()
                    csp = r.headers["Content-Security-Policy"]
                self.assertIn("결재 문서", html)
                self.assertIn("data:image/png;base64,", html)          # 본문 그림 포함
                self.assertIn("default-src 'none'", csp)               # 스크립트·외부 연결 차단
                self.assertNotIn("script-src", csp)
            finally:
                srv.shutdown()
                srv.server_close()


class RestartTest(unittest.TestCase):
    def test_old_engine_is_replaced(self):
        import socket
        from unittest import mock
        from mailhub import webapp
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(Path(d) / "none.toml")
            cfg["archive"]["db_path"] = str(Path(d) / "m.db")
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            from http.server import BaseHTTPRequestHandler

            class OldEngine(BaseHTTPRequestHandler):   # 예전 버전 엔진 흉내 (ping 버전이 다르고 shutdown 지원)
                def log_message(self, *a):
                    pass

                def _ok(self, obj):
                    b = json.dumps(obj).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(b)))
                    self.end_headers()
                    self.wfile.write(b)

                def do_GET(self):
                    self._ok({"ok": True, "version": "예전"})

                def do_POST(self):
                    self._ok({"ok": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()

            old = ThreadingHTTPServer(("127.0.0.1", port), OldEngine)
            threading.Thread(target=old.serve_forever, daemon=True).start()
            orig_shutdown = webapp._shutdown_old

            def shutdown_and_close(p):
                orig_shutdown(p)
                old.server_close()
            with mock.patch.object(webapp, "APP_VERSION", "새버전"), \
                    mock.patch.object(webapp, "load_config", return_value=cfg), \
                    mock.patch.object(webapp.ThreadingHTTPServer, "serve_forever", side_effect=KeyboardInterrupt), \
                    mock.patch("mailhub.sentmove.Scheduler.start"), \
                    mock.patch.object(webapp, "_shutdown_old", side_effect=shutdown_and_close), \
                    mock.patch.object(webapp, "backfill_cars"):
                logs = []
                webapp.serve(None, port=port, open_window=False, log=logs.append)
            self.assertTrue(any("예전 버전 앱 엔진" in x for x in logs), logs)
            self.assertTrue(any("업무 비서 앱: " in x for x in logs), logs)   # 새 엔진으로 시작


class SentMailTest(unittest.TestCase):
    """그룹웨어 보낸편지함 → 받은편지함으로 옮겨 수집한 내 메일을 '보낸 메일'로."""

    def test_moved_sent_mails_become_mine(self):
        from mailhub.classify import Classifier
        from mailhub.webapp import mark_my_mails
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            cfg = load_config(d / "none.toml")
            cfg["archive"]["db_path"] = str(d / "m.db")
            cfg["me"] = {"addresses": ["dh.lee@corp.co.kr"], "names": ["이도현"]}
            clf = Classifier(cfg)
            self.assertTrue(clf.is_my_mail("DH.Lee@corp.co.kr", "", "받은편지함"))
            self.assertTrue(clf.is_my_mail("dohyun@corp.co.kr", "이도현 책임", "받은편지함"))   # 그룹웨어 별칭 주소
            self.assertFalse(clf.is_my_mail("lee@vendor.com", "이도현", "받은편지함"))         # 다른 회사 동명이인
            self.assertFalse(clf.is_my_mail("x@corp.co.kr", "김철수", "받은편지함"))
            c = db.connect(cfg["archive"]["db_path"])
            # 예전 버전으로 '받은 메일'로 저장된 내 메일 + 업체 메일
            mine = db.insert_mail(c, "k1", {"subject": "RE: 범퍼 검토", "sender_name": "이도현 책임",
                                           "sender_email": "dohyun@corp.co.kr", "sent_at": "2026-11-09T18:00:00",
                                           "needs_action": 1, "thread_key": "범퍼 검토"})
            other = db.insert_mail(c, "k2", {"subject": "범퍼 검토", "sender_name": "업체", "sender_email": "a@v.com",
                                            "sent_at": "2026-11-09T09:00:00", "needs_action": 1, "thread_key": "범퍼 검토"})
            c.commit()
            data = Data(cfg)
            self.assertEqual(mark_my_mails(data), 1)
            self.assertEqual(mark_my_mails(data), 0)   # 설정이 그대로면 다시 훑지 않음
            r = c.execute("SELECT is_mine, needs_action FROM mails WHERE id=?", (mine,)).fetchone()
            self.assertEqual((r[0], r[1]), (1, 0))
            rows = data.mails(c, {"special": ["sent"]})
            self.assertEqual([x["id"] for x in rows], [mine])
            self.assertEqual(rows[0]["is_mine"], 1)
            self.assertEqual(data.mailcounts(c)["sent"], 1)
            for i in range(5):   # 목록 '더 보기': offset 으로 이어서
                db.insert_mail(c, f"s{i}", {"subject": f"보낸 {i}", "sender_email": "230419@corp.co.kr", "is_mine": 1,
                                            "sender_name": "이도현", "sent_at": f"2026-11-0{i + 1}T10:00:00"})
            c.commit()
            p1 = data.mails(c, {"special": ["sent"], "limit": ["4"]})
            p2 = data.mails(c, {"special": ["sent"], "limit": ["4"], "offset": ["4"]})
            self.assertEqual((len(p1), len(p2)), (4, 2))
            self.assertFalse({x["id"] for x in p1} & {x["id"] for x in p2})
            self.assertTrue(p1[-1]["sent_at"] >= p2[0]["sent_at"])
            from mailhub.search import todo
            self.assertNotIn(other, [x["id"] for x in todo(c, "2026-01-01", 100)])   # 내가 회신한 건은 회신 필요에서 빠짐
            c.close()
