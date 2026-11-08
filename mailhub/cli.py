"""mailhub 명령어.  사용법: python -m mailhub <명령> --help"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from . import db
from .classify import Classifier
from .config import load_config
from .search import search, snippet, todo

LIST_COLS = ["id", "sent_at", "sender_name", "sender_email", "subject", "tags", "part_nos",
             "attachments", "needs_action", "status", "note", "folder"]


def _out(s: str = ""):
    print(s)


def _row_dict(r, with_body=False, query=""):
    d = {k: r[k] for k in LIST_COLS}
    d["snippet"] = snippet(r["body"], query, att_text=r["att_text"])
    if with_body:
        d.update({"to_addr": r["to_addr"], "cc_addr": r["cc_addr"], "body": r["body"], "att_text": r["att_text"],
                  "file_path": r["file_path"], "source": r["source"]})
    return d


def _short_parts(parts: str, n: int = 5) -> str:
    items = [p for p in (parts or "").split(",") if p]
    return ",".join(items[:n]) + (f" 외 {len(items) - n}개" if len(items) > n else "")


def _group_threads(rows, limit):
    """같은 건(RE:/FW: 뗀 제목)은 첫 번째(최신 또는 가장 관련 높은) 메일만 남기고 통수를 센다."""
    seen, out, counts = {}, [], {}
    for r in rows:
        k = r["thread_key"] or f"#{r['id']}"
        if k in seen:
            counts[seen[k]] += 1
            continue
        seen[k] = r["id"]
        counts[r["id"]] = 1
        out.append(r)
    return out[:limit], counts


def _print_rows(rows, query, as_json=False, counts=None):
    counts = counts or {}
    if as_json:
        _out(json.dumps([_row_dict(r, query=query) for r in rows], ensure_ascii=False, indent=1))
        return
    if not rows:
        _out("검색 결과가 없습니다.")
        return
    for r in rows:
        flag = "★" if r["needs_action"] and r["status"] != "완료" else " "
        att = f" [첨부{len(r['attachments'].splitlines())}]" if r["attachments"] else ""
        st = f" [{r['status']}]" if r["status"] else ""
        grp = f" (같은 건 {counts[r['id']]}통)" if counts.get(r["id"], 1) > 1 else ""
        _out(f"{flag}#{r['id']:<6} {r['sent_at'][:16].replace('T', ' ')}  {r['sender_name'][:16]:<16} "
             f"| {r['subject'][:70]}{att}{st}{grp}")
        meta = "  ".join(x for x in [f"태그:{r['tags']}" if r["tags"] else "",
                                     f"품번:{_short_parts(r['part_nos'])}" if r["part_nos"] else ""] if x)
        if meta:
            _out(f"         {meta}")
        _out(f"         {snippet(r['body'], query, att_text=r['att_text'])}")
    _out(f"\n총 {len(rows)}건 (★ = 검토/회신 필요)")


def cmd_ingest_outlook(args, cfg, conn):
    from .ingest_outlook import ingest_outlook, list_stores
    if args.list:
        list_stores(_out)
        return
    since = datetime.fromisoformat(args.since) if args.since else None
    st = ingest_outlook(conn, cfg, store_filter=args.store, pst_paths=args.pst or [], since=since,
                        save_msg=args.save_msg, save_attachments=args.save_attachments,
                        folder_filter=args.folder, log=_out)
    _out(f"\n완료: 폴더 {st['folders']} / 확인 {st['seen']} / 신규 {st['added']} / 중복 {st['skipped']} / 오류 {st['errors']}")
    if st.get("save_failed"):
        _out(f"원본 사본 저장 실패 {st['save_failed']}건 (메일 내용·검색은 정상, 원본은 아웃룩/PST 에서 'open' 으로 열 수 있음)")


def cmd_imap_setup(args, cfg, conn):
    from .ingest_imap import setup
    setup(cfg, log=_out)


def cmd_ingest_imap(args, cfg, conn):
    from .ingest_imap import ingest_imap
    st = ingest_imap(conn, cfg, since=args.since, folder_filter=args.folder, save_eml=args.save_eml,
                     full=args.full, interactive=not args.no_prompt, log=_out)
    _out(f"\n완료: 폴더 {st['folders']} / 받음 {st['fetched']} / 신규 {st['added']} / 중복 {st['skipped']} / 오류 {st['errors']}"
         "\n(그룹웨어의 읽음/안읽음 상태는 바뀌지 않았습니다)")


def cmd_pop_test(args, cfg, conn):
    from .ingest_pop import pop_test
    pop_test(cfg, "full" if args.full else "header", log=_out)


def cmd_ingest_pop(args, cfg, conn):
    from .ingest_pop import ingest_pop
    st = ingest_pop(conn, cfg, since=args.since, max_new=args.max, save_eml=args.save_eml,
                    interactive=not args.no_prompt, log=_out)
    _out(f"\n{'중단' if st.get('interrupted') else '완료'}: 서버 {st['total']:,} / 확인 {st['checked']:,} / 본문 받음 {st['fetched']:,} / 신규 {st['added']:,}"
         f" / 이미 있음 {st['skipped']:,} / 기간 밖 {st['old']:,} / 오류 {st['errors']}")


def cmd_index_attachments(args, cfg, conn):
    from .index_attach import index_attachments
    st = index_attachments(conn, cfg, since=args.since, use_outlook=args.outlook, limit=args.limit, log=_out)
    _out(f"\n{'중단' if st['interrupted'] else '완료'}: 대상 {st['target']:,} / 처리 {st['done']:,} / "
         f"내용 추출 {st['with_text']:,} / 원본 없음 {st['no_source']:,} / 오류 {st['errors']:,}")
    if st["with_text"]:
        _out("첨부 안의 품번까지 품번 검색(--part)에 반영하려면: python -m mailhub retag")
    if st["no_source"] and not args.outlook:
        _out("원본 없음 = 아웃룩(PST)에서 가져온 메일입니다. 아웃룩을 켠 상태에서 --outlook 을 붙여 실행하세요.")


def _req_line(r) -> str:
    from .followup import dday
    st = r["status"]
    if st == "요청":
        st = "요청(" + dday(r["due_date"]) + ")"
    who = r["vendor"] or r["to_addrs"]
    extra = f"  재요청 {r['remind_count']}회" if r["remind_count"] else ""
    rep = f"  ← 회신 #{r['reply_mail_id']} {str(r['replied_at'])[:10]}" if r["reply_mail_id"] else ""
    return (f"R{r['id']:<4} {st:<22} {who[:24]:<24} | {r['subject'][:60]}"
            f"{'  [' + r['req_type'] + ']' if r['req_type'] else ''}{extra}{rep}")


def cmd_req(args, cfg, conn):
    from . import eo as E
    from . import followup as fu
    fu.ensure(conn)
    E.ensure(conn)
    a = args.req_cmd
    if a == "add":
        rid = fu.add(conn, args.to, args.subject, args.due, cc=args.cc or "", vendor=args.vendor or "",
                     req_type=args.type or "", part_no=args.part or "", mail_id=args.mail, note=args.note or "",
                     requested=args.requested, eo_id=args.eo)
        r = conn.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        _out("요청 등록:\n  " + _req_line(r))
        return
    if a == "list":
        where = "" if args.all else "WHERE status = '요청'"
        rows = conn.execute(f"SELECT * FROM requests {where} ORDER BY status != '요청', due_date").fetchall()
        if args.json:
            _out(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=1))
            return
        for r in rows:
            _out(_req_line(r))
        _out(f"\n총 {len(rows)}건" + ("" if args.all else " (진행 중인 요청만, 전체: --all)"))
        return
    if a == "check":
        res = fu.check(conn, cfg)
        for r, rep in res["replied"]:
            _out(f"✓ 회신받음  R{r['id']} {r['subject'][:50]}  ← #{rep['id']} {rep['sender_name']} {rep['sent_at'][:10]}")
        for r in res["overdue"]:
            _out("! 기한 지남 " + _req_line(r))
        for r in res["soon"]:
            _out("- 기한 임박 " + _req_line(r))
        if not any(res.values()):
            _out("기한이 지났거나 임박한 요청이 없습니다.")
        if res["remind"] and args.ask:
            lock = Path(cfg["archive"]["db_path"]).parent / ".req_popup.lock"
            import time as _t
            if lock.exists() and _t.time() - lock.stat().st_mtime < 6 * 3600:
                _out("(다른 재요청 확인 창이 열려 있어 이번에는 묻지 않습니다)")
            else:
                lock.write_text(str(os.getpid()))
                try:
                    r2 = fu.ask_reminders(conn, cfg, res["remind"], log=_out)
                finally:
                    lock.unlink(missing_ok=True)
                _out(f"재요청 확인: 발송 {r2['send']} / 아웃룩 창 {r2['outlook']} / 기한 연장 {r2['extend']} / 건너뜀 {r2['skip']}")
        elif res["remind"]:
            _out("\n재요청 메일 띄우기: " + "  ".join(f"python -m mailhub req remind {r['id']}" for r in res["remind"])
                 + "\n(확인 창으로 하나씩 결정하려면: python -m mailhub req check --ask)")
        if args.notify and (res["overdue"] or res["soon"] or res["replied"]):
            parts = []
            if res["overdue"]:
                parts.append(f"기한 지남 {len(res['overdue'])}건")
            if res["soon"]:
                parts.append(f"오늘·내일 기한 {len(res['soon'])}건")
            if res["replied"]:
                parts.append(f"업체 회신 {len(res['replied'])}건")
            first = (res["overdue"] or res["soon"] or [r for r, _ in res["replied"]])[0]
            fu.notify("업체 요청 회신 확인 - " + ", ".join(parts), f"{first['vendor'] or first['to_addrs']}: {first['subject'][:60]}")
        return
    if a == "remind":
        r = conn.execute("SELECT * FROM requests WHERE id=?", (args.id,)).fetchone()
        if not r:
            sys.exit(f"R{args.id} 요청이 없습니다.")
        subj, body = fu.reminder_mail(r, cfg)
        if args.preview:
            _out(f"받는사람: {r['to_addrs']}\n참조: {r['cc_addrs']}\n제목: {subj}\n\n{body}")
            return
        if args.ask:
            action, body = fu.ask_popup(r, subj, body, 1, 1)
            if action == "skip":
                _out(f"R{r['id']} 건너뜀")
                return
            if action == "extend":
                conn.execute("UPDATE requests SET due_date=? WHERE id=?", (fu.parse_due("+2영업일"), r["id"]))
                conn.commit()
                _out(_req_line(conn.execute("SELECT * FROM requests WHERE id=?", (r["id"],)).fetchone()))
                return
            send = action == "send"
        else:
            send = False
        res = fu.open_draft(r["to_addrs"], r["cc_addrs"], subj, body, send=send,
                            draft_dir=Path(cfg["archive"].get("draft_dir", "data/drafts")))
        fu.mark_reminded(conn, r["id"])
        _out(f"R{r['id']} 재요청: {res}")
        return
    if a == "update":
        r = conn.execute("SELECT * FROM requests WHERE id=?", (args.id,)).fetchone()
        if not r:
            sys.exit(f"R{args.id} 요청이 없습니다.")
        if args.due:
            conn.execute("UPDATE requests SET due_date=? WHERE id=?", (fu.parse_due(args.due), args.id))
        if args.status:
            conn.execute("UPDATE requests SET status=? WHERE id=?", (args.status, args.id))
        if args.note is not None:
            conn.execute("UPDATE requests SET note=? WHERE id=?", (args.note, args.id))
        conn.commit()
        _out(_req_line(conn.execute("SELECT * FROM requests WHERE id=?", (args.id,)).fetchone()))
        return


def _eo_line(conn, r) -> str:
    from . import eo as E
    from .followup import dday
    dl = E.delay_of(r)
    soon = E.soon_of(r)
    flag = f" ⚠{dl}" if dl else (f" ⏰{soon}" if soon else "")
    nm = E.new_mail_count(conn, r)
    new = f"  ✉새 관련메일 {nm}" if nm else ""
    due = ""
    for f, label in (("review_due", "검토"), ("mold_due", "수정"), ("verify_due", "검증")):
        if r[f] and r["status"] not in E.CLOSED:
            due += f" {label}:{dday(r[f]).split(' ')[0]}"
    name = " ".join(x for x in (r["car"], r["part_no"], r["part_name"]) if x)
    return (f"E{r['id']:<4} [{r['status']}] {name[:50]}"
            f"{'  EO:' + r['eo_no'] if r['eo_no'] else ''}{'  /' + r['mold_vendor'] if r['mold_vendor'] else ''}"
            f"{due}{flag}{new}\n       {r['change_desc'][:90]}")


def _eo_fields(args) -> dict:
    keys = ["eo_no", "car", "part_no", "part_name", "change_desc", "customer", "received_at", "mold_vendor",
            "mold_action", "cf_impact", "cf_vendor", "apply_point", "cost", "review_due", "mold_due", "verify_due",
            "reviewed_at", "ordered_at", "mold_done_at", "verified_at"]
    return {k: getattr(args, k) for k in keys if getattr(args, k, None) is not None}


def cmd_eo(args, cfg, conn):
    from . import eo as E
    E.ensure(conn)
    a = args.eo_cmd
    if a == "add":
        eid = E.add(conn, status=args.status or "접수", mail_id=args.mail, note=args.note or "", **_eo_fields(args))
        _out("E.O 등록:\n" + _eo_line(conn, conn.execute("SELECT * FROM eos WHERE id=?", (eid,)).fetchone()))
        return
    if a == "set":
        r = E.update(conn, args.id, status=args.status, note=args.note, mail_id=args.mail, **_eo_fields(args))
        _out(_eo_line(conn, r))
        return
    if a == "link":
        for m in args.mails:
            E.link(conn, args.id, m, "수동")
        conn.commit()
        _out(f"E{args.id} 에 메일 {', '.join('#' + str(m) for m in args.mails)} 연결")
        return
    if a == "list":
        rows = E.listing(conn, args.status, car=args.car, q=args.q)
        if args.json:
            out = []
            for r in rows:
                d = dict(r)
                d["delay"] = E.delay_of(r)
                d["soon"] = E.soon_of(r)
                d["new_mails"] = E.new_mail_count(conn, r)
                out.append(d)
            _out(json.dumps(out, ensure_ascii=False, indent=1))
            return
        for r in rows:
            _out(_eo_line(conn, r))
        n_delay = sum(1 for r in rows if E.delay_of(r))
        _out(f"\n총 {len(rows)}건 / 지연 {n_delay}건" + ("  (진행 중만. 전체: --status all)" if args.status == "open" else ""))
        return
    if a == "show":
        r = conn.execute("SELECT * FROM eos WHERE id=?", (args.id,)).fetchone()
        if not r:
            sys.exit(f"E{args.id} 항목이 없습니다.")
        mails = conn.execute(
            "SELECT m.id, m.sent_at, m.sender_name, m.subject, em.how FROM eo_mails em JOIN mails m ON m.id=em.mail_id "
            "WHERE em.eo_id=? ORDER BY m.sent_at", (args.id,)).fetchall()
        logs = conn.execute("SELECT * FROM eo_log WHERE eo_id=? ORDER BY id", (args.id,)).fetchall()
        from . import followup as fu
        fu.ensure(conn)
        reqs = conn.execute("SELECT * FROM requests WHERE eo_id=? ORDER BY id", (args.id,)).fetchall()
        if args.json:
            _out(json.dumps({"eo": dict(r), "delay": E.delay_of(r), "mails": [dict(m) for m in mails],
                             "log": [dict(x) for x in logs], "requests": [dict(q) for q in reqs]},
                            ensure_ascii=False, indent=1))
        else:
            _out(_eo_line(conn, r))
            for k in E.FIELDS:
                if r[k] and k not in ("change_desc",):
                    _out(f"  {E.HEADERS.get(k, k):<10}: {r[k]}")
            _out("\n[이력]")
            for x in logs:
                _out(f"  {x['at'][:16].replace('T', ' ')}  {x['from_status'] or '-'} → {x['to_status']}  {x['note'] or ''}"
                     f"{'  #' + str(x['mail_id']) if x['mail_id'] else ''}")
            if reqs:
                _out("\n[업체 요청]")
                for q in reqs:
                    _out("  " + _req_line(q))
            _out("\n[관련 메일]")
            seen = r["seen_mail_max"] or 0
            for m in mails:
                _out(f"  {'★' if m['id'] > seen else ' '}#{m['id']:<6} {m['sent_at'][:10]}  {m['sender_name'][:14]:<14} "
                     f"| {m['subject'][:60]}  ({m['how']})")
        E.mark_seen(conn, args.id)
        return
    if a == "inbox":
        from datetime import timedelta
        since = args.since or (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
        rows = E.inbox(conn, since, args.limit)
        if args.json:
            _out(json.dumps([_row_dict(r) for r in rows], ensure_ascii=False, indent=1))
            return
        _print_rows(rows, "")
        _out(f"↑ E.O 관련인데 아직 E.O 리스트에 연결되지 않은 메일 ({since} 이후, 같은 건은 1통만)\n"
             "  등록: python -m mailhub eo add --mail <번호> --car .. --part-no .. --desc ..  /  기존 항목에 연결: eo link <E번호> <메일번호>")
        return
    if a == "sync":
        added = E.sync(conn)
        for eid, mid, how in added:
            _out(f"E{eid} ← #{mid} ({how})")
        _out(f"새로 연결된 메일 {len(added)}건")
        return
    if a == "check":
        added = E.sync(conn)
        rows = E.listing(conn, "open")
        delayed = [(r, E.delay_of(r)) for r in rows if E.delay_of(r)]
        soon = [(r, E.soon_of(r)) for r in rows if E.soon_of(r) and not E.delay_of(r)]
        news = [r for r in rows if E.new_mail_count(conn, r)]
        for r, d in delayed:
            _out(f"! 지연   E{r['id']} {r['car']} {r['part_no']} {r['part_name']} - {d}")
        for r, d in soon:
            _out(f"- 임박   E{r['id']} {r['car']} {r['part_no']} {r['part_name']} - {d}")
        for r in news:
            _out(f"✉ 새 메일 E{r['id']} {r['car']} {r['part_no']} {r['part_name']} - 관련 메일 {E.new_mail_count(conn, r)}건 (eo show {r['id']})")
        if not (delayed or soon or news):
            _out("E.O: 지연·임박·새 관련 메일 없음")
        n_inbox = len(E.inbox(conn, (datetime.now().replace(day=1)).strftime("%Y-%m-01"), 200))
        if n_inbox:
            _out(f"? 미등록 E.O 메일 {n_inbox}건 (이번 달) → python -m mailhub eo inbox")
        if args.notify and (delayed or soon):
            from .followup import notify
            r0, d0 = (delayed or soon)[0]
            notify(f"E.O 지연 {len(delayed)}건 / 임박 {len(soon)}건",
                   f"E{r0['id']} {r0['car']} {r0['part_name'] or r0['part_no']} - {d0}")
        return
    if a == "img":
        from . import eo_images as I
        I.ensure(conn)
        if args.all:
            targets = [r["id"] for r in E.listing(conn, "open")]
        elif args.id:
            targets = [args.id]
        else:
            sys.exit("E번호를 넣거나 --all 을 주세요. 예) eo img 3 --collect")
        if args.paste:
            ok = I.paste(conn, cfg, targets[0], args.caption or "")
            _out("캡처 그림을 저장했습니다." if ok else "이미 저장된 그림과 같아서 건너뛰었습니다.")
        if args.add:
            _out(f"{I.add_files(conn, cfg, targets[0], args.add, args.caption or '')}장 추가")
        if args.remove:
            for n in args.remove:
                _out(f"[{n}] 삭제" if I.remove(conn, targets[0], n) else f"[{n}] 없음")
        if args.collect or args.all:
            for eid in targets:
                n = I.collect(conn, cfg, eid, use_outlook=args.outlook, use_powerpoint=not args.no_ppt, log=_out)
                if n or not args.all:
                    _out(f"E{eid}: 그림 {n}장 새로 저장")
        if not args.all:
            imgs = I.listing(conn, targets[0])
            _out(f"\nE{targets[0]} 참고 그림 {len(imgs)}장 (엑셀 내보내기에 들어감)")
            for im in imgs:
                _out(f"  [{im['sort']}] {im['caption']}  ({im['source']})")
            if imgs:
                _out(f"  필요 없는 그림 빼기: python -m mailhub eo img {targets[0]} --remove 번호")
        return
    if a == "export":
        if args.collect and not args.no_images:
            from . import eo_images as I
            I.ensure(conn)
            total = 0
            for r in E.listing(conn, args.status):
                total += I.collect(conn, cfg, r["id"], use_outlook=args.outlook, use_powerpoint=True, log=_out)
            _out(f"참고 그림 {total}장 새로 모음")
        p = E.export(conn, args.path, args.status, with_images=not args.no_images)
        _out(f"E.O 리스트 저장: {p.resolve()}")
        if args.open and os.name == "nt":
            os.startfile(str(p.resolve()))  # type: ignore[attr-defined]
        return
    if a == "import":
        ids = E.import_csv(conn, args.path)
        _out(f"{len(ids)}건 가져옴 (E{ids[0]}~E{ids[-1]})" if ids else "가져온 항목이 없습니다 (열 이름 확인)")
        return


def _to_line(conn, r) -> str:
    from . import to as T
    dl, soon = T.delay_of(r), T.soon_of(r)
    flag = f" ⚠{dl}" if dl else (f" ⏰{soon}" if soon else "")
    rec = f" ↻재발(T{r['recur_of']})" if r["recur_of"] else ""
    nm = T.new_mail_count(conn, r)
    new = f"  ✉새 관련메일 {nm}" if nm else ""
    name = " ".join(x for x in (r["car"], r["part_no"], r["part_name"]) if x)
    to = " ".join(x for x in (r["to_stage"], (r["to_date"] or "")[5:].replace("-", "/")) if x)
    cm = f"\n       대책: {r['countermeasure'][:80]}" if r["countermeasure"] else ""
    return (f"T{r['id']:<4} [{r['status']}] {r['defect_type']}  {name[:44]}  ({to}){rec}{flag}{new}"
            f"\n       현상: {(r['location'] + ' - ' if r['location'] else '') + r['phenomenon'][:80]}{cm}")


def _to_fields(args) -> dict:
    from . import to as T
    d = {k: getattr(args, k) for k in T.FIELDS if getattr(args, k, None) is not None}
    if getattr(args, "eo", None) is not None:
        d["eo_id"] = args.eo
    return d


def _print_similar(res) -> None:
    if not res:
        _out("  (비슷한 과거 사례 없음)")
        return
    for score, why, r in res:
        eff = f"[{r['effective']}] " if r["effective"] else ""
        _out(f"  T{r['id']:<4} 점수 {score:<2} {r['defect_type']}  {r['car']} {r['part_no']} {r['part_name']}  "
             f"({r['to_stage']} {(r['to_date'] or '')[:7]}) [{r['status']}]")
        _out(f"        근거: {', '.join(why)}")
        if r["cause"]:
            _out(f"        원인: {('[' + r['cause_cat'] + '] ') if r['cause_cat'] else ''}{r['cause'][:90]}")
        if r["countermeasure"]:
            _out(f"        대책: {('[' + r['action_cat'] + '] ') if r['action_cat'] else ''}{r['countermeasure'][:90]}")
        if r["result"] or r["effective"]:
            _out(f"        결과: {eff}{r['result'][:90]}")


def cmd_to(args, cfg, conn):
    from . import to as T
    from .to_knowledge import guide_text, normalize_defect
    T.ensure(conn)
    a = args.to_cmd
    if a == "add":
        tid, prev = T.add(conn, status=args.status or "발생", mail_id=args.mail, **_to_fields(args))
        r = conn.execute("SELECT * FROM to_issues WHERE id=?", (tid,)).fetchone()
        _out("T/O 문제점 등록:\n" + _to_line(conn, r))
        if prev:
            _out(f"\n⚠ 재발입니다. 이전 발생 T{prev['id']} ({prev['to_stage']} {prev['to_date']}) [{prev['status']}]")
            _out(f"   이전 원인: {prev['cause'] or '-'}\n   이전 대책: {prev['countermeasure'] or '-'}"
                 f"\n   이전 결과: {(prev['effective'] + ' ') if prev['effective'] else ''}{prev['result'] or '-'}")
        res = T.similar(conn, r["defect_type"], r["part_name"], r["part_no"], r["resin"], r["location"],
                        r["phenomenon"], r["car"], exclude_id=tid, limit=3)
        if res:
            _out("\n비슷한 과거 사례 (상세: python -m mailhub to similar " + str(tid) + ")")
            _print_similar(res)
        return
    if a == "set":
        r = T.update(conn, args.id, status=args.status, note=args.note, mail_id=args.mail, **_to_fields(args))
        _out(_to_line(conn, r))
        return
    if a == "link":
        for m in args.mails:
            T.link(conn, args.id, m, "수동")
        conn.commit()
        _out(f"T{args.id} 에 메일 {', '.join('#' + str(m) for m in args.mails)} 연결")
        return
    if a == "list":
        rows = T.listing(conn, args.status, car=args.car, q=args.q, defect=args.defect)
        if args.json:
            out = []
            for r in rows:
                d = dict(r)
                d.update(delay=T.delay_of(r), soon=T.soon_of(r), new_mails=T.new_mail_count(conn, r))
                out.append(d)
            _out(json.dumps(out, ensure_ascii=False, indent=1))
            return
        for r in rows:
            _out(_to_line(conn, r))
        _out(f"\n총 {len(rows)}건 / 지연 {sum(1 for r in rows if T.delay_of(r))}건 / 재발 {sum(1 for r in rows if r['recur_of'])}건"
             + ("  (진행 중만. 전체: --status all)" if args.status == "open" else ""))
        return
    if a == "show":
        r = conn.execute("SELECT * FROM to_issues WHERE id=?", (args.id,)).fetchone()
        if not r:
            sys.exit(f"T{args.id} 항목이 없습니다.")
        mails = conn.execute("SELECT m.id, m.sent_at, m.sender_name, m.subject, t.how FROM to_mails t "
                             "JOIN mails m ON m.id=t.mail_id WHERE t.to_id=? ORDER BY m.sent_at", (args.id,)).fetchall()
        logs = conn.execute("SELECT * FROM to_log WHERE to_id=? ORDER BY id", (args.id,)).fetchall()
        prev = conn.execute("SELECT * FROM to_issues WHERE id=?", (r["recur_of"],)).fetchone() if r["recur_of"] else None
        sim = T.similar(conn, r["defect_type"], r["part_name"], r["part_no"], r["resin"], r["location"],
                        r["phenomenon"], r["car"], exclude_id=r["id"], limit=5)
        if args.json:
            _out(json.dumps({"issue": dict(r), "delay": T.delay_of(r), "previous": dict(prev) if prev else None,
                             "mails": [dict(m) for m in mails], "log": [dict(x) for x in logs],
                             "similar": [{"score": sc, "why": why, **dict(x)} for sc, why, x in sim]},
                            ensure_ascii=False, indent=1))
        else:
            _out(_to_line(conn, r))
            for k in T.FIELDS:
                if r[k] and k not in ("phenomenon",):
                    _out(f"  {T.HEADERS.get(k, k):<8}: {r[k]}")
            if prev:
                _out(f"\n[재발 - 이전 T{prev['id']} {prev['to_stage']} {prev['to_date']}]\n  원인: {prev['cause']}\n"
                     f"  대책: {prev['countermeasure']}\n  결과: {prev['effective']} {prev['result']}")
            _out("\n[이력]")
            for x in logs:
                _out(f"  {x['at'][:16].replace('T', ' ')}  {x['from_status'] or '-'} → {x['to_status']}  {x['note'] or ''}")
            _out("\n[관련 메일]")
            seen = r["seen_mail_max"] or 0
            for m in mails:
                _out(f"  {'★' if m['id'] > seen else ' '}#{m['id']:<6} {m['sent_at'][:10]}  {m['sender_name'][:14]:<14} "
                     f"| {m['subject'][:60]}  ({m['how']})")
            _out("\n[비슷한 과거 사례]")
            _print_similar(sim)
        T.mark_seen(conn, args.id)
        return
    if a == "similar":
        if args.id:
            r = conn.execute("SELECT * FROM to_issues WHERE id=?", (args.id,)).fetchone()
            if not r:
                sys.exit(f"T{args.id} 항목이 없습니다.")
            q = dict(defect=r["defect_type"], part_name=r["part_name"], part_no=r["part_no"], resin=r["resin"],
                     location=r["location"], phenomenon=r["phenomenon"], car=r["car"], exclude_id=r["id"])
        else:
            q = dict(defect=args.defect or "", part_name=args.part_name or "", part_no=args.part_no or "",
                     resin=args.resin or "", location=args.location or "", phenomenon=args.phenomenon or "",
                     car=args.car or "")
        res = T.similar(conn, limit=args.limit, **q)
        if args.json:
            _out(json.dumps([{"score": sc, "why": why, **dict(x)} for sc, why, x in res], ensure_ascii=False, indent=1))
        else:
            _print_similar(res)
            d = normalize_defect(q["defect"]) if q["defect"] else ""
            if d:
                _out(f"\n(불량 유형별 일반 대책: python -m mailhub to guide {d})")
        return
    if a == "guide":
        _out(guide_text(args.defect))
        return
    if a == "inbox":
        from datetime import timedelta
        since = args.since or (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
        rows = T.inbox(conn, since, args.limit)
        if args.json:
            _out(json.dumps([_row_dict(r) for r in rows], ensure_ascii=False, indent=1))
            return
        _print_rows(rows, "")
        _out(f"↑ T/O 관련인데 아직 문제점 대장에 연결되지 않은 메일 ({since} 이후)")
        return
    if a == "sync":
        added = T.sync(conn)
        for tid, mid, how in added:
            _out(f"T{tid} ← #{mid} ({how})")
        _out(f"새로 연결된 메일 {len(added)}건")
        return
    if a == "check":
        T.sync(conn)
        rows = T.listing(conn, "open")
        delayed = [(r, T.delay_of(r)) for r in rows if T.delay_of(r)]
        soon = [(r, T.soon_of(r)) for r in rows if T.soon_of(r) and not T.delay_of(r)]
        news = [r for r in rows if T.new_mail_count(conn, r)]
        for r, d in delayed:
            _out(f"! 지연   T{r['id']} {r['defect_type']} {r['car']} {r['part_name'] or r['part_no']} - {d}")
        for r, d in soon:
            _out(f"- 임박   T{r['id']} {r['defect_type']} {r['car']} {r['part_name'] or r['part_no']} - {d}")
        for r in news:
            _out(f"✉ 새 메일 T{r['id']} {r['defect_type']} {r['part_name'] or r['part_no']} - {T.new_mail_count(conn, r)}건 (to show {r['id']})")
        if not (delayed or soon or news):
            _out("T/O: 지연·임박·새 관련 메일 없음")
        n_inbox = len(T.inbox(conn, datetime.now().strftime("%Y-%m-01"), 200))
        if n_inbox:
            _out(f"? 미등록 T/O 메일 {n_inbox}건 (이번 달) → python -m mailhub to inbox")
        if args.notify and (delayed or soon):
            from .followup import notify
            r0, d0 = (delayed or soon)[0]
            notify(f"T/O 대책 지연 {len(delayed)}건 / 임박 {len(soon)}건",
                   f"T{r0['id']} {r0['defect_type']} {r0['part_name'] or r0['part_no']} - {d0}")
        return
    if a == "img":
        from . import eo_images as I
        I.ensure(conn)
        tid = args.id
        if args.paste:
            ok = I.paste(conn, cfg, tid, args.caption or "", kind="to")
            _out("캡처 사진을 저장했습니다." if ok else "이미 저장된 사진과 같아서 건너뛰었습니다.")
        if args.add:
            _out(f"{I.add_files(conn, cfg, tid, args.add, args.caption or '', kind='to')}장 추가")
        if args.remove:
            for n in args.remove:
                _out(f"[{n}] 삭제" if I.remove(conn, tid, n, kind="to") else f"[{n}] 없음")
        if args.collect:
            n = I.collect(conn, cfg, tid, use_outlook=args.outlook, use_powerpoint=not args.no_ppt, log=_out, kind="to")
            _out(f"T{tid}: 사진 {n}장 새로 저장")
        imgs = I.listing(conn, tid, kind="to")
        _out(f"\nT{tid} 사진 {len(imgs)}장")
        for im in imgs:
            _out(f"  [{im['sort']}] {im['caption']}  ({im['source']})")
        return
    if a == "export":
        p = Path(args.path)
        if p.suffix.lower() == ".csv":
            T.export_csv(conn, p, args.status)
        else:
            from . import eo_images as I
            from . import to_excel
            I.ensure(conn)
            if args.collect and not args.no_images:
                total = sum(I.collect(conn, cfg, r["id"], use_outlook=args.outlook, log=_out, kind="to")
                            for r in T.listing(conn, args.status))
                _out(f"사진 {total}장 새로 모음")
            to_excel.build(conn, T.listing(conn, args.status), p, delay_of=T.delay_of, soon_of=T.soon_of,
                           images_of=lambda i: I.listing(conn, i, kind="to"), with_images=not args.no_images)
        _out(f"T/O 문제점 대장 저장: {p.resolve()}")
        if args.open and os.name == "nt":
            os.startfile(str(p.resolve()))  # type: ignore[attr-defined]
        return
    if a == "import":
        ids = T.import_csv(conn, args.path)
        _out(f"{len(ids)}건 가져옴" if ids else "가져온 항목이 없습니다 (열 이름 확인)")
        return


def _mold_line(conn, m) -> str:
    from . import mold as M
    done, total = M.progress(conn, m["id"])
    chk = " ".join(f"{d['section']}{M.SYMBOL.get(d['status'], '?')}" for d in M.docs(conn, m["id"]))
    s = M.ship(conn, m["id"])
    ship = ""
    if s and (s["etd"] or s["plan_date"]):
        ship = f"  ETD {(s['etd'] or s['plan_date'])[5:]}" + (f" ETA {s['eta'][5:]}" if s["eta"] else "") + \
               (" 출항✓" if s["atd"] else "") + (" 도착✓" if s["ata"] else "")
    al = M.alerts(conn, m)
    nm = M.new_mail_count(conn, m)
    name = " ".join(x for x in (m["mold_no"], m["car"], m["part_no"], m["part_name"]) if x)
    return (f"M{m['id']:<4} [{m['status']}] {name[:56]}  → {m['dest'] or '?'}{ship}"
            f"{'  ✉새 메일 ' + str(nm) if nm else ''}\n       서류 {done}/{total}: {chk}"
            + ("".join(f"\n       ⚠ {a}" for a in al)))


def _mold_fields(args) -> dict:
    from . import mold as M
    return {k: getattr(args, k) for k in M.MOLD_FIELDS if getattr(args, k, None) is not None}


def cmd_mold(args, cfg, conn):
    from . import mold as M
    M.ensure(conn)
    a = args.mold_cmd
    if a == "add":
        mid = M.add(conn, status=args.status or "이관준비", mail_id=args.mail, **_mold_fields(args))
        _out("금형 등록:\n" + _mold_line(conn, M.get(conn, mid)))
        return
    if a == "set":
        m = M.update(conn, args.id, status=args.status, note=args.note, mail_id=args.mail, **_mold_fields(args))
        _out(_mold_line(conn, m))
        return
    if a == "doc":
        d = M.set_doc(conn, cfg, args.id, args.section, status=args.status, mail_id=args.mail, attach=args.attach,
                      files=args.file, doc_date=args.date, version=args.version, due=args.due, note=args.note,
                      use_outlook=args.outlook)
        files = [f for f in (d["files"] or "").split("\n") if f]
        _out(f"M{args.id} {d['section']}: {M.SYMBOL.get(d['status'], '')} {d['status']}  접수일 {d['doc_date'] or '-'}"
             f"  파일 {len(files)}개" + ("\n  " + "\n  ".join(files) if files else ""))
        return
    if a == "ship":
        kw = {k: getattr(args, k) for k in M.SHIP_FIELDS if getattr(args, k, None) is not None}
        M.set_ship(conn, args.id, mail_id=args.mail, **kw)
        _out(_mold_line(conn, M.get(conn, args.id)))
        return
    if a == "items":
        if args.file or args.mail:
            n, name = M.import_items(conn, cfg, args.id, args.type, path=args.file, mail_id=args.mail, attach=args.attach or "",
                                     replace=not args.append, use_outlook=args.outlook)
            _out(f"M{args.id} {args.type} 리스트 {n}건 불러옴 ({name}){' - 기존 목록에 추가' if args.append else ' - 기존 목록 교체'}")
        rows = M.items(conn, args.id, args.type)
        for r in rows[:args.limit]:
            _out(f"  {r['part_no']:<14} {r['name'][:28]:<28} {r['spec'][:18]:<18} {r['qty']:>6} {r['unit']:<4} "
                 f"{r['supply']:<3} {r['maker'][:14]}")
        _out(f"M{args.id} {args.type} 리스트 {len(rows)}건" + (f" (앞 {args.limit}건만 표시)" if len(rows) > args.limit else ""))
        return
    if a == "link":
        for x in args.mails:
            M.link(conn, args.id, x, "수동")
        conn.commit()
        _out(f"M{args.id} 에 메일 {', '.join('#' + str(x) for x in args.mails)} 연결")
        return
    if a == "list":
        rows = M.listing(conn, args.status, dest=args.dest, car=args.car, q=args.q)
        if args.json:
            out = []
            for m in rows:
                d = dict(m)
                d.update(docs=[dict(x) for x in M.docs(conn, m["id"])], ship=dict(M.ship(conn, m["id"]) or {}),
                         alerts=M.alerts(conn, m), new_mails=M.new_mail_count(conn, m))
                out.append(d)
            _out(json.dumps(out, ensure_ascii=False, indent=1))
            return
        for m in rows:
            _out(_mold_line(conn, m))
        _out(f"\n총 {len(rows)}벌 / 확인 필요 {sum(1 for m in rows if M.alerts(conn, m))}벌   "
             "(● 확정 ◐ 접수·검토중 ○ 요청 ✕ 미접수 － 해당없음)")
        return
    if a == "show":
        m = M.get(conn, args.id)
        dd = M.docs(conn, m["id"])
        sh = M.ship(conn, m["id"])
        mails = conn.execute("SELECT x.mail_id, x.how, ml.sent_at, ml.sender_name, ml.subject FROM mold_mails x "
                             "JOIN mails ml ON ml.id=x.mail_id WHERE x.mold_id=? ORDER BY ml.sent_at", (m["id"],)).fetchall()
        logs = conn.execute("SELECT * FROM mold_log WHERE mold_id=? ORDER BY id", (m["id"],)).fetchall()
        if args.json:
            _out(json.dumps({"mold": dict(m), "docs": [dict(x) for x in dd], "ship": dict(sh) if sh else None,
                             "items": {lt: len(M.items(conn, m["id"], lt)) for lt in M.LIST_TYPES},
                             "alerts": M.alerts(conn, m), "mails": [dict(x) for x in mails],
                             "log": [dict(x) for x in logs]}, ensure_ascii=False, indent=1))
        else:
            _out(_mold_line(conn, m))
            for k in M.MOLD_FIELDS:
                if m[k] and k != "note":
                    _out(f"  {k:<12}: {m[k]}")
            _out("\n[서류]")
            for d in dd:
                files = [f for f in (d["files"] or "").split("\n") if f]
                _out(f"  {M.SYMBOL.get(d['status'], '?')} {d['section']:<8} {d['status']:<5} {d['doc_date'] or '':<10} "
                     f"{('v' + d['version']) if d['version'] else '':<6} {('기한 ' + d['due']) if d['due'] else ''}"
                     f"{'  #' + str(d['mail_id']) if d['mail_id'] else ''}  {', '.join(Path(f).name for f in files)}")
            if sh:
                _out("\n[선적] " + "  ".join(f"{k}={sh[k]}" for k in M.SHIP_FIELDS if sh[k]))
            _out("\n[리스트] " + "  ".join(f"{lt} {len(M.items(conn, m['id'], lt))}건" for lt in M.LIST_TYPES))
            _out("\n[이력]")
            for x in logs:
                _out(f"  {x['at'][:16].replace('T', ' ')}  {x['what']}  {x['note'] or ''}")
            _out("\n[관련 메일]")
            seen = m["seen_mail_max"] or 0
            for x in mails:
                _out(f"  {'★' if x['mail_id'] > seen else ' '}#{x['mail_id']:<6} {x['sent_at'][:10]}  {x['sender_name'][:14]:<14} "
                     f"| {x['subject'][:60]}  ({x['how']})")
        M.mark_seen(conn, m["id"])
        return
    if a == "inbox":
        from datetime import timedelta
        since = args.since or (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
        rows = M.inbox(conn, since, args.limit)
        if args.json:
            _out(json.dumps([_row_dict(r) for r in rows], ensure_ascii=False, indent=1))
            return
        _print_rows(rows, "")
        _out(f"↑ 금형이관 관련인데 아직 종합장에 연결되지 않은 메일 ({since} 이후)")
        return
    if a == "sync":
        added = M.sync(conn)
        for mid, x, how in added:
            _out(f"M{mid} ← #{x} ({how})")
        _out(f"새로 연결된 메일 {len(added)}건")
        return
    if a == "check":
        M.sync(conn)
        rows = M.listing(conn, "open")
        hits = [(m, M.alerts(conn, m)) for m in rows]
        hits = [(m, al) for m, al in hits if al]
        for m, al in hits:
            _out(f"! M{m['id']} {m['mold_no']} {m['part_name'] or m['part_no']} → {m['dest']}: " + " / ".join(al))
        for m in rows:
            nm = M.new_mail_count(conn, m)
            if nm:
                _out(f"✉ M{m['id']} {m['mold_no']} {m['part_name'] or m['part_no']}: 새 관련 메일 {nm}건 (mold show {m['id']})")
        if not hits:
            _out("금형 이관: 확인 필요 항목 없음")
        n_inbox = len(M.inbox(conn, datetime.now().strftime("%Y-%m-01"), 200))
        if n_inbox:
            _out(f"? 미등록 금형이관 메일 {n_inbox}건 (이번 달) → python -m mailhub mold inbox")
        if args.notify and hits:
            from .followup import notify
            m0, al0 = hits[0]
            notify(f"금형 이관 확인 필요 {len(hits)}벌", f"M{m0['id']} {m0['part_name'] or m0['mold_no']}: {al0[0]}")
        return
    if a == "export":
        from . import mold_excel
        p = mold_excel.build(conn, M, M.listing(conn, args.status, dest=args.dest), Path(args.path))
        _out(f"금형 이관 종합장 저장: {p.resolve()}")
        if args.open and os.name == "nt":
            os.startfile(str(p.resolve()))  # type: ignore[attr-defined]
        return


def cmd_ingest_files(args, cfg, conn):
    from .ingest_files import ingest_folder
    for p in args.paths:
        st = ingest_folder(conn, cfg, p, log=_out)
        _out(f"{p}: 파일 {st['files']} / 신규 {st['added']} / 중복 {st['skipped']} / 오류 {st['errors']}")


def cmd_search(args, cfg, conn):
    query = " ".join(args.query)
    rows = search(conn, query, sender=args.sender, since=args.since, until=args.until, tag=args.tag,
                  has_attach=args.has_attach, attach_name=args.attach, subject_only=args.subject,
                  needs_action=args.todo, part_no=args.part, sort=args.sort, mine=args.mine, to=args.to,
                  limit=args.limit * 10 if args.group else args.limit)
    counts = None
    if args.group:
        rows, counts = _group_threads(rows, args.limit)
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:  # 엑셀 한글 깨짐 방지
            w = csv.writer(f)
            w.writerow(["ID", "일시", "보낸사람", "메일", "제목", "태그", "품번", "첨부", "조치필요", "상태", "메모", "폴더", "요약"])
            for r in rows:
                d = _row_dict(r, query=query)
                w.writerow([d["id"], d["sent_at"], d["sender_name"], d["sender_email"], d["subject"], d["tags"],
                            d["part_nos"], d["attachments"].replace("\n", "; "), d["needs_action"],
                            d["status"], d["note"], d["folder"], d["snippet"]])
        _out(f"{len(rows)}건을 {args.csv} 로 저장했습니다.")
        return
    _print_rows(rows, query, args.json, counts)


def _get(conn, mail_id):
    r = conn.execute("SELECT * FROM mails WHERE id=?", (mail_id,)).fetchone()
    if not r:
        sys.exit(f"#{mail_id} 메일이 없습니다.")
    return r


def cmd_show(args, cfg, conn):
    r = _get(conn, args.id)
    if args.json:
        d = _row_dict(r, with_body=True)
        if args.max_chars:
            d["body"] = d["body"][: args.max_chars]
            d["att_text"] = d["att_text"][: args.max_chars]
        _out(json.dumps(d, ensure_ascii=False, indent=1))
        return
    _out(f"#{r['id']}  {r['subject']}")
    _out(f"보낸사람: {r['sender_name']} <{r['sender_email']}>")
    _out(f"받는사람: {r['to_addr']}")
    if r["cc_addr"]:
        _out(f"참조    : {r['cc_addr']}")
    _out(f"일시    : {r['sent_at'].replace('T', ' ')}   폴더: {r['folder']}")
    if r["attachments"]:
        _out("첨부    : " + ", ".join(r["attachments"].splitlines()))
    _out(f"태그    : {r['tags']}   품번: {r['part_nos']}   조치필요: {'예' if r['needs_action'] else '아니오'}"
         f"   상태: {r['status'] or '-'}   메모: {r['note'] or '-'}")
    _out("-" * 80)
    body = r["body"]
    _out(body if args.full or len(body) <= 3000 else body[:3000] + "\n… (전체 보기: --full)")
    if r["att_text"]:
        _out("\n" + "=" * 30 + " 첨부파일 내용 " + "=" * 30)
        at = r["att_text"]
        _out(at if args.full or len(at) <= 2000 else at[:2000] + "\n… (전체 보기: --full)")


def cmd_open(args, cfg, conn):
    r = _get(conn, args.id)
    if r["entry_id"]:
        from .ingest_outlook import open_in_outlook
        if open_in_outlook(r["entry_id"], r["store_id"]):
            return
    if r["file_path"] and os.path.exists(r["file_path"]):
        os.startfile(r["file_path"])  # type: ignore[attr-defined]  (Windows)
        return
    sys.exit("원본을 열 수 없습니다 (아웃룩에서 삭제되었고 .msg 사본도 없음). 'show' 로 내용은 확인 가능합니다.")


def cmd_todo(args, cfg, conn):
    from datetime import timedelta
    since = "" if args.all else (args.since or (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d"))
    rows = todo(conn, since, args.limit, include_replied=args.include_replied)
    _print_rows(rows, "", args.json)
    if not args.json:
        _out(f"기간: {since or '전체'} 이후 / 내가 이미 회신한 건{'도 포함' if args.include_replied else '은 제외'}"
             "  (옵션: --days 60, --all, --include-replied)")


def cmd_mark(args, cfg, conn):
    for i in args.ids:
        _get(conn, i)
        if args.status is not None:
            conn.execute("UPDATE mails SET status=? WHERE id=?", (args.status, i))
        if args.note is not None:
            conn.execute("UPDATE mails SET note=? WHERE id=?", (args.note, i))
        if args.action is not None:
            conn.execute("UPDATE mails SET needs_action=? WHERE id=?", (int(args.action), i))
    conn.commit()
    _out(f"{len(args.ids)}건 업데이트 완료")


def cmd_retag(args, cfg, conn):
    clf = Classifier(cfg)
    n = 0
    rows = conn.execute("SELECT id, subject, body, attachments, sender_name, sender_email, to_addr, cc_addr, "
                        "folder, att_text FROM mails").fetchall()
    for r in rows:
        c = clf.classify(dict(r))
        conn.execute("UPDATE mails SET tags=?, part_nos=?, needs_action=?, is_mine=?, thread_key=?, quotes_me=? "
                     "WHERE id=?", (c["tags"], c["part_nos"], c["needs_action"], c["is_mine"], c["thread_key"],
                                    c["quotes_me"], r["id"]))
        n += 1
        if n % 5000 == 0:
            _out(f"  ... {n:,}/{len(rows):,}")
    conn.commit()
    _out(f"{n}건 재분류 완료 (config.toml 의 태그/키워드 기준)")


def cmd_stats(args, cfg, conn):
    total = conn.execute("SELECT COUNT(*) FROM mails").fetchone()[0]
    _out(f"전체 메일: {total:,}건   DB: {cfg['archive']['db_path']}")
    rng = conn.execute("SELECT MIN(sent_at), MAX(sent_at) FROM mails WHERE sent_at!=''").fetchone()
    _out(f"기간     : {rng[0] or '-'} ~ {rng[1] or '-'}")
    _out("\n[연도별]")
    for y, c in conn.execute("SELECT substr(sent_at,1,4) y, COUNT(*) FROM mails GROUP BY y ORDER BY y"):
        _out(f"  {y or '날짜없음'}: {c:,}")
    _out("\n[태그별]")
    counts: dict[str, int] = {}
    for (t,) in conn.execute("SELECT tags FROM mails WHERE tags!=''"):
        for x in t.split(","):
            counts[x] = counts.get(x, 0) + 1
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        _out(f"  {k}: {v:,}")
    _out("\n[보낸사람 TOP 15]")
    for n, e, c in conn.execute("SELECT sender_name, sender_email, COUNT(*) c FROM mails "
                                "GROUP BY sender_email ORDER BY c DESC LIMIT 15"):
        _out(f"  {c:>6,}  {n} <{e}>")
    from datetime import timedelta
    since30 = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    n_all = len(todo(conn, "", 10**7))
    n_30 = len(todo(conn, since30, 10**7))
    _out(f"\n미회신 조치필요 메일: 최근 30일 {n_30:,}건 / 전체 {n_all:,}건  (python -m mailhub todo)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mailhub", description="메일 백업 통합 검색 · 분류 · 조치 관리")
    p.add_argument("--config", help="설정 파일 (기본: config.toml)")
    sp = p.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("ingest-outlook", help="아웃룩(서버 사서함 + PST)에서 메일 가져오기 [Windows]")
    s.add_argument("--list", action="store_true", help="아웃룩에 연결된 사서함/PST 목록만 보기")
    s.add_argument("--store", help="이 이름이 포함된 사서함/PST 만 (예: 2024백업)")
    s.add_argument("--folder", help="이 이름이 포함된 폴더 경로만 (예: 받은 편지함)")
    s.add_argument("--pst", nargs="*", help="추가로 열 PST 파일 경로")
    s.add_argument("--since", help="이 날짜 이후 메일만 (YYYY-MM-DD) - 매일 증분 수집용")
    s.add_argument("--save-msg", action="store_true", help="원본을 .msg 로 따로 보관 (서버 삭제 대비)")
    s.add_argument("--save-attachments", action="store_true", help="첨부파일을 data/attachments 에 저장")
    s.set_defaults(func=cmd_ingest_outlook)

    s = sp.add_parser("imap-setup", help="그룹웨어 메일 서버(IMAP) 접속 확인 + 비밀번호 저장")
    s.set_defaults(func=cmd_imap_setup)

    s = sp.add_parser("ingest-imap", help="그룹웨어 메일 서버에서 직접 수집 (읽음 표시 안 바뀜)")
    s.add_argument("--since", help="처음 수집 시작일 YYYY-MM-DD (이후 실행은 새 메일만 자동)")
    s.add_argument("--folder", help="이 이름이 포함된 폴더만")
    s.add_argument("--save-eml", action="store_true", help="원본을 data/eml 에 .eml 로 보관")
    s.add_argument("--full", action="store_true", help="저장된 진행 위치 무시하고 다시 확인")
    s.add_argument("--no-prompt", action="store_true", help="비밀번호를 묻지 않음 (자동 실행용)")
    s.set_defaults(func=cmd_ingest_imap)

    s = sp.add_parser("pop-test", help="POP3 미리보기로 최근 메일 1건 시험 (읽음 처리 여부 확인용)")
    s.add_argument("--full", action="store_true", help="2단계: 본문 전체를 미리보기로 받아 시험")
    s.set_defaults(func=cmd_pop_test)

    s = sp.add_parser("ingest-pop", help="그룹웨어 메일 서버에서 POP3 미리보기로 수집 (IMAP 불가 시)")
    s.add_argument("--since", help="이 날짜 이후 메일만 YYYY-MM-DD")
    s.add_argument("--max", type=int, default=0, help="이번에 본문을 받을 최대 건수 (시험용)")
    s.add_argument("--save-eml", action="store_true", help="원본을 data/eml 에 .eml 로 보관")
    s.add_argument("--no-prompt", action="store_true", help="비밀번호를 묻지 않음 (자동 실행용)")
    s.set_defaults(func=cmd_ingest_pop)

    s = sp.add_parser("index-attachments", help="첨부파일(엑셀/워드/PPT/PDF) 내용을 검색 가능하게 추출")
    s.add_argument("--outlook", action="store_true", help="PST 메일은 아웃룩에서 첨부를 읽음 [Windows]")
    s.add_argument("--since", help="이 날짜 이후 메일만 YYYY-MM-DD")
    s.add_argument("--limit", type=int, default=0, help="이번에 처리할 최대 건수 (시험용)")
    s.set_defaults(func=cmd_index_attachments)

    s = sp.add_parser("req", help="업체 요청 추적 (기한 등록 · 회신 자동 확인 · 알림 · 재요청)")
    rs = s.add_subparsers(dest="req_cmd", required=True)
    r = rs.add_parser("add", help="요청 등록")
    r.add_argument("--to", required=True, help="업체 담당자 메일 주소 (여러 명은 쉼표)")
    r.add_argument("--subject", required=True, help="업체에 보낸(보낼) 메일 제목 그대로")
    r.add_argument("--due", required=True, help="회신 기한: 2026-11-12, 11/12, +3, +3영업일")
    r.add_argument("--cc", help="재요청 메일 참조")
    r.add_argument("--vendor", help="업체명 (표시용)")
    r.add_argument("--type", help="금형수정검토 / 수정진행 / 검사구수정 / 견적 등")
    r.add_argument("--part", help="품번")
    r.add_argument("--mail", type=int, help="관련 메일 번호 (고객사 요청 메일 등)")
    r.add_argument("--note", help="메모")
    r.add_argument("--requested", help="요청한 날 (기본: 오늘)")
    r.add_argument("--eo", type=int, help="연결할 E.O 항목 번호 (E번호의 숫자)")
    r = rs.add_parser("list", help="진행 중인 요청 목록")
    r.add_argument("--all", action="store_true", help="완료·회신받음 포함 전체")
    r.add_argument("--json", action="store_true")
    r = rs.add_parser("check", help="업체 회신 자동 확인 + 기한 지남/임박 표시")
    r.add_argument("--notify", action="store_true", help="윈도우 알림 띄우기")
    r.add_argument("--ask", action="store_true", help="기한 지난 건마다 확인 창 (발송/아웃룩/기한연장/건너뛰기) [Windows]")
    r = rs.add_parser("remind", help="재요청 메일을 아웃룩 작성 창에 띄우기")
    r.add_argument("id", type=int)
    r.add_argument("--preview", action="store_true", help="화면에 내용만 보기")
    r.add_argument("--ask", action="store_true", help="확인 창에서 내용 수정 후 발송 여부 결정 [Windows]")
    r = rs.add_parser("update", help="기한·상태·메모 변경")
    r.add_argument("id", type=int)
    r.add_argument("--due")
    r.add_argument("--status", help="요청 / 회신받음 / 완료 / 취소")
    r.add_argument("--note")
    s.set_defaults(func=cmd_req)

    s = sp.add_parser("eo", help="E.O 리스트 관리 (접수→검토→금형수정→검증→완료, 지연 알림, 엑셀)")
    es = s.add_subparsers(dest="eo_cmd", required=True)

    def eo_field_args(p):
        p.add_argument("--eo-no", dest="eo_no", help="E.O 번호")
        p.add_argument("--car", help="차종")
        p.add_argument("--part-no", dest="part_no", help="품번")
        p.add_argument("--part-name", dest="part_name", help="품명")
        p.add_argument("--desc", dest="change_desc", help="변경 내용")
        p.add_argument("--customer", help="발행처 (고객사/설계팀)")
        p.add_argument("--received", dest="received_at", help="접수일")
        p.add_argument("--mold-vendor", dest="mold_vendor", help="금형업체")
        p.add_argument("--mold-action", dest="mold_action", help="육성 / 가공 / 입자교체 / 신규 / 수정불필요")
        p.add_argument("--cf", dest="cf_impact", help="검사구 영향: 필요 / 불필요 / 검토중")
        p.add_argument("--cf-vendor", dest="cf_vendor", help="검사구업체")
        p.add_argument("--apply", dest="apply_point", help="적용 시점")
        p.add_argument("--cost", help="수정비")
        p.add_argument("--review-due", dest="review_due", help="업체 검토 회신 기한 (11/12, +3, +3영업일)")
        p.add_argument("--mold-due", dest="mold_due", help="금형 수정 완료 목표")
        p.add_argument("--verify-due", dest="verify_due", help="검증(T/O) 목표")
        p.add_argument("--reviewed", dest="reviewed_at", help="검토 완료일")
        p.add_argument("--ordered", dest="ordered_at", help="수정 요청일")
        p.add_argument("--mold-done", dest="mold_done_at", help="수정 완료일")
        p.add_argument("--verified", dest="verified_at", help="검증 완료일")
        p.add_argument("--mail", type=int, help="관련 메일 번호")
        p.add_argument("--note", help="메모 (set 에서는 이력에 날짜와 함께 추가)")
        p.add_argument("--status", help="접수/검토중/수정요청/수정중/수정완료/검증중/완료/수정불필요/보류/취소")

    e = es.add_parser("add", help="E.O 항목 등록 (품번별 1건)")
    eo_field_args(e)
    e = es.add_parser("set", help="상태·기한·내용 변경 (예: eo set 3 --status 수정요청 --mold-due 11/20)")
    e.add_argument("id", type=int)
    eo_field_args(e)
    e = es.add_parser("link", help="E.O 항목에 메일 연결")
    e.add_argument("id", type=int)
    e.add_argument("mails", type=int, nargs="+")
    e = es.add_parser("list", help="E.O 리스트 (기본: 진행 중)")
    e.add_argument("--status", default="open", help="open(기본) / all / 특정 상태")
    e.add_argument("--car", help="차종")
    e.add_argument("--q", help="E.O번호·품번·품명·내용·업체 검색")
    e.add_argument("--json", action="store_true")
    e = es.add_parser("show", help="항목 상세 (이력, 관련 메일)")
    e.add_argument("id", type=int)
    e.add_argument("--json", action="store_true")
    e = es.add_parser("inbox", help="리스트에 아직 없는 E.O 메일 (누락 방지)")
    e.add_argument("--days", type=int, default=60)
    e.add_argument("--since")
    e.add_argument("--limit", type=int, default=50)
    e.add_argument("--json", action="store_true")
    es.add_parser("sync", help="E.O 번호·품번이 나오는 새 메일 자동 연결")
    e = es.add_parser("check", help="지연·임박·새 관련 메일·미등록 메일 확인 (매일 자동)")
    e.add_argument("--notify", action="store_true")
    e = es.add_parser("export", help="엑셀(.xlsx) 또는 CSV 로 저장")
    e.add_argument("path", nargs="?", default="data/EO_LIST.xlsx")
    e.add_argument("--status", default="all")
    e.add_argument("--open", action="store_true", help="저장 후 열기")
    e.add_argument("--no-images", action="store_true", help="그림 없이 (파일 크기 작게)")
    e.add_argument("--collect", action="store_true", help="내보내기 전에 관련 메일·PPT 그림 모으기")
    e.add_argument("--outlook", action="store_true", help="--collect 시 PST(아웃룩) 메일 첨부도 읽기 [Windows]")
    e = es.add_parser("img", help="참고 그림: 관련 메일·PPT에서 모으기 / 캡처 붙여넣기 / 목록·삭제")
    e.add_argument("id", type=int, nargs="?", help="E번호의 숫자")
    e.add_argument("--collect", action="store_true", help="연결된 메일의 그림·PPT·엑셀 그림 모으기")
    e.add_argument("--all", action="store_true", help="진행 중인 모든 E.O 에 대해 모으기")
    e.add_argument("--outlook", action="store_true", help="PST(아웃룩) 메일의 첨부도 읽기 [Windows]")
    e.add_argument("--no-ppt", action="store_true", help="파워포인트로 슬라이드를 그림으로 만들지 않음")
    e.add_argument("--paste", action="store_true", help="클립보드 캡처(Win+Shift+S)를 붙여넣기 [Windows]")
    e.add_argument("--add", nargs="+", help="그림/PPT 파일 추가")
    e.add_argument("--caption", help="그림 설명")
    e.add_argument("--remove", type=int, nargs="+", help="뺄 그림 번호")
    e = es.add_parser("import", help="CSV(UTF-8) E.O 리스트 가져오기 (열 이름은 export 와 동일)")
    e.add_argument("path")
    s.set_defaults(func=cmd_eo)

    s = sp.add_parser("to", help="T/O 문제점·개선 이력 (재발 감지, 유사 사례, 불량별 대책 가이드, 엑셀)")
    ts = s.add_subparsers(dest="to_cmd", required=True)

    def to_field_args(p):
        p.add_argument("--car", help="차종")
        p.add_argument("--part-no", dest="part_no", help="품번")
        p.add_argument("--part-name", dest="part_name", help="품명")
        p.add_argument("--stage", dest="to_stage", help="T/O 차수 (T0, T1, T2 / 시사출 / P1 / M ...)")
        p.add_argument("--date", dest="to_date", help="T/O 일자")
        p.add_argument("--mold-vendor", dest="mold_vendor", help="금형업체")
        p.add_argument("--molder", help="성형처")
        p.add_argument("--resin", help="재질 (PP-TD20, ABS ...)")
        p.add_argument("--defect", dest="defect_type", help="불량 유형 (싱크, 웰드, 변형, 미성형, 버, 갭 ... 자동 표준화)")
        p.add_argument("--location", help="발생 부위")
        p.add_argument("--phenomenon", help="현상")
        p.add_argument("--cause-cat", dest="cause_cat", help="원인 구분: 성형조건/금형/제품설계/재질/설비/검사구/조립·공정/미확인")
        p.add_argument("--cause", help="원인")
        p.add_argument("--action-cat", dest="action_cat", help="대책 구분: 조건변경/금형수정/설계변경(E.O)/재질변경/설비·지그/검사기준/작업표준")
        p.add_argument("--countermeasure", "--cm", dest="countermeasure", help="대책")
        p.add_argument("--owner", help="담당 (업체/부서)")
        p.add_argument("--due", help="대책 적용 목표일")
        p.add_argument("--verify-due", dest="verify_due", help="효과 확인 목표일 (다음 T/O 등)")
        p.add_argument("--result", help="효과 확인 결과")
        p.add_argument("--effective", help="대책 효과: 유효 / 부분 / 무효")
        p.add_argument("--applied", dest="applied_at", help="대책 적용일")
        p.add_argument("--closed", dest="closed_at", help="종결일")
        p.add_argument("--eo", type=int, help="연결할 E.O 항목 번호")
        p.add_argument("--mail", type=int, help="관련 메일 번호")
        p.add_argument("--note", help="메모")
        p.add_argument("--status", help="발생/원인분석/대책수립/대책적용/효과확인/종결/보류")

    t = ts.add_parser("add", help="문제점 등록 (재발이면 이전 대책 표시)")
    to_field_args(t)
    t = ts.add_parser("set", help="원인·대책·상태 갱신")
    t.add_argument("id", type=int)
    to_field_args(t)
    t = ts.add_parser("link", help="메일 연결")
    t.add_argument("id", type=int)
    t.add_argument("mails", type=int, nargs="+")
    t = ts.add_parser("list", help="문제점 목록 (기본: 진행 중)")
    t.add_argument("--status", default="open")
    t.add_argument("--car")
    t.add_argument("--defect", help="불량 유형")
    t.add_argument("--q", help="품번·품명·현상·원인·대책·부위·업체 검색")
    t.add_argument("--json", action="store_true")
    t = ts.add_parser("show", help="상세 (재발 이력, 관련 메일, 비슷한 사례)")
    t.add_argument("id", type=int)
    t.add_argument("--json", action="store_true")
    t = ts.add_parser("similar", help="비슷한 과거 사례 (번호 또는 조건)")
    t.add_argument("id", type=int, nargs="?")
    t.add_argument("--defect")
    t.add_argument("--part-name", dest="part_name")
    t.add_argument("--part-no", dest="part_no")
    t.add_argument("--resin")
    t.add_argument("--location")
    t.add_argument("--phenomenon")
    t.add_argument("--car")
    t.add_argument("--limit", type=int, default=10)
    t.add_argument("--json", action="store_true")
    t = ts.add_parser("guide", help="불량 유형별 원인·대책 가이드")
    t.add_argument("defect")
    t = ts.add_parser("inbox", help="대장에 아직 없는 T/O 메일")
    t.add_argument("--days", type=int, default=60)
    t.add_argument("--since")
    t.add_argument("--limit", type=int, default=50)
    t.add_argument("--json", action="store_true")
    ts.add_parser("sync", help="관련 메일 자동 연결")
    t = ts.add_parser("check", help="지연·임박·새 관련 메일 (매일 자동)")
    t.add_argument("--notify", action="store_true")
    t = ts.add_parser("img", help="사진: 관련 메일·보고서에서 모으기 / 캡처 붙여넣기 / 삭제")
    t.add_argument("id", type=int)
    t.add_argument("--collect", action="store_true")
    t.add_argument("--outlook", action="store_true")
    t.add_argument("--no-ppt", action="store_true")
    t.add_argument("--paste", action="store_true")
    t.add_argument("--add", nargs="+")
    t.add_argument("--caption")
    t.add_argument("--remove", type=int, nargs="+")
    t = ts.add_parser("export", help="문제점 대장 엑셀(.xlsx) 또는 CSV")
    t.add_argument("path", nargs="?", default="data/TO_ISSUES.xlsx")
    t.add_argument("--status", default="all")
    t.add_argument("--open", action="store_true")
    t.add_argument("--no-images", action="store_true")
    t.add_argument("--collect", action="store_true")
    t.add_argument("--outlook", action="store_true")
    t = ts.add_parser("import", help="CSV(UTF-8) 가져오기 (열 이름은 export 와 동일)")
    t.add_argument("path")
    s.set_defaults(func=cmd_to)

    s = sp.add_parser("mold", help="금형 이관 종합장 (사양서·성형조건·부품·선적·KD·원소재 체크, 엑셀)")
    ms = s.add_subparsers(dest="mold_cmd", required=True)

    def mold_field_args(p):
        p.add_argument("--mold-no", dest="mold_no", help="금형번호")
        p.add_argument("--car", help="차종")
        p.add_argument("--part-no", dest="part_no", help="품번")
        p.add_argument("--part-name", dest="part_name", help="품명")
        p.add_argument("--mold-vendor", dest="mold_vendor", help="금형업체")
        p.add_argument("--dest", help="이관 법인 (멕시코, 인도, 체코 ...)")
        p.add_argument("--local-molder", dest="local_molder", help="현지 성형처")
        p.add_argument("--cavity", help="캐비티 (1x1, 2CAV ...)")
        p.add_argument("--tonnage", help="성형기 톤수")
        p.add_argument("--size", dest="mold_size", help="금형 크기 (WxLxH)")
        p.add_argument("--weight", dest="mold_weight", help="금형 중량")
        p.add_argument("--hot-runner", dest="hot_runner", help="핫런너 (사양/메이커/점수)")
        p.add_argument("--resin", help="재질")
        p.add_argument("--target", dest="target_date", help="이관 목표일")
        p.add_argument("--status", help="제작중/T/O/이관준비/선적대기/선적/현지도착/현지T/O/이관완료/보류")
        p.add_argument("--note", help="메모")
        p.add_argument("--mail", type=int, help="관련 메일 번호")

    x = ms.add_parser("add", help="금형 등록 (기본 6개 서류 항목 자동 생성)")
    mold_field_args(x)
    x = ms.add_parser("set", help="금형 정보·상태 변경")
    x.add_argument("id", type=int)
    mold_field_args(x)
    x = ms.add_parser("doc", help="서류 항목 갱신 (메일 첨부를 그대로 보관)")
    x.add_argument("id", type=int)
    x.add_argument("section", help="금형사양서 / 성형조건표 / 부품리스트 / 선적일정 / KD리스트 / 원소재리스트 (다른 이름도 가능)")
    x.add_argument("--status", help="미접수 / 요청 / 접수 / 검토중 / 확정 / 해당없음 (메일·파일을 붙이면 자동 '접수')")
    x.add_argument("--mail", type=int, help="받은 메일 번호")
    x.add_argument("--attach", help="저장할 첨부파일 이름 일부 (빈 문자열이면 첨부 전부)")
    x.add_argument("--file", nargs="+", help="PC 의 파일을 직접 보관")
    x.add_argument("--date", help="접수일 (기본: 메일 날짜)")
    x.add_argument("--version", help="버전/Rev")
    x.add_argument("--due", help="회신(제출) 기한")
    x.add_argument("--note", help="메모")
    x.add_argument("--outlook", action="store_true", help="PST(아웃룩) 메일 첨부 읽기 [Windows]")
    x = ms.add_parser("ship", help="선적 일정 (ETD/ETA/출항/도착/BL ...)")
    x.add_argument("id", type=int)
    x.add_argument("--plan", dest="plan_date", help="선적 계획일")
    x.add_argument("--etd")
    x.add_argument("--eta")
    x.add_argument("--atd", help="실제 출항일 → 상태 '선적'")
    x.add_argument("--ata", help="실제 도착일 → 상태 '현지도착'")
    x.add_argument("--bl", dest="bl_no", help="B/L 번호")
    x.add_argument("--vessel", help="선박명")
    x.add_argument("--from", dest="port_from", help="출발항")
    x.add_argument("--to", dest="port_to", help="도착항")
    x.add_argument("--forwarder", help="포워더")
    x.add_argument("--container", help="컨테이너 번호")
    x.add_argument("--note")
    x.add_argument("--mail", type=int)
    x = ms.add_parser("items", help="부품 / KD / 원소재 리스트 (엑셀·CSV 또는 메일 첨부에서 불러오기)")
    x.add_argument("id", type=int)
    x.add_argument("--type", required=True, choices=["부품", "KD", "원소재"])
    x.add_argument("--file", help="엑셀(.xlsx)/CSV 파일")
    x.add_argument("--mail", type=int, help="첨부 엑셀이 있는 메일 번호")
    x.add_argument("--attach", help="첨부파일 이름 일부 (예: KD)")
    x.add_argument("--append", action="store_true", help="기존 목록을 지우지 않고 추가")
    x.add_argument("--outlook", action="store_true")
    x.add_argument("--limit", type=int, default=30, help="화면에 보여줄 줄 수")
    x = ms.add_parser("link", help="메일 연결")
    x.add_argument("id", type=int)
    x.add_argument("mails", type=int, nargs="+")
    x = ms.add_parser("list", help="금형별 현황 (서류 체크·선적)")
    x.add_argument("--status", default="open", help="open(기본: 이관완료 제외) / all / 특정 상태")
    x.add_argument("--dest", help="이관 법인")
    x.add_argument("--car")
    x.add_argument("--q", help="금형번호·품번·품명·업체 검색")
    x.add_argument("--json", action="store_true")
    x = ms.add_parser("show", help="금형 상세 (서류·선적·리스트·이력·관련 메일)")
    x.add_argument("id", type=int)
    x.add_argument("--json", action="store_true")
    x = ms.add_parser("inbox", help="종합장에 아직 없는 금형이관 메일")
    x.add_argument("--days", type=int, default=60)
    x.add_argument("--since")
    x.add_argument("--limit", type=int, default=50)
    x.add_argument("--json", action="store_true")
    ms.add_parser("sync", help="관련 메일 자동 연결")
    x = ms.add_parser("check", help="선적 전 미완료 서류·기한·도착 확인 (매일 자동)")
    x.add_argument("--notify", action="store_true")
    x = ms.add_parser("export", help="금형 이관 종합장 엑셀")
    x.add_argument("path", nargs="?", default="data/MOLD_TRANSFER.xlsx")
    x.add_argument("--status", default="all")
    x.add_argument("--dest", help="이관 법인만")
    x.add_argument("--open", action="store_true")
    s.set_defaults(func=cmd_mold)

    s = sp.add_parser("ingest-files", help=".eml/.msg 파일 폴더 가져오기")
    s.add_argument("paths", nargs="+")
    s.set_defaults(func=cmd_ingest_files)

    s = sp.add_parser("search", help="메일 검색", aliases=["s"])
    s.add_argument("query", nargs="*", help='검색어 (공백=AND, "정확한 구문", -제외어)')
    s.add_argument("--from", dest="sender", help="보낸사람 이름/주소")
    s.add_argument("--to", help="받는사람/참조 이름·주소·도메인 (예: --to abc-mold.co.kr)")
    s.add_argument("--mine", action="store_true", help="내가 보낸 메일만 (회신 말투 참고용)")
    s.add_argument("--since", help="시작일 YYYY-MM-DD")
    s.add_argument("--until", help="종료일 YYYY-MM-DD")
    s.add_argument("--tag", help="태그 (E.O, T/O, 금형수정, 검사구, 품질문제, 금형이관, 일정, 설계반영)")
    s.add_argument("--part", help="품번")
    s.add_argument("--attach", help="첨부파일명 포함")
    s.add_argument("--has-attach", action="store_true", help="첨부 있는 메일만")
    s.add_argument("--subject", action="store_true", help="제목에서만 검색")
    s.add_argument("--todo", action="store_true", help="조치필요 메일만")
    s.add_argument("--group", action="store_true", help="같은 건(RE:/FW:)은 최신 1통만 표시")
    s.add_argument("--sort", choices=["date", "relevance"], default="date")
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--json", action="store_true", help="JSON 출력 (Claude Code 연동용)")
    s.add_argument("--csv", help="결과를 CSV(엑셀) 파일로 저장")
    s.set_defaults(func=cmd_search)

    s = sp.add_parser("show", help="메일 내용 보기")
    s.add_argument("id", type=int)
    s.add_argument("--full", action="store_true")
    s.add_argument("--json", action="store_true")
    s.add_argument("--max-chars", type=int, default=0, help="JSON 본문 최대 글자수")
    s.set_defaults(func=cmd_show)

    s = sp.add_parser("open", help="아웃룩에서 원본 메일 열기 [Windows]")
    s.add_argument("id", type=int)
    s.set_defaults(func=cmd_open)

    s = sp.add_parser("todo", help="검토/회신 필요 메일 목록 (미완료·미회신)")
    s.add_argument("--days", type=int, default=30, help="최근 며칠 (기본 30)")
    s.add_argument("--since", help="YYYY-MM-DD (--days 대신)")
    s.add_argument("--all", action="store_true", help="기간 제한 없이 전체")
    s.add_argument("--include-replied", action="store_true", help="내가 이미 회신한 건도 표시")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_todo)

    s = sp.add_parser("mark", help="처리 상태/메모 기록")
    s.add_argument("ids", type=int, nargs="+")
    s.add_argument("--status", help="예: 진행중, 업체검토중, 설계반영, 완료, 보류, 제외")
    s.add_argument("--note", help="메모 (예: 금형업체 회신 대기, 11/15 수정완료 예정)")
    s.add_argument("--action", choices=["0", "1"], help="조치필요 여부 수동 변경")
    s.set_defaults(func=cmd_mark)

    s = sp.add_parser("retag", help="config.toml 변경 후 전체 재분류")
    s.set_defaults(func=cmd_retag)

    s = sp.add_parser("stats", help="보관 현황")
    s.set_defaults(func=cmd_stats)
    return p


def main(argv=None):
    try:
        if sys.stdout.isatty():
            sys.stdout.reconfigure(errors="replace")  # 콘솔 인코딩 문제로 멈추지 않게
        else:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Claude Code 등 다른 프로그램이 읽을 때 한글 유지
    except Exception:
        pass
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    conn = db.connect(cfg["archive"]["db_path"])
    try:
        args.func(args, cfg, conn)
    finally:
        conn.close()
