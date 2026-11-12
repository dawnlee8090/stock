import base64
import io
import json
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path

from localmold import core as L
from localmold.config import load_config
from localmold.webapp import Data, make_handler

TODAY = date(2026, 11, 12)

V4_ROWS = [
    ["외주처", "품번", "품명", "금형 제작 업체", "카테고리", "발생 문제점(Problem)", "수리/개선 계획(Improvement)", "발생일정",
     "수정 진행 일정", "작업자 이름", "중요도", "\n수정 후 \nT/O 현황", "T/O 결과", "현재 상태(STATUS)", "금형수정대상"],
    ["ASHA-9", "86511-BN000", "COVER ASSY-FR BUMPER UPR", "A금형", "습합", "측면 BURR", "재습합", "07월 03일", "8/30",
     "금형 수정 완료 (T/O 요청필요)", "상", "", "", "개선 중", "O"],
    ["ASHA-9", "86511-BN000", "COVER ASSY-FR BUMPER UPR", "A금형", "습합", "측면 BURR 재발", "파팅 보강", "08월 14일", "8/30",
     "", "중", "O", "NG", "개선 중", "-"],
    ["ASHA-9", "847D2-BN200", "GARNISH-C/PAD CTR", "B금형", "기타", "금형 내 스크래치", "", "2026. 7. 18", "", "", "하", "", "",
     "관망", "-"],
    ["ASHA-9", "84611-BN000", "CONSOLE-FLOOR", "B금형", "SINK", "리브 싱크", "두께 증대", "7/29", "", "", "중", "O", "OK", "개선 완", "O"],
    ["ASHA-9", "84611-BN000", "CONSOLE-FLOOR", "B금형", "기타", "캘린더 시인성", "캘린더 재적용", "7/29", "", "", "하", "", "", "개선 완", "-"],
    ["ASHA-9", "84611-BN000", "CONSOLE-FLOOR", "B금형", "기타", "-", "", "", "", "", "", "", "", "-", "-"],
]
MASTER_ROWS = [
    ["MOLD ISSUE LIST — ASAH-9", "", "", ""],
    ["NO", "Part No\n(엑셀탭 생성)", "PART NO\n(하이퍼링크)", "PART_NAME", "ITEM", "MOLD TYPE (FAREL NUMBER)", "KR 업체", "습합",
     "TOTAL", "Responsible", "LAST Date", "STATUS", "수정대상\n문제점 수", "T/O\n현황", ""],
    ["1", "", '=HYPERLINK("#")', "", "", "", "", "", "", "", "", "", "", "", ""],
    ["2", "86511-BN000", "86511-BN000", "COVER ASSY-FR BUMPER UPR", "BUMPER", "INJECTION", "A금형", "2", "2", "", "", "", "", "2nd", "2475g"],
    ["3", "86599-BN000", "86599-BN000", "BRKT-FR BUMPER", "BUMPER", "INJECTION", "A금형", "0", "0", "", "-", "-", "0", "1st", ""],
    ["15", "", "", "", "", "", "", "", "", "", "-", "", "", "", ""],
]


def png(color=(200, 60, 60), size=(640, 480)) -> bytes:
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "PNG")
    return b.getvalue()


def mkcfg(d: Path):
    cfg = load_config(d / "none.toml")
    cfg.update(db_path=str(d / "data" / "lm.db"), photo_dir=str(d / "data" / "photos"), export_dir=str(d / "data" / "export"))
    return cfg


class ValueTest(unittest.TestCase):
    def test_parse_date(self):
        self.assertEqual(L.parse_date("07월 03일", TODAY), "2026-07-03")
        self.assertEqual(L.parse_date("7/29", TODAY), "2026-07-29")
        self.assertEqual(L.parse_date("2026. 7. 18", TODAY), "2026-07-18")
        self.assertEqual(L.parse_date("2026-07-08 00:00:00", TODAY), "2026-07-08")
        self.assertEqual(L.parse_date("7/14 오전", TODAY), "2026-07-14")
        self.assertEqual(L.parse_date("12/20", date(2027, 1, 5)), "2026-12-20")   # 한 달 넘게 뒤면 작년
        self.assertEqual(L.parse_date("-", TODAY), "")
        self.assertEqual(L.parse_date("↑", TODAY), "")

    def test_map_status(self):
        self.assertEqual(L.map_status("검토 중"), "원인분석")
        self.assertEqual(L.map_status("개선 중", plan="재습합"), "대책수립")
        self.assertEqual(L.map_status("개선 중", progress="금형 수정 완료 (T/O 요청필요)"), "대책적용")
        self.assertEqual(L.map_status("개선 완"), "효과확인")             # T/O OK 가 없으면 검증 필요
        self.assertEqual(L.map_status("개선 완", to_result="OK"), "종결")
        self.assertEqual(L.map_status("개선 완", to_result="NG"), "대책수립")
        self.assertEqual(L.map_status("AQL 협의"), "효과확인")
        self.assertEqual(L.map_status("관망"), "보류")
        self.assertEqual(L.map_status("-"), "발생")
        self.assertEqual(L.map_status("대책적용"), "대책적용")

    def test_mold_status_and_category(self):
        self.assertEqual(L.mold_status(["종결", "대책적용", "원인분석"]), "원인분석")
        self.assertEqual(L.mold_status(["종결", "보류"]), "보류")
        self.assertEqual(L.mold_status([]), "-")
        self.assertEqual(L.norm_category("WELD LINE"), "WELD LINE")
        self.assertEqual(L.norm_category("weld"), "WELD LINE")
        self.assertEqual(L.norm_category("", "리브 싱크 발생"), "SINK")
        self.assertEqual(L.norm_category("", "알 수 없음"), "기타")
        self.assertEqual(L.fmt_part_no("847d2bn200"), "847D2-BN200")
        self.assertTrue(L.is_part_no("83370-BNAA0-INJ"))
        self.assertFalse(L.is_part_no('=HYPERLINK("#")'))
        self.assertEqual(L.vendor_from_name("BC4t CUV_CAR_NAPLAS_REV.1 (매크로).xlsx"), "NAPLAS")


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.cfg = mkcfg(self.d)
        self.c = L.connect(self.cfg["db_path"])
        L.ensure(self.c)

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def test_issue_list_import_is_idempotent_and_keeps_app_edits(self):
        st = L.import_issues(self.c, "CUV", V4_ROWS, today=TODAY)
        self.assertEqual((st["new"], st["skipped"]), (5, 1))
        rows = {r["problem"]: r for r in L.issues(self.c, "CUV")}
        self.assertEqual(rows["측면 BURR"]["status"], "대책적용")
        self.assertEqual(rows["측면 BURR 재발"]["status"], "대책수립")          # 수정 후 T/O NG
        self.assertEqual(rows["측면 BURR 재발"]["recur_of"], rows["측면 BURR"]["id"])
        self.assertEqual(rows["리브 싱크"]["status"], "종결")
        self.assertEqual(rows["캘린더 시인성"]["status"], "효과확인")
        self.assertEqual(rows["금형 내 스크래치"]["status"], "보류")
        self.assertEqual(rows["금형 내 스크래치"]["part_no"], "847D2-BN200")
        self.assertEqual(rows["측면 BURR"]["due"], "2026-08-30")
        self.assertEqual(rows["측면 BURR"]["src_status"], "개선 중")
        # 다시 가져오면 변경 없음, 앱에서 고친 건은 유지
        L.update_issue(self.c, rows["캘린더 시인성"]["id"], status="종결", to_result="OK", today=TODAY)
        st = L.import_issues(self.c, "CUV", V4_ROWS, today=TODAY)
        self.assertEqual((st["new"], st["same"], st["kept"]), (0, 4, 1))
        self.assertEqual(L.get_issue(self.c, rows["캘린더 시인성"]["id"])["status"], "종결")
        car = L.cars(self.c)[0]
        self.assertEqual((car["molds"], car["issues"], car["closed"]), (3, 5, 2))

    def test_master_import_matches_vendor_typo_and_reads_weight(self):
        L.import_issues(self.c, "CUV", V4_ROWS, today=TODAY)
        st = L.import_molds(self.c, "CUV", MASTER_ROWS)
        self.assertEqual((st["new"], st["updated"]), (1, 1))
        ms = {m["part_no"]: m for m in L.molds(self.c, "CUV", "ASHA-9")}
        self.assertEqual(set(ms), {"86511-BN000", "86599-BN000", "847D2-BN200", "84611-BN000"})
        m = ms["86511-BN000"]
        self.assertEqual((m["item"], m["mold_maker"], m["to_round"], m["weight"]), ("BUMPER", "A금형", "2nd", "2475g"))
        self.assertEqual((m["total"], m["counts"]["습합"], m["status"]), (2, 2, "대책수립"))
        self.assertEqual(ms["86599-BN000"]["status"], "-")
        self.assertRaises(ValueError, L.import_molds, self.c, "CUV", MASTER_ROWS[1:])   # 외주처를 알 수 없음

    def _workbook(self) -> bytes:
        import openpyxl
        from openpyxl.drawing.image import Image as XImage
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "MASTER"
        for r in MASTER_ROWS:
            ws.append(r)
        sh = wb.create_sheet("86511-BN000")
        sh.append(["86511-BN000  |  COVER ASSY-FR BUMPER UPR  |  BUMPER  |  INJECTION  |  A금형"])
        sh.append(["🔼 갑지로 돌아가기"])
        sh.append(["NO", "PROBLEM", "CATEGORY", "PART VISUALS (Before)", "MOLD IMPROVEMENT", "DATE", "PART VISUALS (After)",
                   "Remark", "RESULT", "금형수정 대상"])
        sh.append([1, "측면 BURR", "습합", "", "재습합", "2026-07-03", "", "", "개선 중", "O"])
        sh.append([2, "새 문제 (을지에만)", "취출", "", "랩핑", "2026-09-01", "", "", "검토 중", ""])
        for col, color in (("D4", (200, 40, 40)), ("G4", (40, 160, 60)), ("D5", (40, 40, 200))):
            img = XImage(io.BytesIO(png(color)))
            sh.add_image(img, col)
        b = io.BytesIO()
        wb.save(b)
        return b.getvalue()

    def test_vendor_workbook_with_photos(self):
        L.import_issues(self.c, "CUV", V4_ROWS, today=TODAY)
        data = self._workbook()
        st = L.import_any(self.c, self.cfg, "CUV", "CUV_CAR_ASHA-9_REV.1.xlsx", data, "ASHA-9", today=TODAY)
        self.assertEqual(st["kind"], "외주처 엑셀")
        self.assertEqual((st["sheets"], st["new"], st["photos"]), (1, 1, 3))
        a = next(r for r in L.issues(self.c, "CUV") if r["problem"] == "측면 BURR")
        self.assertEqual(a["status"], "대책적용")    # 을지에 진행 열이 없어도 단계가 거꾸로 가지 않음
        self.assertEqual(sorted(p["grp"] for p in L.photos(self.c, a["id"])), ["after", "before"])
        b = next(r for r in L.issues(self.c, "CUV") if r["problem"] == "새 문제 (을지에만)")
        self.assertEqual(([p["grp"] for p in L.photos(self.c, b["id"])], b["status"]), (["before"], "원인분석"))
        st = L.import_any(self.c, self.cfg, "CUV", "x.xlsx", data, "ASHA-9", today=TODAY)
        self.assertEqual((st["new"], st["photos"]), (0, 0))            # 같은 사진은 다시 넣지 않음

    def test_photos_marks_and_export(self):
        L.import_issues(self.c, "CUV", V4_ROWS, today=TODAY)
        iid = L.issues(self.c, "CUV", q="측면 BURR")[0]["id"]
        p1 = L.save_photo(self.c, self.cfg, iid, png(), "before")
        p2 = L.save_photo(self.c, self.cfg, iid, png((10, 10, 10)), "before")
        self.assertIsNone(L.save_photo(self.c, self.cfg, iid, png(), "before"))
        marks = [{"t": "ellipse", "a": {"x": .2, "y": .2}, "b": {"x": .4, "y": .4}, "c": "r"}, {"t": "bogus"}]
        L.set_marks(self.c, self.cfg, p1, marks, png((255, 255, 0)))
        r = self.c.execute("SELECT * FROM lm_photos WHERE id=?", (p1,)).fetchone()
        self.assertEqual(len(json.loads(r["marks"])), 1)
        self.assertTrue(Path(r["marked_path"]).is_file())
        L.move_photo(self.c, p2, -1)
        self.assertEqual([p["id"] for p in L.photos(self.c, iid, "before")], [p2, p1])
        L.set_marks(self.c, self.cfg, p1, [])
        self.assertFalse(Path(r["marked_path"]).exists())
        paths = L.export(self.c, self.cfg, "CUV", out_dir=self.d / "out")
        self.assertEqual(len(paths), 2)                                 # 통합 리스트 + 외주처 1곳
        import openpyxl
        wb = openpyxl.load_workbook(paths[1])
        self.assertEqual(wb.sheetnames[0], "MASTER")
        self.assertIn("86511-BN000", wb.sheetnames)
        self.assertEqual(len(wb["86511-BN000"]._images), 1)
        self.assertEqual(wb["86511-BN000"]["B4"].value, "측면 BURR")
        L.remove_photo(self.c, p2)
        self.assertEqual(len(L.photos(self.c, iid)), 1)

    def test_add_update_rules(self):
        mid, _ = L.upsert_mold(self.c, "CUV", "asha-9", "86511bn000", part_name="FR BUMPER")
        self.assertEqual(L.get_mold(self.c, mid)["part_no"], "86511-BN000")
        self.assertRaises(ValueError, L.add_issue, self.c, mid, problem="")
        iid = L.add_issue(self.c, mid, problem="보스 싱크 0.15mm", importance="상", due="2026-11-01", today=TODAY)
        r = L.get_issue(self.c, iid)
        self.assertEqual((r["category"], r["defect_type"], r["occ_date"]), ("SINK", "싱크", "2026-11-12"))
        self.assertEqual(L.delay_of(r, TODAY), "수정 11일 지연")
        L.update_issue(self.c, iid, status="대책적용", today=TODAY)
        self.assertEqual(L.wait_check(L.get_issue(self.c, iid)), "검증 T/O 필요")
        self.assertRaises(ValueError, L.update_issue, self.c, iid, status="종결", to_result="NG", today=TODAY)
        r = L.update_issue(self.c, iid, status="종결", to_result="OK", note="T3 OK", today=TODAY)
        self.assertEqual((r["closed_at"], r["edited"]), ("2026-11-12", 1))
        self.assertIn("T3 OK", r["note"])
        self.assertEqual([x["to_status"] for x in L.log(self.c, iid)], ["발생", "대책적용", "종결"])


class PhoneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.cfg = mkcfg(self.d)
        self.c = L.connect(self.cfg["db_path"])
        L.import_issues(self.c, "CUV", V4_ROWS, today=TODAY)

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def _download(self, docs: dict) -> Path:
        """ArtifactData out_dir 모양 (uploads/<id>.json, '~' 는 '@') + Artifact read 로 받은 사진 (assets/<id>.jpg)."""
        src = self.d / "phone"
        (src / "uploads").mkdir(parents=True)
        (src / "assets").mkdir()
        for i, (doc_id, d) in enumerate(docs.items()):
            (src / "uploads" / f"{doc_id.replace('~', '@')}.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            for k in ("asset", "flat"):
                if d.get(k):
                    (src / "assets" / f"{d[k]}.jpg").write_bytes(png((i * 40 % 255, 90, 30 + i)))
        return src

    def test_snapshot_keys_and_phone_import(self):
        snap = L.snapshot(self.c, "CUV")
        self.assertEqual(len(snap), 1)
        x = snap[0]
        self.assertEqual(len(x["molds"]), 3)
        self.assertEqual(len(x["issues"]), 3)                        # 진행 중만 (종결·보류 제외)
        key, lno = next((i[0], i[1]) for i in x["issues"] if i[6] == "측면 BURR")
        mark = [{"t": "ellipse", "a": {"x": .1, "y": .1}, "b": {"x": .2, "y": .2}, "c": "r"}]
        docs = {
            "u1": {"batch": "b1", "car": "CUV", "vendor": "ASHA-9", "part_no": "86511-BN000", "key": key, "l": 0, "grp": "after",
                   "asset": "a" * 32, "flat": "f" * 32, "marks": mark, "memo": "T3 사출품", "at": "2026-11-12T10:00:00Z", "synced": False},
            "u2": {"batch": "b2", "car": "CUV", "vendor": "ASHA-9", "part_no": "86599-BN000", "part_name": "BRKT-FR BUMPER", "key": "", "l": 0,
                   "problem": "보스 크랙", "category": "기타", "importance": "상", "grp": "before", "asset": "b" * 32,
                   "at": "2026-11-12T10:05:00Z", "synced": False},
            "u3": {"batch": "b2", "car": "CUV", "vendor": "ASHA-9", "part_no": "86599-BN000", "key": "", "l": 0, "problem": "보스 크랙",
                   "grp": "before", "asset": "c" * 32, "at": "2026-11-12T10:05:30Z", "synced": False},
            "old": {"car": "CUV", "part_no": "86511-BN000", "asset": "d" * 32, "synced": True},
        }
        ups, base = L.phone_uploads(self._download(docs))
        self.assertEqual([u["id"] for u in ups], ["u1", "u2", "u3"])   # 반영된 건은 빼고 시간순
        res = L.phone_import(self.c, self.cfg, ups, base, today=TODAY)
        self.assertEqual([r["status"] for r in res], ["added", "added", "added"])
        self.assertEqual(res[0]["l"], lno)
        self.assertEqual(res[1]["l"], res[2]["l"])                     # 같은 접수 묶음 = 문제점 1건
        self.assertTrue(res[1].get("new"))
        ph = L.photos(self.c, lno)
        self.assertEqual((ph[0]["grp"], ph[0]["source"], bool(ph[0]["marked_path"])), ("after", "phone", True))
        self.assertIn("폰] T3 사출품", L.get_issue(self.c, lno)["note"])
        n = L.get_issue(self.c, res[1]["l"])
        self.assertEqual((n["problem"], n["importance"], n["status"], n["occ_date"]), ("보스 크랙", "상", "발생", "2026-11-12"))
        self.assertEqual(len(L.photos(self.c, n["id"])), 2)
        self.assertEqual(len(L.molds(self.c, "CUV", "ASHA-9")), 4)      # 목록에 없던 금형은 등록
        again = L.phone_import(self.c, self.cfg, ups, base, today=TODAY)  # 다시 해도 중복 없음
        self.assertEqual([r["status"] for r in again], ["duplicate"] * 3)
        bad = L.phone_import(self.c, self.cfg, [{"id": "x", "car": "CUV", "part_no": "86511-BN000", "key": "", "l": 0, "file": "none.jpg"}],
                             base, today=TODAY)
        self.assertEqual(bad[0]["status"], "error")

    def test_app_issue_gets_portable_key(self):
        mid = L.molds(self.c, "CUV")[0]["id"]
        iid = L.add_issue(self.c, mid, problem="앱에서 등록", today=TODAY)
        self.assertTrue(L.get_issue(self.c, iid)["src_key"].startswith("app-"))
        self.c.execute("UPDATE lm_issues SET src_key=NULL WHERE id=?", (iid,))
        self.c.commit()
        keys = [i[0] for x in L.snapshot(self.c) for i in x["issues"]]
        self.assertTrue(all(keys))


class PriorityAndMoveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.cfg = mkcfg(self.d)
        self.c = L.connect(self.cfg["db_path"])

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def test_priority_order_and_reasons(self):
        mid, _ = L.upsert_mold(self.c, "CUV", "ASHA-9", "86511-BN000")
        a = L.add_issue(self.c, mid, problem="게이트 웰드", importance="하", occ_date="2026-11-01", today=TODAY)
        b = L.add_issue(self.c, mid, problem="보스 싱크", importance="상", occ_date="2026-11-01", today=TODAY)
        c = L.add_issue(self.c, mid, problem="취출 긁힘", importance="중", due="2026-11-02", occ_date="2026-10-20", today=TODAY)
        d = L.add_issue(self.c, mid, problem="리브 BURR", importance="중", status="대책적용", occ_date="2026-11-01", today=TODAY)
        L.add_issue(self.c, mid, problem="끝난 건", importance="상", status="종결", to_result="OK", today=TODAY)
        top = L.priority(self.c, "CUV", today=TODAY)
        self.assertEqual([r["id"] for r in top], [c, b, d, a])       # 지연 > 중요도 상 > 검증 T/O > 나머지, 종결 제외
        self.assertEqual(top[0]["why"][0], "수정 10일 지연")
        self.assertIn("검증 T/O 필요", top[2]["why"])

    def test_vendor_names_are_matched(self):
        for v, pn in (("SAFE DEMO", "86511-BN001"), ("TEKNIKAPLAST", "86511-BN002"), ("ASHA-1", "86511-BN003"), ("ASHA-12", "86511-BN004")):
            L.upsert_mold(self.c, "CUV", v, pn)
        got = [L.match_vendor(self.c, "CUV", v) for v in ("SAFEDEMO", "TEKNIKA", "ASHA1", "ASHA-2", "NEW")]
        self.assertEqual(got, ["SAFE DEMO", "TEKNIKAPLAST", "ASHA-1", "ASHA-2", "NEW"])

    def test_move_from_mailhub_keeps_numbers_and_photos(self):
        import sqlite3
        old = self.d / "mailhub.db"
        src = sqlite3.connect(old)
        src.executescript(L.SCHEMA.split("CREATE TABLE IF NOT EXISTS lm_photos")[0])
        src.execute("CREATE TABLE eo_images (id INTEGER PRIMARY KEY, eo_id INTEGER, kind TEXT, path TEXT, source TEXT, caption TEXT, "
                    "sha1 TEXT, sort INTEGER, grp TEXT, marks TEXT, marked_path TEXT, w INTEGER, h INTEGER, created_at TEXT)")
        src.execute("INSERT INTO lm_molds (id, car, vendor, part_no, part_name) VALUES (7, 'CUV', 'ASHA-9', '86511-BN000', 'COVER')")
        src.execute("INSERT INTO lm_issues (id, mold_id, car, vendor, part_no, problem, status, src_key) "
                    "VALUES (41, 7, 'CUV', 'ASHA-9', '86511-BN000', '측면 BURR', '대책수립', 'k1')")
        src.execute("INSERT INTO lm_log (issue_id, at, from_status, to_status, note) VALUES (41, '2026-11-01T09:00:00', '', '발생', '등록')")
        jpg = self.d / "old" / "01_before.jpg"
        jpg.parent.mkdir()
        jpg.write_bytes(png())
        src.execute("INSERT INTO eo_images (id, eo_id, kind, path, source, grp, marks, marked_path, w, h) "
                    "VALUES (3, 41, 'lm', ?, 'phone', 'before', '[]', '', 640, 480)", (str(jpg),))
        src.execute("INSERT INTO eo_images (id, eo_id, kind, path) VALUES (4, 41, 'eo', 'x.jpg')")   # E.O 그림은 옮기지 않음
        src.commit()
        src.close()
        res = L.from_mailhub(self.c, self.cfg, old)
        self.assertEqual((res["금형"], res["문제점"], res["이력"], res["사진"]), (1, 1, 1, 1))
        r = L.get_issue(self.c, 41)
        self.assertEqual((r["problem"], r["mold_id"]), ("측면 BURR", 7))
        p = L.photos(self.c, 41)[0]
        self.assertEqual((p["source"], p["grp"]), ("phone", "before"))
        self.assertTrue(Path(p["path"]).is_file() and Path(self.cfg["photo_dir"]) in Path(p["path"]).parents)
        self.assertRaises(ValueError, L.from_mailhub, self.c, self.cfg, old)    # 두 번은 안 옮김


class LocalApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cfg = mkcfg(Path(cls.tmp.name))
        c = L.connect(cls.cfg["db_path"])
        c.close()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
        port = cls.srv.server_address[1]
        cls.srv.RequestHandlerClass = make_handler(Data(cls.cfg), port)
        cls.base = f"http://127.0.0.1:{port}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def get(self, path):
        return json.load(urllib.request.urlopen(self.base + "/api/" + path))

    def post(self, path, body):
        req = urllib.request.Request(self.base + "/api/" + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req))

    def test_flow(self):
        self.assertEqual(self.get("board")["cars"], [])
        csv_text = "\n".join(",".join(f'"{x}"' for x in r) for r in V4_ROWS)
        r = self.post("import", {"car": "CUV", "name": "list.csv", "data": base64.b64encode(csv_text.encode()).decode()})
        self.assertEqual((r["kind"], r["new"]), ("문제점 리스트", 5))
        d = self.get("board?car=CUV")
        self.assertEqual((d["car"], len(d["molds"]), len(d["issues"])), ("CUV", 3, 5))
        mid = next(m["id"] for m in d["molds"] if m["part_no"] == "86511-BN000")
        iid = self.post("issue/add", {"mold_id": mid, "problem": "취출 긁힘", "category": "취출", "importance": "상"})["id"]
        url = "data:image/png;base64," + base64.b64encode(png()).decode()
        ph = self.post(f"issue/{iid}/photo", {"grp": "before", "data": url,
                                                    "marks": [{"t": "rect", "a": {"x": .1, "y": .1}, "b": {"x": .3, "y": .3}, "c": "b"}],
                                                    "flat": url})["photo"]
        self.assertTrue(ph["marked"] and ph["marks"][0]["c"] == "b")
        img = urllib.request.urlopen(self.base + ph["url"]).read()
        self.assertTrue(img.startswith(b"\xff\xd8"))                    # JPEG 로 저장
        self.post(f"photo/{ph['id']}", {"marks": []})
        self.assertEqual(self.get(f"issue/{iid}")["photos"][0]["marks"], [])
        r = self.post(f"issue/{iid}", {"status": "대책적용", "note": "랩핑 완료"})
        self.assertEqual((r["issue"]["status"], r["issue"]["wait"]), ("대책적용", "검증 T/O 필요"))
        m = self.get(f"mold/{mid}")
        self.assertEqual(len(m["issues"]), 3)
        self.assertEqual(len(next(x for x in m["issues"] if x["id"] == iid)["photo_list"]), 1)
        out = self.post("export", {"car": "CUV", "vendor": "ASHA-9"})
        self.assertTrue(out["path"].endswith(".xlsx"))
        self.post(f"issue/{iid}/del", {})
        self.assertEqual(len(self.get(f"mold/{mid}")["issues"]), 2)
        s = self.get("summary")
        self.assertEqual((s["total"]["issues"], s["cars"][0]["car"]), (5, "CUV"))
        self.assertTrue(s["top"] and s["top"][0]["why"])
        self.assertEqual(self.get("top?car=CUV&limit=3")["issues"][0]["id"], s["top"][0]["id"])
        self.assertEqual(self.get("find?q=" + urllib.parse.quote("싱크"))["issues"][0]["problem"], "리브 싱크")
        self.assertIn("localmold", self.get("ping")["app"])


if __name__ == "__main__":
    unittest.main()
