"""mailhub 명령어.  사용법: python -m mailhub <명령> --help"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime

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
    d["snippet"] = snippet(r["body"], query)
    if with_body:
        d.update({"to_addr": r["to_addr"], "cc_addr": r["cc_addr"], "body": r["body"],
                  "file_path": r["file_path"], "source": r["source"]})
    return d


def _print_rows(rows, query, as_json=False):
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
        _out(f"{flag}#{r['id']:<6} {r['sent_at'][:16].replace('T', ' ')}  {r['sender_name'][:16]:<16} "
             f"| {r['subject'][:70]}{att}{st}")
        meta = "  ".join(x for x in [f"태그:{r['tags']}" if r["tags"] else "",
                                     f"품번:{r['part_nos']}" if r["part_nos"] else ""] if x)
        if meta:
            _out(f"         {meta}")
        _out(f"         {snippet(r['body'], query)}")
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


def cmd_ingest_files(args, cfg, conn):
    from .ingest_files import ingest_folder
    for p in args.paths:
        st = ingest_folder(conn, cfg, p, log=_out)
        _out(f"{p}: 파일 {st['files']} / 신규 {st['added']} / 중복 {st['skipped']} / 오류 {st['errors']}")


def cmd_search(args, cfg, conn):
    query = " ".join(args.query)
    rows = search(conn, query, sender=args.sender, since=args.since, until=args.until, tag=args.tag,
                  has_attach=args.has_attach, attach_name=args.attach, subject_only=args.subject,
                  needs_action=args.todo, part_no=args.part, sort=args.sort, limit=args.limit)
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
    _print_rows(rows, query, args.json)


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
                        "folder FROM mails").fetchall()
    for r in rows:
        c = clf.classify(dict(r))
        conn.execute("UPDATE mails SET tags=?, part_nos=?, needs_action=?, is_mine=?, thread_key=? WHERE id=?",
                     (c["tags"], c["part_nos"], c["needs_action"], c["is_mine"], c["thread_key"], r["id"]))
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

    s = sp.add_parser("ingest-files", help=".eml/.msg 파일 폴더 가져오기")
    s.add_argument("paths", nargs="+")
    s.set_defaults(func=cmd_ingest_files)

    s = sp.add_parser("search", help="메일 검색", aliases=["s"])
    s.add_argument("query", nargs="*", help='검색어 (공백=AND, "정확한 구문", -제외어)')
    s.add_argument("--from", dest="sender", help="보낸사람 이름/주소")
    s.add_argument("--since", help="시작일 YYYY-MM-DD")
    s.add_argument("--until", help="종료일 YYYY-MM-DD")
    s.add_argument("--tag", help="태그 (E.O, T/O, 금형수정, 검사구, 품질문제, 금형이관, 일정, 설계반영)")
    s.add_argument("--part", help="품번")
    s.add_argument("--attach", help="첨부파일명 포함")
    s.add_argument("--has-attach", action="store_true", help="첨부 있는 메일만")
    s.add_argument("--subject", action="store_true", help="제목에서만 검색")
    s.add_argument("--todo", action="store_true", help="조치필요 메일만")
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
        sys.stdout.reconfigure(errors="replace")  # 콘솔 인코딩 문제로 멈추지 않게
    except Exception:
        pass
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    conn = db.connect(cfg["archive"]["db_path"])
    try:
        args.func(args, cfg, conn)
    finally:
        conn.close()
