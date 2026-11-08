import tempfile
import unittest
from datetime import date
from pathlib import Path

from mailhub import db, followup as fu
from mailhub.classify import thread_key
from mailhub.config import load_config


class FollowupTest(unittest.TestCase):
    def test_parse_due(self):
        t = date(2026, 11, 6)  # 금
        self.assertEqual(fu.parse_due("11/12", t), "2026-11-12")
        self.assertEqual(fu.parse_due("2026-11-12", t), "2026-11-12")
        self.assertEqual(fu.parse_due("+3", t), "2026-11-09")
        self.assertEqual(fu.parse_due("+3영업일", t), "2026-11-11")  # 주말 건너뜀
        self.assertEqual(fu.parse_due("1/5", date(2026, 12, 20)), "2027-01-05")
        self.assertIn("2일 지남", fu.dday("2026-11-04", t))
        with self.assertRaises(ValueError):
            fu.parse_due("다음주", t)

    def test_flow(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(Path(d) / "none.toml")
            cfg["me"].update(signature="서연이화 개발3팀 이성욱 대리")
            conn = db.connect(Path(d) / "t.db")
            t0 = date(2026, 11, 2)
            r1 = fu.add(conn, "kim@abc-mold.co.kr", "BC4t CUV 범퍼 보스 높이 변경 금형수정 검토 요청", "11/4",
                        vendor="ABC정밀", req_type="금형수정검토", today=t0)
            r2 = fu.add(conn, "lee@cf-tech.co.kr", "헤드라이닝 검사구 수정 요청", "11/10", vendor="CF테크", today=t0)

            # 다른 업체가 같은 제목으로 보낸 메일은 회신으로 보지 않음
            db.insert_mail(conn, "x1", {"subject": "RE: BC4t CUV 범퍼 보스 높이 변경 금형수정 검토 요청",
                                        "sender_email": "park@other.co.kr", "sent_at": "2026-11-03T10:00:00",
                                        "thread_key": thread_key("RE: BC4t CUV 범퍼 보스 높이 변경 금형수정 검토 요청")})
            res = fu.check(conn, cfg, today=date(2026, 11, 6))
            self.assertEqual([r["id"] for r in res["overdue"]], [r1])
            self.assertEqual([r["id"] for r in res["remind"]], [r1])

            subj, body = fu.reminder_mail(res["remind"][0], cfg)
            self.assertTrue(subj.startswith("[재요청] BC4t CUV"))
            self.assertIn("11/4", body)
            self.assertIn("이성욱", body)
            fu.mark_reminded(conn, r1)
            # 재요청 직후에는 interval(2일) 동안 다시 재요청 대상 아님
            self.assertEqual(fu.check(conn, cfg, today=date(2026, 11, 6))["remind"], [])
            row = conn.execute("SELECT * FROM requests WHERE id=?", (r1,)).fetchone()
            self.assertTrue(fu.reminder_mail(row, cfg)[0].startswith("[재요청 2차]"))

            # 업체 회신 도착 → 자동 '회신받음'
            db.insert_mail(conn, "x2", {"subject": "RE: BC4t CUV 범퍼 보스 높이 변경 금형수정 검토 요청",
                                        "sender_email": "kim@abc-mold.co.kr", "sender_name": "김과장",
                                        "sent_at": "2026-11-07T09:00:00",
                                        "thread_key": thread_key("RE: BC4t CUV 범퍼 보스 높이 변경 금형수정 검토 요청")})
            res = fu.check(conn, cfg, today=date(2026, 11, 9))
            self.assertEqual([r["id"] for r, _ in res["replied"]], [r1])
            self.assertEqual([r["id"] for r in res["soon"]], [r2])  # 11/10 = D-1
            st = conn.execute("SELECT status, reply_mail_id FROM requests WHERE id=?", (r1,)).fetchone()
            self.assertEqual(st[0], "회신받음")
            conn.close()


if __name__ == "__main__":
    unittest.main()


class AskTest(unittest.TestCase):
    def test_popup_decisions(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(Path(d) / "none.toml")
            conn = db.connect(Path(d) / "t.db")
            t0, today = date(2026, 11, 2), date(2026, 11, 9)
            ids = [fu.add(conn, f"a{i}@v.co.kr", f"요청 {i}", "11/4", today=t0) for i in range(4)]
            targets = fu.check(conn, cfg, today=today)["remind"]
            self.assertEqual(len(targets), 4)
            answers = iter(["send", "outlook", "extend", "skip"])
            sent = []

            def asker(r, subject, body, i, n):
                return next(answers), body + "\n(수정함)"

            def sender(to, cc, subject, body, send, draft_dir):
                sent.append((to, send, body.endswith("(수정함)")))

            res = fu.ask_reminders(conn, cfg, targets, asker=asker, sender=sender, today=today, log=lambda *_: None)
            self.assertEqual(res, {"send": 1, "outlook": 1, "extend": 1, "skip": 1, "error": 0})
            self.assertEqual(sent, [("a0@v.co.kr", True, True), ("a1@v.co.kr", False, True)])
            rows = {r["id"]: r for r in conn.execute("SELECT * FROM requests")}
            self.assertEqual(rows[ids[0]]["remind_count"], 1)
            self.assertEqual(rows[ids[2]]["due_date"], "2026-11-11")   # +2영업일
            self.assertEqual(rows[ids[3]]["remind_count"], 0)
            # 같은 날 다시 확인해도 묻지 않음
            again = fu.check(conn, cfg, today=today)["remind"]
            res2 = fu.ask_reminders(conn, cfg, again, asker=lambda *a: self.fail("다시 물으면 안 됨"),
                                    sender=sender, today=today, log=lambda *_: None)
            self.assertEqual(sum(res2.values()), 0)
            # 다음 날에는 건너뛴 건을 다시 물음
            nxt = fu.check(conn, cfg, today=date(2026, 11, 10))["remind"]
            self.assertIn(ids[3], [r["id"] for r in nxt])
            conn.close()


class CliTest(unittest.TestCase):
    """명령어(main) 경로로 실행 - import 누락 같은 실수를 잡기 위함."""

    def test_req_commands(self):
        import contextlib
        import io
        import os
        from mailhub.cli import main
        with tempfile.TemporaryDirectory() as d:
            cfgp = Path(d) / "c.toml"
            cfgp.write_text(f'[archive]\ndb_path = "{(Path(d) / "m.db").as_posix()}"\n'
                            f'draft_dir = "{(Path(d) / "drafts").as_posix()}"\n', encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                main(["--config", str(cfgp), "req", "add", "--to", "t@example.com", "--subject", "확인창 시험",
                      "--due", "2020-01-02", "--requested", "2020-01-01", "--vendor", "시험업체"])
                main(["--config", str(cfgp), "req", "list"])
                main(["--config", str(cfgp), "req", "check"])
                main(["--config", str(cfgp), "req", "check", "--ask", "--notify"])
                main(["--config", str(cfgp), "req", "remind", "1", "--preview"])
                main(["--config", str(cfgp), "req", "update", "1", "--status", "취소"])
                main(["--config", str(cfgp), "req", "list", "--all", "--json"])
            text = out.getvalue()
            self.assertIn("확인창 시험", text)
            self.assertIn("기한 지남", text)
            self.assertIn("제목: [재요청", text)
            self.assertIn("아웃룩 창 1", text)
            self.assertIn("취소", text)
            if os.name != "nt":  # 창을 못 띄우면 안전하게 초안 파일로
                self.assertTrue(list((Path(d) / "drafts").glob("*.eml")))
