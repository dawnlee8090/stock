import io
import tempfile
import unittest
import zipfile
from email.message import EmailMessage
from pathlib import Path

from mailhub import attach, db
from mailhub.config import load_config
from mailhub.ingest_files import ingest_folder
from mailhub.search import search, snippet


def make_zip(files: dict) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return b.getvalue()


XLSX = make_zip({
    "xl/sharedStrings.xml": '<sst><si><t>EO 번호</t></si><si><t>84710-AA000 CRASH PAD 보스 높이 변경</t></si></sst>',
    "xl/worksheets/sheet1.xml": '<worksheet><c t="inlineStr"><is><t>금형 수정: 육성 0.5mm</t></is></c>'
                                '<c><v>12345</v></c><c><v>ABC-123</v></c></worksheet>'})
DOCX = make_zip({"word/document.xml": '<w:document><w:p><w:r><w:t>T/O 결과 보고</w:t></w:r></w:p>'
                                      '<w:p><w:r><w:t>웰드라인 발생</w:t></w:r></w:p></w:document>'})
PPTX = make_zip({"ppt/slides/slide1.xml": '<p:sld><a:t>싱크마크 대책</a:t><a:t>게이트 위치 변경</a:t></p:sld>',
                 "ppt/slides/slide2.xml": '<p:sld><a:t>검사구 C/F 수정</a:t></p:sld>'})


def make_pdf(text: str) -> bytes:
    # 최소 PDF (영문)
    stream = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out = b"%PDF-1.4\n"
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    x = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, x)
    return out


class AttachTest(unittest.TestCase):
    def test_extract(self):
        x = attach.extract_text("EO LIST.xlsx", XLSX)
        self.assertIn("84710-AA000", x)
        self.assertIn("육성", x)
        self.assertIn("ABC-123", x)
        self.assertNotIn("12345", x)
        self.assertIn("웰드라인", attach.extract_text("보고서.docx", DOCX))
        p = attach.extract_text("대책.pptx", PPTX)
        self.assertLess(p.index("싱크마크"), p.index("검사구"))
        try:
            import pypdf  # noqa: F401
            self.assertIn("GATE", attach.extract_text("a.pdf", make_pdf("GATE MOVE 5MM")))
        except ImportError:
            pass
        self.assertEqual(attach.extract_text("도면.dwg", b"xx"), "")
        self.assertEqual(attach.extract_text("깨짐.xlsx", b"not a zip"), "")

    def test_ingest_and_search(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            m = EmailMessage()
            m["Subject"], m["From"], m["To"] = "EO 리스트 송부", "c@oem.com", "me@corp.com"
            m["Date"], m["Message-ID"] = "Mon, 02 Nov 2026 09:00:00 +0900", "<att@x>"
            m.set_content("첨부 확인 바랍니다.")
            m.add_attachment(XLSX, maintype="application", subtype="octet-stream", filename="EO LIST.xlsx")
            m.add_attachment(PPTX, maintype="application", subtype="octet-stream", filename="대책.pptx")
            (d / "a.eml").write_bytes(bytes(m))
            cfg = load_config(d / "none.toml")
            conn = db.connect(d / "t.db")
            ingest_folder(conn, cfg, d, log=lambda *_: None)
            rows = search(conn, "보스 높이")
            self.assertEqual(len(rows), 1)
            self.assertTrue(snippet(rows[0]["body"], "보스 높이", att_text=rows[0]["att_text"]).startswith("[첨부내용]"))
            self.assertEqual(len(search(conn, "게이트")), 1)      # 3글자 → FTS
            self.assertEqual(len(search(conn, "육성")), 1)        # 2글자 → LIKE
            conn.close()

    def test_old_db_upgrade_rebuilds_index(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "old.db"
            c = sqlite3.connect(p)
            c.executescript("""
CREATE TABLE mails (id INTEGER PRIMARY KEY, msg_key TEXT UNIQUE NOT NULL, source TEXT, folder TEXT,
 subject TEXT DEFAULT '', sender_name TEXT DEFAULT '', sender_email TEXT DEFAULT '', to_addr TEXT DEFAULT '',
 cc_addr TEXT DEFAULT '', sent_at TEXT DEFAULT '', body TEXT DEFAULT '', attachments TEXT DEFAULT '',
 entry_id TEXT DEFAULT '', store_id TEXT DEFAULT '', file_path TEXT DEFAULT '', tags TEXT DEFAULT '',
 part_nos TEXT DEFAULT '', needs_action INTEGER DEFAULT 0, status TEXT DEFAULT '', note TEXT DEFAULT '',
 ingested_at TEXT DEFAULT '');
CREATE VIRTUAL TABLE mails_fts USING fts5(subject, sender_name, sender_email, to_addr, cc_addr, body,
 attachments, tags, part_nos, content='mails', content_rowid='id', tokenize='trigram');
CREATE TRIGGER mails_ai AFTER INSERT ON mails BEGIN
  INSERT INTO mails_fts(rowid, subject, sender_name, sender_email, to_addr, cc_addr, body, attachments, tags, part_nos)
  VALUES (new.id, new.subject, new.sender_name, new.sender_email, new.to_addr, new.cc_addr, new.body, new.attachments, new.tags, new.part_nos);
END;
INSERT INTO mails(msg_key, subject, body, sent_at) VALUES ('k1', '크래쉬패드 E.O', '금형 수정 요청', '2026-11-01');
""")
            c.commit()
            c.close()
            conn = db.connect(p)
            self.assertEqual(len(search(conn, "크래쉬패드")), 1)
            conn.execute("UPDATE mails SET att_text='첨부 안의 웰드라인' WHERE id=1")
            self.assertEqual(len(search(conn, "웰드라인")), 1)
            conn.close()


if __name__ == "__main__":
    unittest.main()
