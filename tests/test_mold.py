import contextlib
import io
import tempfile
import unittest
from datetime import date
from email.message import EmailMessage
from pathlib import Path

from mailhub import db, mold as M
from mailhub.classify import thread_key
from mailhub.config import load_config


def kd_xlsx() -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["BC4t CUV 금형 이관 KD LIST"])          # 제목 줄 (머리글 아님)
    ws.append([])
    ws.append(["No", "Part No", "Part Name", "Spec", "Q'ty", "Unit", "KD/LP", "Maker", "Remark"])
    ws.append([1, "86511-BN200-INS", "INSERT NUT", "M6", 4, "EA", "KD", "(예시)A사", ""])
    ws.append([2, "", "HOT RUNNER NOZZLE", "", 2, "EA", "LP", "(예시)B사", "현지 조달"])
    ws.append([3, "", "", "", "", "", "", "", ""])     # 빈 줄
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def make_mail(d: Path, conn, key, subject, attachments, sent="2026-11-02T09:00:00"):
    m = EmailMessage()
    m["Subject"], m["From"], m["Date"], m["Message-ID"] = subject, "v@mold.co.kr", "Mon, 02 Nov 2026 09:00:00 +0900", f"<{key}@x>"
    m.set_content("첨부 송부드립니다.")
    for name, data in attachments:
        m.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)
    p = d / f"{key}.eml"
    p.write_bytes(bytes(m))
    return db.insert_mail(conn, f"mid:{key}@x", {"subject": subject, "sent_at": sent, "file_path": str(p),
                                                  "tags": "금형이관", "thread_key": thread_key(subject),
                                                  "sender_email": "v@mold.co.kr"})


class MoldTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.cfg = load_config(self.d / "none.toml")
        self.cfg["archive"]["mold_dir"] = str(self.d / "molds")
        self.conn = db.connect(self.d / "t.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_header_detection(self):
        rows = M.read_table("kd.xlsx", kd_xlsx())
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0]["part_no"], rows[0]["name"], rows[0]["qty"], rows[0]["supply"]),
                         ("86511-BN200-INS", "INSERT NUT", "4", "KD"))
        self.assertEqual(rows[1]["supply"], "LP")
        csv_data = "품번,품명,재질,소요량,구분,제조사\n,PP-TD20,PP-TD20 BK,0.85kg,LP,(예시)C사\n".encode("cp949")
        r = M.read_table("원소재.csv", csv_data)
        self.assertEqual((r[0]["name"], r[0]["spec"], r[0]["supply"], r[0]["maker"]), ("PP-TD20", "PP-TD20 BK", "LP", "(예시)C사"))
        with self.assertRaises(ValueError):
            M.read_table("x.xlsx", self._no_header())

    def _no_header(self):
        from openpyxl import Workbook
        wb = Workbook()
        wb.active.append(["a", "b"])
        b = io.BytesIO()
        wb.save(b)
        return b.getvalue()

    def test_full_flow(self):
        t = date(2026, 11, 2)
        mid = M.add(self.conn, mold_no="(예시)MD-001", car="BC4t CUV", part_no="86511-BN200", part_name="FR BUMPER UPR",
                    mold_vendor="(예시)금형A", dest="멕시코", local_molder="(예시)현지성형")
        self.assertEqual([d["status"] for d in M.docs(self.conn, mid)], ["미접수"] * 6)
        # 사양서 메일 → 첨부 보관 + 자동 '접수' + 메일 날짜
        m1 = make_mail(self.d, self.conn, "spec", "MD-001 금형 사양서 송부",
                       [("금형사양서_MD-001.pdf", b"%PDF-1.4 spec"), ("image001.png", b"x" * 100)])
        d = M.set_doc(self.conn, self.cfg, mid, "금형사양서", mail_id=m1, attach="", today=t)
        files = d["files"].split("\n")
        self.assertEqual((d["status"], d["doc_date"], len(files)), ("접수", "2026-11-02", 1))
        self.assertTrue(Path(files[0]).exists() and files[0].endswith("금형사양서_MD-001.pdf"))
        M.set_doc(self.conn, self.cfg, mid, "금형사양서", status="확정", version="Rev1", today=t)
        M.set_doc(self.conn, self.cfg, mid, "성형조건표", status="요청", due="11/5", today=t)
        # KD 리스트: 메일 첨부 엑셀에서 불러오기 → 항목 + 섹션 접수 + 파일 보관
        m2 = make_mail(self.d, self.conn, "kd", "RE: MD-001 금형 사양서 송부", [("KD LIST_MD-001.xlsx", kd_xlsx())])
        n, name = M.import_items(self.conn, self.cfg, mid, "KD", mail_id=m2, attach="KD")
        self.assertEqual((n, name), (2, "KD LIST_MD-001.xlsx"))
        kd = [x for x in M.docs(self.conn, mid) if x["section"] == "KD리스트"][0]
        self.assertEqual(kd["status"], "접수")
        self.assertTrue(kd["files"].endswith("KD LIST_MD-001.xlsx"))
        n2, _ = M.import_items(self.conn, self.cfg, mid, "KD", mail_id=m2, attach="KD")   # 다시 → 교체 (중복 없음)
        self.assertEqual(len(M.items(self.conn, mid, "KD")), 2)
        # 기타 섹션 자유 추가
        M.set_doc(self.conn, self.cfg, mid, "검사구·지그", status="해당없음", today=t)
        self.assertEqual(M.progress(self.conn, mid), (2, 7))
        # 선적: ETD 10일 남았는데 서류 미완료 → 알림 / 성형조건표 기한 지남
        M.set_ship(self.conn, mid, etd="11/12", eta="12/10", bl_no="BL123", today=t)
        m = M.get(self.conn, mid)
        al = M.alerts(self.conn, m, date(2026, 11, 6))
        self.assertTrue(any(a.startswith("선적 D-6, 미완료:") and "KD리스트" in a for a in al))
        self.assertIn("성형조건표 기한 11-05 지남", al)
        sd = [x for x in M.docs(self.conn, mid) if x["section"] == "선적일정"][0]
        self.assertEqual(sd["status"], "접수")
        # 출항 → 상태 '선적', 선적일정 확정 / 도착 → '현지도착'
        M.set_ship(self.conn, mid, atd="11/12", today=t)
        self.assertEqual(M.get(self.conn, mid)["status"], "선적")
        self.assertEqual([x for x in M.docs(self.conn, mid) if x["section"] == "선적일정"][0]["status"], "확정")
        self.assertIn("ETA 12-10 지남 - 현지 도착 확인 필요", M.alerts(self.conn, M.get(self.conn, mid), date(2026, 12, 12)))
        M.set_ship(self.conn, mid, ata="12/11", today=t)
        self.assertEqual(M.get(self.conn, mid)["status"], "현지도착")
        # 메일: 같은 건 회신 자동 연결, 미등록 메일
        m3 = make_mail(self.d, self.conn, "c", "RE: RE: MD-001 금형 사양서 송부", [], "2026-11-20T09:00:00")
        m4 = make_mail(self.d, self.conn, "e", "콘솔 금형 이관 일정 문의", [], "2026-11-20T10:00:00")
        self.assertIn((mid, m3), [(a, b) for a, b, _ in M.sync(self.conn)])
        self.assertEqual([r["id"] for r in M.inbox(self.conn)], [m4])
        # 엑셀
        from mailhub import mold_excel
        x = mold_excel.build(self.conn, M, M.listing(self.conn, "all"), self.d / "mt.xlsx", today=date(2026, 11, 6))
        from openpyxl import load_workbook
        wb = load_workbook(x)
        self.assertEqual(wb.sheetnames, ["이관 종합", "선적 일정", "부품 리스트", "KD 리스트", "원소재 리스트", f"M{mid}"])
        ws = wb["이관 종합"]
        self.assertEqual(ws.cell(4, 8).value, "금형사양서")
        self.assertEqual(ws.cell(5, 8).value, "● 11/02")
        self.assertTrue(str(ws.cell(5, 8).hyperlink.target).endswith(".pdf"))
        self.assertEqual(wb["KD 리스트"].cell(5, 4).value, "86511-BN200-INS")
        self.assertEqual(wb["KD 리스트"].cell(6, 9).value, "LP")

    def test_cli(self):
        from mailhub.cli import main
        cfgp = self.d / "c.toml"
        cfgp.write_text(f'[archive]\ndb_path = "{(self.d / "m.db").as_posix()}"\nmold_dir = "{(self.d / "md").as_posix()}"\n',
                        encoding="utf-8")
        kd = self.d / "kd.xlsx"
        kd.write_bytes(kd_xlsx())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            c = ["--config", str(cfgp), "mold"]
            main(c + ["add", "--mold-no", "MD-9", "--part-name", "CONSOLE", "--dest", "인도"])
            main(c + ["doc", "1", "금형사양서", "--file", str(kd), "--status", "확정"])
            main(c + ["items", "1", "--type", "KD", "--file", str(kd)])
            main(c + ["ship", "1", "--etd", "2020-01-10", "--eta", "2020-02-10"])
            main(c + ["list"])
            main(c + ["show", "1"])
            main(c + ["inbox"])
            main(c + ["check"])
            main(c + ["export", str(self.d / "x.xlsx")])
        t = out.getvalue()
        self.assertIn("서류 1/6", t)            # 사양서 확정만 완료 (KD 는 '접수' 상태)
        self.assertIn("KD 리스트 2건", t)
        self.assertIn("ETD 01-10 지남", t)
        self.assertTrue((self.d / "x.xlsx").exists())


if __name__ == "__main__":
    unittest.main()
