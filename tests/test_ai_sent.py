import json
import os
import stat
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from mailhub import ai, db, sentmove
from mailhub.config import load_config
from mailhub.webapp import Data, make_handler

FAKE_REVIEW = "### 1) 요약\n- 회신 기한 11/12\n\n| No | 요청 |\n|---|---|\n| 1 | 검토 |\n"
FAKE_EO = 'E.O 항목입니다\n```json\n[{"eo_no":"EO26-0871","car":"NX5","part_no":"84634-NX100","part_name":"BRKT","change_desc":"PAD 변경","review_due":"2026-11-14"}]\n```'


def fake_claude(d: Path, output: str) -> Path:
    """PATH 에 둘 가짜 claude (받은 인자를 기록하고 정해진 결과 출력)."""
    b = d / "bin"
    b.mkdir(exist_ok=True)
    (b / "out.txt").write_text(output, encoding="utf-8")
    p = b / "claude"
    p.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{b}/args.txt"\ncat "{b}/out.txt"\n')
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return b


class AiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.cfg = load_config(self.d / "x.toml")
        self.cfg["archive"]["db_path"] = str(self.d / "data" / "m.db")
        c = db.connect(self.cfg["archive"]["db_path"])
        db.insert_mail(c, "k", {"subject": "[NX5 HEV] 콘솔 E.O 통보 EO26-0871", "part_nos": "84634-NX100,84650-NX000",
                                "sender_name": "설계", "tags": "E.O"})
        c.commit()
        c.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _wait(self, key):
        for _ in range(100):
            j = ai.job(key)
            if j and j["state"] != "running":
                return j
            time.sleep(0.05)
        self.fail("작업이 끝나지 않음")

    def test_review_saved_and_read_only_tools(self):
        b = fake_claude(self.d, FAKE_REVIEW)
        with mock.patch.dict(os.environ, {"PATH": f"{b}:{os.environ['PATH']}"}):
            ai.start_review(self.cfg, 1)
            j = self._wait("review:1")
        self.assertEqual(j["state"], "done", j.get("error"))
        self.assertIn("회신 기한", ai.saved_review(self.cfg, 1)["text"])
        args = (b / "args.txt").read_text(encoding="utf-8")
        self.assertIn("메일 #1 을 검토해줘", args)                      # mail-review 명령 내용 + 번호
        self.assertIn("Bash(python -m mailhub show:*)", args)
        self.assertNotIn("mark", args.split("--allowedTools")[1].split("\n앱")[0].replace("mark ", ""))  # 쓰기 명령 미허용

    def test_eo_extract_claude_and_fallback(self):
        b = fake_claude(self.d, FAKE_EO)
        with mock.patch.dict(os.environ, {"PATH": f"{b}:{os.environ['PATH']}"}):
            ai.start_eo_extract(self.cfg, 1, Data(self.cfg).conn)
            j = self._wait("eo:1")
        self.assertEqual(j["result"]["by"], "Claude")
        self.assertEqual(j["result"]["items"][0]["part_no"], "84634-NX100")
        with mock.patch.object(ai, "find_claude", return_value=None):
            ai._jobs.pop("eo:1", None)
            ai.start_eo_extract(self.cfg, 1, Data(self.cfg).conn)
            j = self._wait("eo:1")
        self.assertEqual(j["result"]["by"], "기본값")
        self.assertEqual([x["part_no"] for x in j["result"]["items"]], ["84634-NX100", "84650-NX000"])
        self.assertEqual(j["result"]["items"][0]["eo_no"], "EO26-0871")

    def test_parse_json_block_errors(self):
        with self.assertRaises(ValueError):
            ai.parse_json_block("항목이 없습니다")


class SentMoveTest(unittest.TestCase):
    def test_reminder_logic(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(Path(d) / "x.toml")
            cfg["archive"]["db_path"] = str(Path(d) / "m.db")
            factory = lambda: db.connect(cfg["archive"]["db_path"])  # noqa: E731
            wed_19 = datetime(2026, 11, 11, 19, 0)
            c = factory()
            self.assertFalse(sentmove.status(c, cfg, datetime(2026, 11, 11, 18, 0))["show"])   # 19시 전
            self.assertTrue(sentmove.status(c, cfg, wed_19)["show"])
            self.assertFalse(sentmove.status(c, cfg, datetime(2026, 11, 14, 20, 0))["show"])  # 토요일
            c.close()
            synced = []

            def sync():   # '옮겼어요' 후 수집 → 오늘 보낸 메일이 들어옴
                c2 = factory()
                db.insert_mail(c2, "s1", {"subject": "RE: 회신", "is_mine": 1, "sent_at": "2026-11-11T17:30:00"})
                c2.commit()
                c2.close()
                synced.append(1)

            with mock.patch("mailhub.followup.notify"):
                self.assertEqual(sentmove.check_and_ask(factory, cfg, sync, log=lambda *_: None, now=wed_19,
                                                        asker=lambda: False), "나중에")
                self.assertEqual(sentmove.check_and_ask(factory, cfg, sync, log=lambda *_: None, now=wed_19,
                                                        asker=lambda: True), "수집 1건")
                # 수집된 뒤에는 다시 묻지 않음
                self.assertEqual(sentmove.check_and_ask(factory, cfg, sync, log=lambda *_: None,
                                                        now=datetime(2026, 11, 11, 20, 0), asker=lambda: self.fail()), "이미 수집됨")
            c = factory()
            self.assertFalse(sentmove.status(c, cfg, wed_19)["show"])
            c.close()
            # 스케줄러: 같은 시각은 하루 한 번, 정해진 시각에만
            sch = sentmove.Scheduler(factory, cfg, sync, log=lambda *_: None)
            with mock.patch.object(sentmove, "check_and_ask", return_value="ok") as cka:
                sch.tick(datetime(2026, 11, 12, 19, 0))
                sch.tick(datetime(2026, 11, 12, 19, 0))
                sch.tick(datetime(2026, 11, 12, 19, 30))
                sch.tick(datetime(2026, 11, 12, 20, 0))
            self.assertEqual(cka.call_count, 2)


class EndpointTest(unittest.TestCase):
    def test_original_header_eo_add_sentmove(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(Path(d) / "x.toml")
            cfg["archive"]["db_path"] = str(Path(d) / "m.db")
            c = db.connect(cfg["archive"]["db_path"])
            db.insert_mail(c, "k", {"subject": "결재 문서", "body": "본문", "sender_name": "보낸이", "sender_email": "a@x.com",
                                    "to_addr": "김철수 <k@x.com>; 이영희 <l@x.com>", "cc_addr": "박민수 <p@x.com>",
                                    "sent_at": "2026-11-05T15:53:00", "attachments": "a.pdf"})
            c.commit()
            c.close()
            srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
            port = srv.server_address[1]
            srv.RequestHandlerClass = make_handler(Data(cfg), port)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{port}/api/"

            def get(p):
                with urllib.request.urlopen(base + p) as r:
                    return r.read().decode()

            def post(p, obj):
                req = urllib.request.Request(base + p, data=json.dumps(obj).encode(), method="POST",
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req) as r:
                    return json.loads(r.read())
            try:
                h = get("mail/1/original")
                for t in ("받는사람", "김철수 &lt;k@x.com&gt;; 이영희", "참조", "박민수", "2026-11-05 15:53", "a.pdf"):
                    self.assertIn(t, h)
                r = post("eo/add", {"mail_id": 1, "items": [
                    {"eo_no": "EO1", "car": "NX5", "part_no": "84634-NX100", "change_desc": "PAD", "review_due": "2026-11-14"},
                    {"car": "", "part_no": "", "part_name": ""}]})        # 빈 항목은 건너뜀
                self.assertEqual(len(r["ids"]), 1)
                eo = json.loads(get(f"eo/{r['ids'][0]}"))
                self.assertEqual((eo["eo"]["review_due"], eo["mails"][0]["id"]), ("2026-11-14", 1))
                st = json.loads(get("sentmove"))
                self.assertIn("sent_today", st)
                a = json.loads(get("ai/review/1"))
                self.assertIsNone(a["saved"])
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
