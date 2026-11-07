import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

from mailhub import db
from mailhub.config import load_config
from mailhub.ingest_imap import ingest_imap, parse_list, utf7_decode


def raw_mail(subject, sender, to, date, mid, body="검토 요청드립니다."):
    m = EmailMessage()
    m["Subject"], m["From"], m["To"], m["Date"], m["Message-ID"] = subject, sender, to, date, mid
    m.set_content(body)
    return bytes(m)


class FakeIMAP:
    """읽기 전용 열기(EXAMINE)와 BODY.PEEK 사용 여부를 기록하는 가짜 메일 서버."""

    def __init__(self, boxes):
        self.boxes = boxes  # raw_name -> {uid: bytes}
        self.commands = []
        self.current = None

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"',
                      b'(\\HasNoChildren \\Sent) "/" "&vPSwuNO4ycDVaA-"',   # 보낸편지함
                      b'(\\HasNoChildren \\Trash) "/" "&1zTJwNG1-"']          # 휴지통

    def select(self, mailbox, readonly=False):
        self.commands.append(("SELECT", mailbox, readonly))
        self.current = mailbox.strip('"')
        return "OK", [b"1"]

    def response(self, code):
        return code, [b"777"]

    def uid(self, cmd, *args):
        self.commands.append(("UID", cmd, args))
        box = self.boxes.get(self.current, {})
        if cmd == "SEARCH":
            crit = args[-1]
            uids = sorted(box)
            if crit.startswith("UID "):
                lo = int(crit.split()[1].split(":")[0])
                uids = [u for u in uids if u >= lo] or ([max(box)] if box else [])  # 실제 서버처럼 마지막 1건은 항상 반환
            return "OK", [" ".join(map(str, uids)).encode()]
        if cmd == "FETCH":
            assert "PEEK" in args[1], "BODY.PEEK 를 써야 읽음 표시가 안 바뀜"
            out = []
            for u in map(int, args[0].split(",")):
                out += [(f"{u} (UID {u} BODY[] {{{len(box[u])}}}".encode(), box[u]), b")"]
            return "OK", out
        raise AssertionError(cmd)

    def logout(self):
        pass


class ImapTest(unittest.TestCase):
    def test_utf7_and_list(self):
        self.assertEqual(utf7_decode("&vPSwuNO4ycDVaA-"), "보낸편지함")
        fs = parse_list(FakeIMAP({}).list()[1])
        self.assertEqual([(f["name"], f["sent"], f["skip"]) for f in fs],
                         [("INBOX", False, False), ("보낸편지함", True, False), ("휴지통", False, True)])

    def test_ingest_readonly_incremental(self):
        boxes = {
            "INBOX": {1: raw_mail("A건 검토 요청", "c@oem.com", "나 <me@corp.com>", "Mon, 02 Nov 2026 09:00:00 +0900", "<a@x>"),
                      2: raw_mail("B건 검토 요청", "c@oem.com", "나 <me@corp.com>", "Mon, 02 Nov 2026 10:00:00 +0900", "<b@x>")},
            "&vPSwuNO4ycDVaA-": {5: raw_mail("RE: A건 검토 요청", "me@corp.com", "c@oem.com", "Tue, 03 Nov 2026 09:00:00 +0900", "<r@x>", "회신드립니다")},
        }
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(Path(d) / "none.toml")
            cfg["imap"].update(host="h", user="u")
            cfg["me"] = {"addresses": ["me@corp.com"], "names": []}
            cfg["archive"]["eml_dir"] = str(Path(d) / "eml")
            conn = db.connect(Path(d) / "t.db")
            fake = FakeIMAP(boxes)
            st = ingest_imap(conn, cfg, save_eml=True, _imap=fake, log=lambda *_: None)
            self.assertEqual((st["folders"], st["added"]), (2, 3))
            self.assertTrue(all(c[2] is True for c in fake.commands if c[0] == "SELECT"))  # 읽기 전용
            sent = conn.execute("SELECT is_mine, folder, file_path FROM mails WHERE subject LIKE 'RE:%'").fetchone()
            self.assertEqual(sent[0], 1)
            self.assertTrue(Path(sent[2]).exists())
            from mailhub.search import todo
            self.assertEqual([r["subject"] for r in todo(conn, "2026-01-01")], ["B건 검토 요청"])

            # 두 번째 실행: 새 메일 1건만
            boxes["INBOX"][3] = raw_mail("C건 확인 요청", "c@oem.com", "me@corp.com", "Wed, 04 Nov 2026 09:00:00 +0900", "<c@x>")
            fake2 = FakeIMAP(boxes)
            st2 = ingest_imap(conn, cfg, _imap=fake2, log=lambda *_: None)
            self.assertEqual((st2["fetched"], st2["added"]), (1, 1))
            conn.close()


if __name__ == "__main__":
    unittest.main()
