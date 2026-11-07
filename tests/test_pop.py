import tempfile
import unittest
from pathlib import Path

from mailhub import db
from mailhub.config import load_config
from mailhub.ingest_pop import ingest_pop
from tests_helpers import raw_mail


class FakePOP:
    def __init__(self, msgs):
        self.msgs = msgs  # [(uidl, bytes)]
        self.cmds = []

    def uidl(self):
        return b"+OK", [f"{i + 1} {u}".encode() for i, (u, _) in enumerate(self.msgs)], 0

    def top(self, n, lines):
        self.cmds.append(("TOP", n, lines))
        raw = self.msgs[n - 1][1]
        ls = raw.split(b"\n")
        if lines == 0:
            ls = raw.split(b"\n\n")[0].split(b"\n")
        return b"+OK", ls, len(raw)

    def retr(self, n):
        raise AssertionError("RETR 를 쓰면 안 됨 (읽음 처리 위험)")

    def dele(self, n):
        raise AssertionError("DELE 금지")

    def quit(self):
        self.cmds.append(("QUIT",))


class PopTest(unittest.TestCase):
    def test_pop_peek_incremental(self):
        msgs = [("u1", raw_mail("옛날 메일", "c@oem.com", "me@corp.com", "Mon, 01 Sep 2026 09:00:00 +0900", "<old@x>")),
                ("u2", raw_mail("A건 검토 요청", "c@oem.com", "me@corp.com", "Mon, 02 Nov 2026 09:00:00 +0900", "<a@x>")),
                ("u3", raw_mail("B건 확인 요청", "c@oem.com", "me@corp.com", "Tue, 03 Nov 2026 09:00:00 +0900", "<b@x>"))]
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(Path(d) / "none.toml")
            cfg["pop"] = {"host": "h", "user": "u"}
            conn = db.connect(Path(d) / "t.db")
            # B건은 이미 PST 로 받은 상태
            db.insert_mail(conn, "mid:b@x", {"subject": "B건 확인 요청"})
            fake = FakePOP(msgs)
            st = ingest_pop(conn, cfg, since="2026-10-01", _pop=fake, log=lambda *_: None)
            self.assertEqual((st["added"], st["skipped"], st["old"], st["fetched"]), (1, 1, 1, 1))
            st2 = ingest_pop(conn, cfg, _pop=FakePOP(msgs), log=lambda *_: None)
            self.assertEqual(st2["checked"], 0)
            conn.close()


if __name__ == "__main__":
    unittest.main()
