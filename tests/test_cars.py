import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from mailhub import db
from mailhub.classify import Classifier, car_key, find_cars
from mailhub.config import load_config
from mailhub.webapp import Data, make_handler


class CarTest(unittest.TestCase):
    def test_find(self):
        cases = {
            "BC4t CUV 차종 범퍼 금형 검토 요청": ["BC4t"], "RE: [BC4 HEV N] 카울탑커버 EO": ["BC4"],
            "Re: NX4a SBC CONSOLE 최신 EO": ["NX4a"], "GN7 PE EO 적용시점 통보": ["GN7"],
            "(Warning_Threat)RE: BC4t CUV 문제점 송부 件": ["BC4t"], "[매우 중요]Re: BC4t CUV PV 신뢰성": ["BC4t"],
            "[긴급] LP2 이벤트 TD20 재질 PP20": [], "[전사 보안공지] 해킹": [], "RE: MD-2611 금형 이관 KD LIST": [],
            "BC4t / GN7 공용 부품 검토": ["BC4t", "GN7"],
        }
        for subj, want in cases.items():
            self.assertEqual(find_cars(subj), want, subj)
        self.assertEqual(find_cars("공유드립니다", "BC4t 5DR 범퍼 관련하여 문의드립니다"), ["BC4t"])   # 본문 앞부분
        self.assertEqual(find_cars("공유드립니다", "BC4t 관련하여"), [])                                # 본문은 엄격하게
        self.assertEqual(find_cars("XX1 부품 리스트", known={"XX1"}), ["XX1"])
        self.assertEqual(car_key("BC4t"), car_key("BC4b"))
        cfg = load_config("none.toml")
        self.assertEqual(Classifier(cfg).classify({"subject": "[GN7 PE] 범퍼", "body": ""})["cars"], "GN7")

    def test_folders_and_filter(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(Path(d) / "x.toml")
            cfg["archive"]["db_path"] = str(Path(d) / "m.db")
            c = db.connect(cfg["archive"]["db_path"])
            clf = Classifier(cfg)
            subjects = ["BC4t CUV 범퍼 1", "BC4t CUV 범퍼 2", "[BC4 HEV N] 카울", "BC4b/t ARM REST", "GN7 PE EO 1",
                        "GN7 PE EO 2", "GN7 PE EO 3", "NX4a SBC CONSOLE", "공지"]
            tags = ["E.O", "T/O", "E.O", "", "E.O", "", "", "", ""]
            for i, sub in enumerate(subjects):
                rec = {"subject": sub, "body": "", "sent_at": f"2026-11-0{1 + i % 8}T09:00:00", "tags": tags[i]}
                rec.update({k: v for k, v in clf.classify(rec).items() if k != "tags"})
                db.insert_mail(c, f"k{i}", rec)
            for i in range(3):   # 오토웨이 접수 (RPABOT) - BC4t·E.O 가 들어 있어도 차종·분류 폴더에서는 빠짐
                rec = {"subject": f"BC4t CUV EO 시스템 알림 {i}", "sender_name": "RPABOT", "sender_email": "rpabot@corp.com",
                       "tags": "E.O", "sent_at": "2026-11-05T08:00:00"}
                rec.update({k: v for k, v in clf.classify(rec).items() if k != "tags"})
                db.insert_mail(c, f"r{i}", rec)
            c.commit()
            c.close()
            srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
            port = srv.server_address[1]
            srv.RequestHandlerClass = make_handler(Data(cfg), port)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                def get(p):
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/{p}") as r:
                        return json.loads(r.read())
                mc = get("mailcounts")
                cars = mc["cars"]
                # BC4t·BC4·BC4b → 한 폴더(이름은 가장 많이 쓴 BC4t), NX4a 는 1건이라 폴더 없음, RPABOT 은 제외
                self.assertEqual([(x["key"], x["name"], x["count"]) for x in cars], [("BC4", "BC4t", 4), ("GN7", "GN7", 3)])
                self.assertEqual((cars[0]["tags"]["E.O"], cars[0]["tags"]["T/O"], cars[1]["tags"]["E.O"]), (2, 1, 1))
                self.assertEqual((mc["autoway"], mc["tags"]["E.O"]), (3, 3))
                self.assertEqual(len(get("mails?car=BC4")), 4)
                self.assertEqual(len(get("mails?car=GN7")), 3)
                self.assertEqual(len(get("mails?car=BC4&tag=E.O")), 2)
                self.assertEqual(len(get("mails?tag=E.O")), 3)
                auto = get("mails?special=autoway")
                self.assertEqual((len(auto), auto[0]["sender_name"]), (3, "RPABOT"))
                self.assertEqual(len(get("mails")), 12)          # 전체 메일에는 모두
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()


class BackfillTest(unittest.TestCase):
    def test_backfill_once(self):
        from mailhub.webapp import backfill_cars
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(Path(d) / "x.toml")
            cfg["archive"]["db_path"] = str(Path(d) / "m.db")
            c = db.connect(cfg["archive"]["db_path"])
            for i in range(4):   # 차종 기능 전에 들어온 메일 (cars 비어 있음)
                db.insert_mail(c, f"k{i}", {"subject": f"BC4t CUV 범퍼 {i}", "body": ""})
            c.commit()
            c.close()
            data = Data(cfg)
            self.assertEqual(backfill_cars(data, log=lambda *_: None), 4)
            self.assertEqual(backfill_cars(data, log=lambda *_: None), 0)          # 한 번만
            c = db.connect(cfg["archive"]["db_path"])
            self.assertEqual({r[0] for r in c.execute("SELECT cars FROM mails")}, {"BC4t"})
            c.close()
            self.assertEqual(data.mailcounts(data.conn())["cars"][0]["name"], "BC4t")
