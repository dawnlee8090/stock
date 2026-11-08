import contextlib
import io
import tempfile
import unittest
from datetime import date
from pathlib import Path

from mailhub import db, to as T
from mailhub.classify import thread_key
from mailhub.to_knowledge import DEFECTS, GUIDE, guide_text, normalize_defect


def mail(conn, key, subject, sent_at, tags="T/O", part_nos=""):
    return db.insert_mail(conn, key, {"subject": subject, "sent_at": sent_at, "tags": tags, "part_nos": part_nos,
                                      "thread_key": thread_key(subject), "sender_email": "v@mold.co.kr"})


class KnowledgeTest(unittest.TestCase):
    def test_normalize_and_guide(self):
        self.assertEqual(normalize_defect("SINK MARK 발생"), "싱크")
        self.assertEqual(normalize_defect("커버 변형"), "변형")
        self.assertEqual(normalize_defect("조립 후 크랙"), "크랙")
        self.assertEqual(normalize_defect("헤드라이닝 갭 과다"), "단차·갭")
        self.assertEqual(set(DEFECTS), set(GUIDE))
        self.assertIn("보압", guide_text("싱크"))
        self.assertIn("가이드가 없습니다", guide_text("알수없음"))


class ToTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.conn = db.connect(self.d / "t.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_recurrence_and_similar(self):
        t1, prev = T.add(self.conn, today=date(2026, 3, 2), car="BC4t CUV", part_no="86525/26-BNGA0",
                         part_name="BRKT-FR BPR SIDE UPR", to_stage="T1", resin="PP-TD20", defect_type="sink",
                         location="보스 주변", phenomenon="보스 뒷면 싱크 0.15mm", cause_cat="제품설계",
                         cause="보스 두께 과다", action_cat="금형수정", countermeasure="보스 살빼기 육성")
        self.assertIsNone(prev)
        T.update(self.conn, t1, status="종결", result="T2 싱크 0.03mm 이하", effective="유효", today=date(2026, 4, 1))
        t2, _ = T.add(self.conn, today=date(2026, 5, 1), car="BC4t 5DR", part_no="86511-BN200",
                      part_name="BRKT-FR BPR SIDE LWR", resin="PP-TD20", defect_type="웰드", phenomenon="홀 주변 웰드")
        # 같은 품번(RH 쪽) 같은 불량 → 재발
        t3, prev = T.add(self.conn, today=date(2026, 11, 2), car="BC4t CUV", part_no="86526-BNGA0",
                         part_name="BRKT-FR BPR SIDE UPR", to_stage="M", defect_type="싱크마크",
                         phenomenon="보스 싱크 재발")
        self.assertEqual(prev["id"], t1)
        r3 = self.conn.execute("SELECT * FROM to_issues WHERE id=?", (t3,)).fetchone()
        self.assertEqual((r3["recur_of"], r3["defect_type"]), (t1, "싱크"))
        # 유사 사례: 같은 불량·같은 품번·효과 확인 대책이 1위
        res = T.similar(self.conn, defect="싱크", part_name="BRKT FR BPR SIDE", part_no="86525-BNGA0",
                        resin="PP-TD20", exclude_id=t3)
        self.assertEqual(res[0][2]["id"], t1)
        self.assertIn("효과 확인된 대책", res[0][1])
        self.assertIn("같은 품번", res[0][1])
        # 다른 불량은 점수가 낮음
        self.assertTrue(all(s for s, _, r in res if r["id"] == t2))
        self.assertGreater(res[0][0], [s for s, _, r in res if r["id"] == t2][0])

    def test_stages_delay_sync(self):
        m1 = mail(self.conn, "a", "[CUV] FR BUMPER T1 결과 공유", "2026-11-02T09:00:00", part_nos="86511-BN200")
        tid, _ = T.add(self.conn, today=date(2026, 11, 2), part_no="86511-BN200", part_name="FR BUMPER UPR",
                       defect_type="변형", phenomenon="상단 플랜지 휨 1.2mm", due="11/10", verify_due="11/20", mail_id=m1)
        r = self.conn.execute("SELECT * FROM to_issues WHERE id=?", (tid,)).fetchone()
        self.assertEqual(T.soon_of(r, date(2026, 11, 9)), "대책 적용 D-1")
        self.assertEqual(T.delay_of(r, date(2026, 11, 12)), "대책 적용 2일 지연")
        r = T.update(self.conn, tid, status="대책적용", today=date(2026, 11, 11), countermeasure="냉각 보강 + 역변형 가공")
        self.assertEqual(r["applied_at"], "2026-11-11")
        self.assertEqual(T.delay_of(r, date(2026, 11, 12)), "")
        self.assertEqual(T.delay_of(r, date(2026, 11, 22)), "효과 확인 2일 지연")
        r = T.update(self.conn, tid, status="종결", today=date(2026, 11, 21), result="T2 휨 0.3mm OK", effective="유효")
        self.assertEqual(r["closed_at"], "2026-11-21")
        # 관련 메일 자동 연결 + 미등록 메일
        m2 = mail(self.conn, "b", "RE: [CUV] FR BUMPER T1 결과 공유", "2026-11-05T09:00:00")
        m3 = mail(self.conn, "c", "콘솔 T1 결과", "2026-11-05T10:00:00")
        self.assertEqual([r["id"] for r in T.inbox(self.conn)], [m3])
        tid2, _ = T.add(self.conn, today=date(2026, 11, 2), part_no="86511-BN200", defect_type="버",
                        phenomenon="파팅 버", mail_id=m1)
        self.assertIn((tid2, m2), [(a, b) for a, b, _ in T.sync(self.conn)])

    def test_excel_and_csv(self):
        T.add(self.conn, today=date(2026, 11, 2), car="CUV", part_no="86511-BN200", part_name="FR BUMPER",
              defect_type="싱크", phenomenon="보스 싱크 " * 10, cause_cat="제품설계", cause="보스 두께",
              countermeasure="살빼기", due="2026-11-03")
        T.add(self.conn, today=date(2026, 11, 2), car="CUV", part_no="86511-BN200", defect_type="싱크",
              phenomenon="재발", status="발생")
        from mailhub import eo_images as I, to_excel
        I.ensure(self.conn)
        x = to_excel.build(self.conn, T.listing(self.conn, "all"), self.d / "to.xlsx", T.delay_of, T.soon_of,
                           lambda i: I.listing(self.conn, i, kind="to"))
        from openpyxl import load_workbook
        wb = load_workbook(x)
        self.assertEqual(wb.sheetnames[:2], ["TO 문제점", "불량 통계"])
        self.assertIn("T2", wb.sheetnames)
        ws = wb["TO 문제점"]
        self.assertEqual(ws.cell(4, 5).value, "불량 유형")
        self.assertIn("재발", str(ws.cell(6, 19).value))
        self.assertGreater(ws.row_dimensions[5].height, 30)
        c = T.export_csv(self.conn, self.d / "to.csv")
        conn2 = db.connect(self.d / "t2.db")
        self.assertEqual(len(T.import_csv(conn2, c)), 2)
        conn2.close()

    def test_cli(self):
        from mailhub.cli import main
        cfgp = self.d / "c.toml"
        cfgp.write_text(f'[archive]\ndb_path = "{(self.d / "m.db").as_posix()}"\n'
                        f'eo_image_dir = "{(self.d / "img").as_posix()}"\n', encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            c = ["--config", str(cfgp), "to"]
            main(c + ["add", "--part-no", "86511-BN200", "--part-name", "FR BUMPER", "--defect", "싱크", "--phenomenon",
                      "보스 싱크", "--cm", "보압 증가", "--due", "2020-01-05", "--date", "2020-01-01"])
            main(c + ["set", "1", "--status", "종결", "--effective", "유효", "--result", "OK"])
            main(c + ["add", "--part-no", "86511-BN200", "--defect", "sink", "--phenomenon", "재발"])
            main(c + ["list", "--status", "all"])
            main(c + ["show", "2"])
            main(c + ["similar", "--defect", "싱크", "--part-name", "BUMPER"])
            main(c + ["guide", "변형"])
            main(c + ["inbox"])
            main(c + ["check"])
            main(c + ["img", "1"])
            main(c + ["export", str(self.d / "x.xlsx")])
        t = out.getvalue()
        self.assertIn("재발입니다", t)
        self.assertIn("효과 확인된 대책", t)
        self.assertIn("변형 - 원인·대책 가이드", t)
        self.assertTrue((self.d / "x.xlsx").exists())


if __name__ == "__main__":
    unittest.main()
