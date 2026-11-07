import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

from mailhub import db
from mailhub.config import load_config
from mailhub.ingest_files import ingest_folder
from mailhub.search import search


def make_eml(folder: Path, name: str, subject: str, body: str, sender: str, date: str,
             mid: str | None, attach: str | None = None):
    m = EmailMessage()
    m["Subject"], m["From"], m["To"], m["Date"] = subject, sender, "me@corp.com", date
    if mid:
        m["Message-ID"] = mid
    m.set_content(body)
    if attach:
        m.add_attachment(b"x", maintype="application", subtype="pdf", filename=attach)
    (folder / name).write_bytes(bytes(m))


class MailhubTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.mails = root / "mails"
        self.mails.mkdir()
        self.cfg = load_config(root / "none.toml")
        self.cfg["me"]["addresses"] = ["me@corp.com"]
        self.conn = db.connect(root / "t.db")
        make_eml(self.mails, "a.eml", "[긴급] 84710-AA000 CRASH PAD E.O 변경 검토 요청",
                 "안녕하세요.\nE.O 변경에 따른 금형 수정 가능 여부 검토 부탁드립니다.\n회신 기한: 11/15",
                 "고객사 담당 <cust@oem.com>", "Mon, 02 Nov 2026 09:00:00 +0900", "<a1@oem.com>", "EO_84710.pdf")
        make_eml(self.mails, "b.eml", "RE: T/O 결과 공유",
                 "1차 T/O 결과 웰드라인 및 싱크 발생. 게이트 위치 변경 검토 중입니다.\n\n"
                 "-----Original Message-----\n검토 부탁드립니다.",
                 "금형업체 <mold@vendor.com>", "Tue, 03 Nov 2026 10:00:00 +0900", "<b1@vendor.com>")
        make_eml(self.mails, "c.eml", "검사구 제작 일정", "검사구 제작 일정 공유드립니다.",
                 "나 <me@corp.com>", "Wed, 04 Nov 2026 11:00:00 +0900", None)
        # 같은 메일이 다른 백업에 또 있는 경우
        make_eml(self.mails, "z_dup.eml", "[긴급] 84710-AA000 CRASH PAD E.O 변경 검토 요청", "중복",
                 "고객사 담당 <cust@oem.com>", "Mon, 02 Nov 2026 09:00:00 +0900", "<A1@oem.com>")
        self.stats = ingest_folder(self.conn, self.cfg, self.mails, log=lambda *_: None)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_dedup(self):
        self.assertEqual(self.stats["added"], 3)
        self.assertEqual(self.stats["skipped"], 1)

    def test_search_korean_short_and_long(self):
        self.assertEqual(len(search(self.conn, "금형")), 2)          # 2글자 → LIKE
        self.assertEqual(len(search(self.conn, "웰드라인")), 1)       # 3글자 이상 → FTS
        self.assertEqual(len(search(self.conn, "검사구 일정")), 1)    # AND
        self.assertEqual(len(search(self.conn, "금형 -웰드")), 1)     # 제외어
        self.assertEqual(len(search(self.conn, "", attach_name="EO_84710")), 1)
        self.assertEqual(len(search(self.conn, "", part_no="84710-AA000")), 1)
        self.assertEqual(len(search(self.conn, "", sender="vendor")), 1)
        self.assertEqual(len(search(self.conn, "", since="2026-11-03", until="2026-11-03")), 1)

    def test_classify(self):
        a = search(self.conn, "CRASH PAD")[0]
        self.assertIn("E.O", a["tags"])
        self.assertIn("금형수정", a["tags"])
        self.assertEqual(a["part_nos"], "84710-AA000")
        self.assertEqual(a["needs_action"], 1)
        b = search(self.conn, "웰드라인")[0]
        self.assertIn("T/O", b["tags"])
        self.assertIn("품질문제", b["tags"])
        self.assertEqual(b["needs_action"], 0)   # '검토 부탁'은 인용된 이전 메일에만 있음
        c = search(self.conn, "검사구 제작")[0]
        self.assertEqual(c["needs_action"], 0)   # 내가 보낸 메일
        self.assertEqual(len(search(self.conn, "", tag="T/O")), 1)


if __name__ == "__main__":
    unittest.main()


class ClassifierRulesTest(unittest.TestCase):
    def setUp(self):
        from mailhub.classify import Classifier
        cfg = load_config("none.toml")
        cfg["me"] = {"addresses": ["230419@corp.com"], "names": ["이성욱"]}
        self.c = Classifier(cfg)

    def test_word_boundary_and_quote(self):
        r = self.c.classify({"subject": "second sample", "body": "economy", "to_addr": "이성욱"})
        self.assertNotIn("E.O", r["tags"])
        r = self.c.classify({"subject": "ECO 적용", "body": "", "to_addr": "이성욱"})
        self.assertIn("E.O", r["tags"])
        r = self.c.classify({"subject": "RE: 공유", "body": "참고 바랍니다.\n보낸 사람: x\n일정 검토 부탁", "to_addr": "이성욱"})
        self.assertNotIn("일정", r["tags"])
        self.assertEqual(r["needs_action"], 0)

    def test_action_rules(self):
        base = {"subject": "RE: 금형 수정 검토 요청", "body": "검토 부탁드립니다."}
        self.assertEqual(self.c.classify({**base, "to_addr": "이성욱/책임/개발3팀"})["needs_action"], 1)
        self.assertEqual(self.c.classify({**base, "to_addr": "김동혁", "cc_addr": "이성욱"})["needs_action"], 0)
        mine = self.c.classify({**base, "sender_email": "230419@corp.com", "to_addr": "x"})
        self.assertEqual((mine["is_mine"], mine["needs_action"]), (1, 0))
        self.assertEqual(mine["thread_key"], "금형 수정 검토 요청")
        self.assertEqual(self.c.classify({"subject": "회신: 공유드립니다", "body": "", "to_addr": "이성욱"})["needs_action"], 0)
        # 시스템 알림 / 전사 공지 / 단체 메일
        self.assertEqual(self.c.classify({**base, "to_addr": "이성욱", "sender_email": "postmaster@corp.com"})["needs_action"], 0)
        self.assertEqual(self.c.classify({**base, "subject": "[전사 보안공지] 확인 요청", "to_addr": "이성욱"})["needs_action"], 0)
        many = "; ".join(["이성욱"] + [f"사원{i}" for i in range(15)])
        self.assertEqual(self.c.classify({**base, "to_addr": many})["needs_action"], 0)


class ReplyTrackingTest(unittest.TestCase):
    def test_replied_thread_excluded(self):
        from mailhub.classify import Classifier
        from mailhub.search import todo
        cfg = load_config("none.toml")
        cfg["me"] = {"addresses": ["me@corp.com"], "names": ["나"]}
        clf = Classifier(cfg)
        with tempfile.TemporaryDirectory() as d:
            conn = db.connect(Path(d) / "t.db")
            def add(k, subj, sender, to, at, folder="받은 편지함"):
                rec = {"subject": subj, "body": "검토 요청드립니다", "sender_email": sender, "to_addr": to,
                       "sent_at": at, "folder": folder}
                rec.update(clf.classify(rec))
                db.insert_mail(conn, k, rec)
            add("1", "A건 검토 요청", "c@oem.com", "나", "2026-11-01T09:00:00")
            add("2", "B건 검토 요청", "c@oem.com", "나", "2026-11-01T09:00:00")
            add("3", "RE: A건 검토 요청", "me@corp.com", "c@oem.com", "2026-11-02T09:00:00", "보낸 편지함")
            self.assertEqual([r["subject"] for r in todo(conn, "2026-01-01")], ["B건 검토 요청"])
            self.assertEqual(len(todo(conn, "2026-01-01", include_replied=True)), 2)
            conn.close()
