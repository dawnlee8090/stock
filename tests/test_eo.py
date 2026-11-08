import contextlib
import io
import tempfile
import unittest
from datetime import date
from pathlib import Path

from mailhub import db, eo as E
from mailhub.classify import thread_key
from mailhub.config import load_config


def mail(conn, key, subject, sent_at, tags="E.O", part_nos="", body="", is_mine=0, sender="c@oem.com"):
    return db.insert_mail(conn, key, {"subject": subject, "sent_at": sent_at, "tags": tags, "part_nos": part_nos,
                                      "body": body, "is_mine": is_mine, "sender_email": sender,
                                      "thread_key": thread_key(subject)})


class EoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.conn = db.connect(self.d / "t.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_lifecycle_and_delay(self):
        t = date(2026, 11, 2)
        m1 = mail(self.conn, "a", "[BC4t CUV] 범퍼 E.O 리스트 송부", "2026-11-02T09:00:00", part_nos="86511-BN200")
        eid = E.add(self.conn, today=t, car="BC4t CUV", part_no="86511-BN200", part_name="FR BUMPER UPR",
                    change_desc="스크류 보스 높이 2mm 상향", mold_vendor="ABC정밀", review_due="11/5", mold_due="11/20",
                    mail_id=m1)
        r = self.conn.execute("SELECT * FROM eos WHERE id=?", (eid,)).fetchone()
        self.assertEqual((r["status"], r["received_at"], r["review_due"]), ("접수", "2026-11-02", "2026-11-05"))
        self.assertEqual(E.delay_of(r, date(2026, 11, 4)), "")
        self.assertEqual(E.soon_of(r, date(2026, 11, 4)), "검토 회신 D-1")
        self.assertEqual(E.delay_of(r, date(2026, 11, 7)), "검토 회신 2일 지연")

        r = E.update(self.conn, eid, status="수정요청", note="육성 0.5mm, 수정비 협의 중", today=date(2026, 11, 6),
                     mold_action="육성")
        self.assertEqual((r["ordered_at"], r["reviewed_at"], r["mold_action"]), ("2026-11-06", "2026-11-06", "육성"))
        self.assertIn("[11/6] 육성 0.5mm", r["note"])
        self.assertEqual(E.delay_of(r, date(2026, 11, 7)), "")          # 검토 단계 지남 → 수정 기한 기준
        self.assertEqual(E.delay_of(r, date(2026, 11, 22)), "금형 수정 2일 지연")
        r = E.update(self.conn, eid, status="수정완료", today=date(2026, 11, 19), verify_due="11/25")
        r = E.update(self.conn, eid, status="완료", today=date(2026, 11, 26))
        self.assertEqual((r["mold_done_at"], r["verified_at"]), ("2026-11-19", "2026-11-26"))
        self.assertEqual(E.delay_of(r, date(2026, 12, 30)), "")
        logs = [x["to_status"] for x in self.conn.execute("SELECT * FROM eo_log WHERE eo_id=? ORDER BY id", (eid,))]
        self.assertEqual(logs, ["접수", "수정요청", "수정완료", "완료"])
        with self.assertRaises(ValueError):
            E.update(self.conn, eid, status="이상한상태")

    def test_inbox_and_sync(self):
        m1 = mail(self.conn, "a", "[BC4t] 범퍼 E.O 송부", "2026-11-02T09:00:00", part_nos="86511-BN200")
        mail(self.conn, "b", "RE: [BC4t] 범퍼 E.O 송부", "2026-11-03T09:00:00")
        m3 = mail(self.conn, "c", "콘솔 EO 변경 통보", "2026-11-03T10:00:00")
        mail(self.conn, "d", "내가 보낸 EO", "2026-11-03T11:00:00", is_mine=1)
        # 등록 전: 범퍼 건(1통으로 묶임) + 콘솔 건
        self.assertEqual({r["id"] for r in E.inbox(self.conn)}, {2, m3})
        eid = E.add(self.conn, today=date(2026, 11, 2), car="BC4t", part_no="86511-BN200", eo_no="EO26-1234",
                    received_at="2026-11-02", mail_id=m1)
        self.assertEqual([r["id"] for r in E.inbox(self.conn)], [m3])   # 같은 건(RE:)도 제외
        # 새 메일: 품번 언급(금형수정 태그), E.O 번호 언급, 같은 건 회신, 관계없는 메일
        a = mail(self.conn, "e", "86511-BN200 금형 수정 검토서", "2026-11-05T09:00:00", tags="금형수정", part_nos="86511-BN200")
        b = mail(self.conn, "f", "설계변경 회의록", "2026-11-05T10:00:00", tags="", body="EO26-1234 적용 시점 협의")
        mail(self.conn, "g", "86511-BN200 단가 문의", "2026-11-05T11:00:00", tags="", part_nos="86511-BN200")
        added = E.sync(self.conn)
        self.assertEqual(sorted(m for _, m, _ in added), sorted([2, a, b]))
        r = self.conn.execute("SELECT * FROM eos WHERE id=?", (eid,)).fetchone()
        self.assertEqual(E.new_mail_count(self.conn, r), 3)   # 등록 메일(m1)보다 나중에 온 것
        E.mark_seen(self.conn, eid)
        r = self.conn.execute("SELECT * FROM eos WHERE id=?", (eid,)).fetchone()
        self.assertEqual(E.new_mail_count(self.conn, r), 0)
        self.assertEqual(E.sync(self.conn), [])               # 두 번 실행해도 중복 없음

    def test_export_import(self):
        E.add(self.conn, today=date(2026, 11, 2), car="BC4t", part_no="86511-BN200", part_name="FR BUMPER",
              change_desc="보스 높이 변경", review_due="2026-11-03", mold_vendor="ABC정밀")
        E.add(self.conn, today=date(2026, 11, 2), car="BC4t", part_no="84710-AA000", status="완료")
        x = E.export(self.conn, self.d / "eo.xlsx")
        from openpyxl import load_workbook
        wb = load_workbook(x)
        ws = wb["E.O LIST"]
        self.assertEqual(ws.cell(1, 1).value, "E.O 관리 리스트")
        self.assertEqual(ws.cell(3, 1).value, "기본 정보")          # 그룹 머리글
        self.assertEqual(ws.cell(4, 3).value, "품번")
        self.assertEqual(ws.max_row, 6)                              # 머리글 4줄 + 2건
        from mailhub.eo_excel import COLUMNS
        alert_col = [k for k, *_ in COLUMNS].index("alert") + 1
        self.assertTrue(any("지연" in str(ws.cell(r, alert_col).value or "") for r in (5, 6)))
        self.assertEqual(ws.cell(5, 1).hyperlink.location, "'E1'!A1")
        self.assertIn("E1", wb.sheetnames)
        self.assertIn("요약", wb.sheetnames)
        c = E.export(self.conn, self.d / "eo.csv")
        conn2 = db.connect(self.d / "t2.db")
        ids = E.import_csv(conn2, c, today=date(2026, 11, 2))
        self.assertEqual(len(ids), 2)
        rows = {r["part_no"]: r for r in conn2.execute("SELECT * FROM eos")}
        self.assertEqual(rows["86511-BN200"]["mold_vendor"], "ABC정밀")
        self.assertEqual(rows["84710-AA000"]["status"], "완료")
        conn2.close()

    def test_cli(self):
        from mailhub.cli import main
        cfgp = self.d / "c.toml"
        cfgp.write_text(f'[archive]\ndb_path = "{(self.d / "m.db").as_posix()}"\n', encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            c = ["--config", str(cfgp), "eo"]
            main(c + ["add", "--car", "BC4t", "--part-no", "86511-BN200", "--desc", "보스 높이", "--review-due", "2020-01-02",
                      "--received", "2020-01-01"])
            main(c + ["set", "1", "--status", "검토중", "--note", "업체 검토 요청"])
            main(c + ["list"])
            main(c + ["show", "1"])
            main(c + ["inbox"])
            main(c + ["sync"])
            main(c + ["check"])
            main(c + ["list", "--json"])
            main(c + ["export", str(self.d / "x.xlsx")])
        t = out.getvalue()
        self.assertIn("E1", t)
        self.assertIn("지연", t)
        self.assertIn("업체 검토 요청", t)
        self.assertTrue((self.d / "x.xlsx").exists())


if __name__ == "__main__":
    unittest.main()


class EoReqTest(unittest.TestCase):
    def test_req_reply_logged_on_eo(self):
        from mailhub import followup as fu
        with tempfile.TemporaryDirectory() as d:
            conn = db.connect(Path(d) / "t.db")
            E.ensure(conn)
            eid = E.add(conn, today=date(2026, 11, 2), car="BC4t", part_no="86511-BN200")
            rid = fu.add(conn, "kim@abc-mold.co.kr", "범퍼 보스 높이 금형수정 검토 요청", "11/5", eo_id=eid,
                         today=date(2026, 11, 2))
            m = mail(conn, "r", "RE: 범퍼 보스 높이 금형수정 검토 요청", "2026-11-04T09:00:00", tags="",
                     sender="kim@abc-mold.co.kr")
            fu.check(conn, load_config(Path(d) / "x.toml"), today=date(2026, 11, 4))
            self.assertEqual(conn.execute("SELECT status FROM requests WHERE id=?", (rid,)).fetchone()[0], "회신받음")
            self.assertIsNotNone(conn.execute("SELECT 1 FROM eo_mails WHERE eo_id=? AND mail_id=?", (eid, m)).fetchone())
            log = conn.execute("SELECT note FROM eo_log WHERE eo_id=? ORDER BY id DESC", (eid,)).fetchone()[0]
            self.assertIn("업체 회신 도착", log)
            conn.close()


class PartExpandTest(unittest.TestCase):
    def test_expand(self):
        self.assertEqual(E.expand_parts("86525/26-BNGA0"), ["86525-BNGA0", "86526-BNGA0"])
        self.assertEqual(E.expand_parts("86595/96-BNQA0 MOLDING, 86531-bnqa0"),
                         ["86595-BNQA0", "86596-BNQA0", "86531-BNQA0"])
        self.assertEqual(E.expand_parts("83931/41-BN100"), ["83931-BN100", "83941-BN100"])
        self.assertEqual(E.expand_parts("RR BUMPER BACKBEAM"), [])

    def test_sync_with_lh_rh(self):
        with tempfile.TemporaryDirectory() as d:
            conn = db.connect(Path(d) / "t.db")
            eid = E.add(conn, today=date(2026, 11, 1), car="CUV", part_no="86525/26-BNGA0", received_at="2026-11-01")
            m = mail(conn, "x", "86526-BNGA0 후크 금형수정 검토서", "2026-11-05T09:00:00", tags="금형수정",
                     part_nos="86526-BNGA0")
            self.assertEqual([(e, mid) for e, mid, _ in E.sync(conn)], [(eid, m)])
            conn.close()


class EoImageTest(unittest.TestCase):
    def test_collect_and_excel(self):
        import zipfile
        from email.message import EmailMessage
        from PIL import Image, ImageDraw
        from mailhub import eo_images as I

        def png(w, h, text):
            im = Image.new("RGB", (w, h), "white")
            dr = ImageDraw.Draw(im)
            for k in range(0, w, 7):  # 압축이 덜 되게 패턴
                dr.line([(k, 0), (w - k, h)], fill=(k % 255, 80, 160))
            dr.text((20, 20), text, fill="black")
            b = io.BytesIO()
            im.save(b, "PNG")
            return b.getvalue()

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            cfg = load_config(d / "none.toml")
            cfg["archive"]["eo_image_dir"] = str(d / "img")
            conn = db.connect(d / "t.db")
            drawing, logo = png(900, 600, "BOSS +2mm"), png(120, 40, "logo")
            pptx = io.BytesIO()
            with zipfile.ZipFile(pptx, "w") as z:
                z.writestr("ppt/slides/slide1.xml", "<p:sld/>")
                z.writestr("ppt/media/image1.png", png(1000, 700, "slide pic"))
            m = EmailMessage()
            m["Subject"], m["From"], m["Date"], m["Message-ID"] = "EO 송부", "c@oem.com", "Mon, 02 Nov 2026 09:00:00 +0900", "<i@x>"
            m.set_content("도면 참고")
            m.add_related = None
            m.add_attachment(drawing, maintype="image", subtype="png", filename="image001.png")
            m.add_attachment(logo, maintype="image", subtype="png", filename="image002.png")
            m.add_attachment(pptx.getvalue(), maintype="application", subtype="octet-stream", filename="EO 설명.pptx")
            (d / "a.eml").write_bytes(bytes(m))
            mid = db.insert_mail(conn, "mid:i@x", {"subject": "EO 송부", "sent_at": "2026-11-02T09:00:00",
                                                   "file_path": str(d / "a.eml")})
            eid = E.add(conn, today=date(2026, 11, 2), car="BC4t", part_no="86511-BN200", mail_id=mid,
                        change_desc="보스 높이 2mm 상향 " * 8)
            n = I.collect(conn, cfg, eid, use_powerpoint=False, log=lambda *_: None)
            self.assertEqual(n, 2)                                   # 도면 + PPT 그림 (로고 제외)
            self.assertEqual(I.collect(conn, cfg, eid, use_powerpoint=False, log=lambda *_: None), 0)  # 중복 없음
            caps = [x["caption"] for x in I.listing(conn, eid)]
            self.assertTrue(caps[0].startswith("#%d 11/02 본문 캡처" % mid))
            self.assertIn("EO 설명.pptx", caps[1])
            # 직접 추가 + 삭제 후 다시 모아도 되살아나지 않음
            p = d / "cap.png"
            p.write_bytes(png(700, 500, "capture"))
            self.assertEqual(I.add_files(conn, cfg, eid, [str(p)], "현장 사진"), 1)
            self.assertTrue(I.remove(conn, eid, 1))
            self.assertEqual(I.collect(conn, cfg, eid, use_powerpoint=False, log=lambda *_: None), 0)
            self.assertEqual([x["sort"] for x in I.listing(conn, eid)], [2, 3])

            x = E.export(conn, d / "eo.xlsx")
            from openpyxl import load_workbook
            wb = load_workbook(x)
            ws = wb["E.O LIST"]
            self.assertEqual(len(ws._images), 1)                     # 목록 썸네일
            self.assertEqual(len(wb[f"E{eid}"]._images), 2)          # 상세 시트 그림 2장
            self.assertGreater(ws.row_dimensions[5].height, 60)      # 썸네일/긴 내용에 맞춰 행 높이
            self.assertGreater(x.stat().st_size, 5000)
            y = E.export(conn, d / "eo2.xlsx", with_images=False)
            self.assertEqual(len(load_workbook(y)["E.O LIST"]._images), 0)
            conn.close()
