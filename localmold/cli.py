"""현지 금형 육성 관리 - 명령줄.

    python -m localmold app                         화면 (http://127.0.0.1:8766)
    python -m localmold import <엑셀/CSV> --car ..  외주처 엑셀 · 통합 리스트 · 금형 리스트 가져오기 (자동 구분)
    python -m localmold cars | molds | list | show | set | top | snapshot | phone-import | export | from-mailhub
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import core as LM
from .config import load_config, mailhub_db


def _out(s: str = ""):
    print(s)


def run(args, cfg, conn):
    a = args.cmd
    if a == "import":
        p = Path(args.path)
        if not p.is_file():
            raise SystemExit(f"파일이 없습니다: {p}")
        vendor = (args.vendor or LM.vendor_from_name(p.name) or "").upper() or None
        res = LM.import_any(conn, cfg, args.car, p.name, p.read_bytes(), vendor)
        _out(f"[{res.pop('kind')}] {p.name} → {args.car}{' / ' + vendor if vendor else ''}\n  " +
             ", ".join(f"{k} {v}" for k, v in res.items()))
        return
    if a == "cars":
        rows = LM.cars(conn)
        if args.json:
            _out(json.dumps(rows, ensure_ascii=False, indent=1))
            return
        for x in rows:
            _out(f"{x['car']}: 금형 {x['molds']}벌 (문제 있는 금형 {x['molds_with_issue']}) · 문제점 {x['issues']}건 · 진행 {x['open']} · "
                 f"중요도 상 진행 {x['hi_open']} · 수정 지연 {x['late']} · 검증 T/O 필요 {x['wait']} · 종결률 {x['close_rate']}%")
            for v in x["vendors"]:
                _out(f"  - {v['vendor']}: 금형 {v['molds']} · 문제점 {v['issues']} · 진행 {v['open']} · 중요도 상 {v['hi_open']}")
        return
    if a == "molds":
        rows = LM.molds(conn, args.car, args.vendor)
        if args.json:
            _out(json.dumps(rows, ensure_ascii=False, indent=1))
            return
        for m in rows:
            cnt = " ".join(f"{c}{m['counts'][c]}" for c in LM.CATS if m["counts"][c])
            _out(f"{m['vendor']:<13} {m['part_no']:<16} {m['part_name'][:30]:<30} {m['status']:<5} 문제 {m['total']} (진행 {m['open']}) {cnt}")
        return
    if a == "list":
        rows = LM.issues(conn, args.car, args.vendor, status=args.status, q=args.q, importance=args.importance,
                         category=args.category)
        if args.json:
            _out(json.dumps([dict(r) | {"delay": LM.delay_of(r), "wait": LM.wait_check(r)} for r in rows], ensure_ascii=False, indent=1))
            return
        for r in rows[:args.limit]:
            flag = " ".join(x for x in (LM.delay_of(r), LM.wait_check(r), f"재발←L{r['recur_of']}" if r["recur_of"] else "") if x)
            _out(f"L{r['id']:<4} {r['vendor']:<12} {r['part_no']:<15} [{r['category']}] {r['importance'] or '-'} {r['status']:<4} "
                 f"{' '.join(r['problem'].split())[:40]}{'  ' + flag if flag else ''}")
        _out(f"({len(rows)}건)")
        return
    if a == "show":
        r = LM.get_issue(conn, args.id)
        if not r:
            raise SystemExit(f"L{args.id} 이 없습니다")
        ph = LM.photos(conn, args.id)
        if args.json:
            _out(json.dumps({"issue": dict(r), "photos": [dict(p) for p in ph], "log": [dict(x) for x in LM.log(conn, args.id)]},
                            ensure_ascii=False, indent=1))
            return
        _out(f"L{r['id']} {r['car']} / {r['vendor']} / {r['part_no']} {r['part_name']}\n  [{r['category']}] {r['problem']}\n"
             f"  상태 {r['status']} (양식: {r['src_status'] or '-'}) · 중요도 {r['importance'] or '-'} · 발생 {r['occ_date'] or '-'} · "
             f"수정 목표 {r['due'] or '-'}\n  대책: {r['countermeasure'] or '-'}\n  진행: {r['progress'] or '-'} · 수정 후 T/O "
             f"{r['to_after'] or '-'} {r['to_result']}\n  사진: 수정 전 {sum(1 for p in ph if p['grp'] == 'before')} · "
             f"수정 후 {sum(1 for p in ph if p['grp'] == 'after')}")
        for x in LM.log(conn, args.id):
            _out(f"  {x['at'][:16]} {x['from_status'] or '등록'} → {x['to_status']} {x['note']}")
        return
    if a == "set":
        kw = {k: getattr(args, k) for k in LM.ISSUE_FIELDS if getattr(args, k, None) is not None}
        r = LM.update_issue(conn, args.id, note=args.note, **kw)
        _out(f"L{r['id']} → {r['status']} · {r['problem'][:50]}")
        return
    if a == "snapshot":
        rows = LM.snapshot(conn, args.car)
        text = json.dumps(rows, ensure_ascii=False)
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(text, encoding="utf-8")
            _out(f"{args.out}: {len(rows)}개 (외주처별) · 금형 {sum(len(r['molds']) for r in rows)} · 진행 중 문제점 {sum(len(r['issues']) for r in rows)}")
        else:
            _out(text)
        return
    if a == "phone-import":
        ups, base = LM.phone_uploads(Path(args.source))
        if not ups:
            _out("들여올 폰 사진이 없습니다")
            return
        res = LM.phone_import(conn, cfg, ups, base)
        if args.json:
            _out(json.dumps(res, ensure_ascii=False, indent=1))
            return
        for r in res:
            _out(f"{r['id']}: {r['status']}{' (새 문제점)' if r.get('new') else ''} → L{r['l']}" + (f"  {r['error']}" if r.get("error") else ""))
        return
    if a == "export":
        paths = LM.export(conn, cfg, args.car, args.vendor, Path(args.dir) if args.dir else None)
        for p in paths:
            _out(str(p))
        if args.open:
            from mailhub.winopen import open_file
            open_file(str(paths[0] if args.vendor else paths[0].parent))
        return
    if a == "top":
        rows = LM.priority(conn, args.car, args.vendor, limit=args.limit)
        if args.json:
            _out(json.dumps(rows, ensure_ascii=False, indent=1))
            return
        for i, r in enumerate(rows, 1):
            _out(f"{i:>2}. L{r['id']:<4} {r['car']} / {r['vendor']:<12} {r['part_no']:<15} [{r['category']}] {r['status']:<4} "
                 f"{' '.join(r['problem'].split())[:36]}  ← {' · '.join(r['why']) or '진행 중'}")
        _out(f"(진행 중 문제점 중 상위 {len(rows)}건 · 점수 = 지연 > 중요도 상 > 검증 T/O · NG > 재발 > 임박)")
        return
    if a == "from-mailhub":
        src = Path(args.db) if args.db else mailhub_db(cfg)
        res = LM.from_mailhub(conn, cfg, src)
        _out(f"업무 비서({src})에서 옮김: " + ", ".join(f"{k} {v}" for k, v in res.items()))
        return
    if a == "app":
        from .webapp import serve
        conn.close()
        serve(args.config, port=args.port or cfg["port"], open_window=not args.no_window, log=_out)
        return


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="localmold", description="현지 금형 육성 관리 (차종 → 외주처 → 금형 → 문제점)")
    ap.add_argument("--config", help="설정 파일 (기본 localmold.toml, 없어도 됨)")
    ls = ap.add_subparsers(dest="cmd", required=True)
    x = ls.add_parser("app", help="화면 열기 (이 PC 에서만)")
    x.add_argument("--port", type=int)
    x.add_argument("--no-window", action="store_true", help="창을 열지 않고 엔진만")
    x = ls.add_parser("import", help="엑셀/CSV 가져오기: 외주처 엑셀(갑지+을지+사진) · 통합 문제점 리스트 · 금형 리스트 (자동 구분)")
    x.add_argument("path")
    x.add_argument("--car", required=True, help="차종 (예: \"BC4t CUV\")")
    x.add_argument("--vendor", help="외주처 (파일 이름 _CAR_외주처_REV 에서 자동)")
    x = ls.add_parser("cars", help="차종별 요약")
    x.add_argument("--json", action="store_true")
    x = ls.add_parser("molds", help="갑지 (금형별 유형 건수 · STATUS)")
    x.add_argument("--car")
    x.add_argument("--vendor")
    x.add_argument("--json", action="store_true")
    x = ls.add_parser("list", help="문제점 목록")
    x.add_argument("--car")
    x.add_argument("--vendor")
    x.add_argument("--status", default="open", help="open / all / wait(검증 T/O 필요) / 단계 이름")
    x.add_argument("--importance", help="상/중/하")
    x.add_argument("--category", help="습합/취출/WELD LINE/SINK/미성형/GAS/기타")
    x.add_argument("--q", help="품번·품명·문제점·대책 검색")
    x.add_argument("--limit", type=int, default=200)
    x.add_argument("--json", action="store_true")
    x = ls.add_parser("show", help="문제점 상세 (L번호)")
    x.add_argument("id", type=int)
    x.add_argument("--json", action="store_true")
    x = ls.add_parser("set", help="문제점 갱신")
    x.add_argument("id", type=int)
    for f, h in (("status", "발생/원인분석/대책수립/대책적용/효과확인/종결/보류"), ("problem", "문제점"),
                 ("category", "유형"), ("countermeasure", "개선 대책"), ("owner", "담당"), ("importance", "상/중/하"),
                 ("mold_fix", "금형수정 대상 O/X"), ("occ_date", "발생일"), ("due", "수정 목표일"),
                 ("to_after", "수정 후 T/O O/X"), ("to_result", "T/O 결과 OK/NG"), ("progress", "진행 메모")):
        x.add_argument("--" + f.replace("_", "-"), dest=f, help=h)
    x.add_argument("--note", help="메모 (이력에 남음)")
    x = ls.add_parser("top", help="작업 우선순위 (출장 · 현지 점검 때 먼저 볼 문제점과 이유)")
    x.add_argument("--car")
    x.add_argument("--vendor")
    x.add_argument("--limit", type=int, default=20)
    x.add_argument("--json", action="store_true")
    x = ls.add_parser("snapshot", help="폰 사진 접수 페이지에 보낼 목록 (외주처별 금형 · 진행 중 문제점, JSON)")
    x.add_argument("--car")
    x.add_argument("--out", help="저장할 파일 (없으면 화면에 출력)")
    x = ls.add_parser("phone-import", help="폰에서 올린 사진 들여오기 (내려받은 폴더: uploads/*.json + assets/사진, 또는 manifest JSON)")
    x.add_argument("source")
    x.add_argument("--json", action="store_true")
    x = ls.add_parser("export", help="엑셀: --vendor 면 외주처 양식(갑지+을지+사진), 없으면 통합 리스트 + 외주처별 전부")
    x.add_argument("--car", required=True)
    x.add_argument("--vendor")
    x.add_argument("--dir", help="저장 폴더 (기본 data/localmold/export/차종)")
    x.add_argument("--open", action="store_true")
    x = ls.add_parser("from-mailhub", help="업무 비서 안의 [현지 금형]에 넣었던 데이터 옮기기 (처음 한 번)")
    x.add_argument("--db", help="업무 비서 DB (기본: config.toml 의 archive.db_path)")
    return ap


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace") if not sys.stdout.isatty() else sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    conn = LM.connect(cfg["db_path"])
    try:
        run(args, cfg, conn)
    except ValueError as e:
        raise SystemExit(f"[확인] {e}") from None
    finally:
        try:
            conn.close()
        except Exception:
            pass
