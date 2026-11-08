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
        self.assertEqual(self.get("/api/ping"), {"ok": True})
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

    def test_writes(self):
        self.post("/api/mail/1/mark", {"status": "업체검토중"})
        self.assertEqual([m["id"] for m in self.get("/api/mails?status=" + urllib.request.quote("업체검토중"))], [1])
        s = self.get("/api/summary")
        self.assertEqual((s["counts"]["mail_vendor"], [m["id"] for m in s["progress"]]), (1, [1]))
        self.post("/api/mail/1/mark", {"status": ""})                       # 해제
        self.assertEqual(self.get("/api/summary")["progress"], [])
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
