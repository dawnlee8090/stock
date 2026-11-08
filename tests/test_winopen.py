import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mailhub import winopen
from mailhub.textutil import looks_html, readable_body


class OpenTest(unittest.TestCase):
    def test_eml_and_msg_use_classic_outlook(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(winopen, "find_outlook", return_value=r"C:\Office16\OUTLOOK.EXE"), \
                mock.patch.object(winopen.subprocess, "Popen") as popen:
            eml, msg = Path(d) / "a.eml", Path(d) / "b.msg"
            eml.write_text("x")
            msg.write_text("x")
            self.assertEqual(winopen.open_file(eml), "Outlook")
            self.assertEqual(popen.call_args[0][0][:2], [r"C:\Office16\OUTLOOK.EXE", "/eml"])
            opened = popen.call_args[0][0][2]                    # 영문 임시 이름 복사본
            self.assertTrue(Path(opened).name.startswith("mail_") and Path(opened).name.isascii())
            self.assertEqual(Path(opened).read_text(), "x")
            winopen.open_file(msg)
            self.assertEqual(popen.call_args[0][0][1], "/f")

    def test_html_body(self):
        h = ('<table class="tb_attachFile" width="100%"><tr><td>대용량 첨부</td><td>BC4t.7z</td></tr></table>'
             '<div>검토 부탁드립니다</div>')
        self.assertTrue(looks_html(h))
        self.assertNotIn("<table", readable_body(h))
        self.assertIn("검토 부탁드립니다", readable_body(h))
        self.assertFalse(looks_html("보스 높이 < 3mm, 갭 > 1mm"))
        self.assertEqual(readable_body("그냥 글자"), "그냥 글자")

    def test_retag_cleans_stored_html(self):
        import contextlib
        import io
        from mailhub import db
        from mailhub.cli import main
        with tempfile.TemporaryDirectory() as d:
            cfgp = Path(d) / "c.toml"
            cfgp.write_text(f'[archive]\ndb_path = "{(Path(d) / "m.db").as_posix()}"\n', encoding="utf-8")
            c = db.connect(Path(d) / "m.db")
            db.insert_mail(c, "k", {"subject": "EO", "body": '<div>검토 요청</div><br><table><tr><td>x</td></tr></table>'})
            c.commit()
            c.close()
            with contextlib.redirect_stdout(io.StringIO()) as out:
                main(["--config", str(cfgp), "retag"])
            self.assertIn("HTML 본문 1건 정리", out.getvalue())
            c = db.connect(Path(d) / "m.db")
            self.assertNotIn("<div>", c.execute("SELECT body FROM mails").fetchone()[0])
            c.close()


if __name__ == "__main__":
    unittest.main()
