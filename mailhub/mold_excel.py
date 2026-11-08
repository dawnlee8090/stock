"""금형 이관 종합장 엑셀.

시트: 이관 종합(금형별 서류 체크 매트릭스) / 선적 일정 / 부품 리스트 / KD 리스트 / 원소재 리스트 / M<번호> 상세
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .eo_excel import C_BAND, C_HEAD, FONT, PT_PER_LINE, _d, _link, _thin, disp_len, lines_needed

DOC_FILL = {"확정": "C6E0B4", "검토중": "FFE699", "접수": "FFF2CC", "요청": "FCE4D6", "미접수": "F8CBAD", "해당없음": "EDEDED"}
MOLD_FILL = {"제작중": "EDEDED", "T/O": "FFF2CC", "이관준비": "FFE699", "선적대기": "DDEBF7", "선적": "BDD7EE",
             "현지도착": "C6E0B4", "현지T/O": "A9D08E", "이관완료": "D9D9D9", "보류": "F8CBAD"}


def _head_row(ws, row: int, heads: list[str], color: str = C_HEAD, height: float = 30):
    border = _thin()
    for ci, h in enumerate(heads, 1):
        c = ws.cell(row, ci, h)
        c.font = Font(name=FONT, size=9, bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=color)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = border
    ws.row_dimensions[row].height = height


def _title(ws, title: str, sub: str = ""):
    ws.sheet_view.showGridLines = False
    ws.cell(1, 1, title).font = Font(name=FONT, size=16, bold=True, color=C_HEAD)
    ws.row_dimensions[1].height = 28
    if sub:
        ws.cell(2, 1, sub).font = Font(name=FONT, size=10, bold=True, color="404040")
        ws.row_dimensions[2].height = 20


def _fit(ws, data: list[list], first_row: int, widths_minmax: list[tuple[int, int]], wrap_cols: set[int]):
    """내용 길이에 맞춰 열 너비·행 높이."""
    widths = []
    for ci, (mn, mx) in enumerate(widths_minmax):
        longest = max([disp_len(line) for row in data for line in str(row[ci] if ci < len(row) and row[ci] is not None else "").split("\n")] + [0])
        w = min(mx, max(mn, longest + 2))
        widths.append(w)
        ws.column_dimensions[get_column_letter(ci + 1)].width = w
    for i, row in enumerate(data):
        need = max([lines_needed(str(row[ci] or ""), widths[ci]) for ci in wrap_cols if ci < len(row)] + [1])
        ws.row_dimensions[first_row + i].height = max(20.0, min(need * PT_PER_LINE + 6, 200))
    return widths


def _style_body(ws, r0: int, nrows: int, ncols: int, center_cols: set[int]):
    border = _thin()
    for i in range(nrows):
        band = PatternFill("solid", fgColor=C_BAND) if i % 2 else None
        for ci in range(1, ncols + 1):
            c = ws.cell(r0 + i, ci)
            c.border = border
            if c.font is None or not c.font.bold:
                c.font = Font(name=FONT, size=9)
            c.alignment = Alignment(vertical="center", wrap_text=True, horizontal="center" if ci in center_cols else "left")
            if band and (c.fill is None or c.fill.fgColor.rgb in (None, "00000000")):
                c.fill = band


def _print_setup(ws, landscape=True):
    ws.page_setup.orientation = "landscape" if landscape else "portrait"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True


def build(conn, M, molds: list, path: Path, today: date | None = None) -> Path:
    today = today or date.today()
    molds = sorted(molds, key=lambda m: m["id"])
    wb = Workbook()
    ws = wb.active
    ws.title = "이관 종합"
    secs = list(M.SECTIONS)
    extra = sorted({d["section"] for m in molds for d in M.docs(conn, m["id"])} - set(secs))
    secs += extra

    n_done = sum(1 for m in molds if m["status"] == "이관완료")
    n_alert = sum(1 for m in molds if M.alerts(conn, m, today))
    _title(ws, "금형 이관 종합장", f"금형 {len(molds)}벌   ·   진행 중 {len(molds) - n_done}벌   ·   확인 필요 {n_alert}벌   ·   "
                                   f"이관완료 {n_done}벌        (● 확정  ◐ 접수·검토중  ○ 요청  ✕ 미접수  － 해당없음)")
    ws.cell(1, 8, f"작성 {datetime.now():%Y-%m-%d %H:%M}").font = Font(name=FONT, size=9, color="808080")

    heads = ["No", "금형번호", "차종", "품번 / 품명", "금형업체", "이관 법인\n/ 현지 성형처", "상태"] + secs + \
            ["서류\n완료", "ETD", "ETA", "출항", "도착", "확인 필요", "비고"]
    _head_row(ws, 4, heads)
    rows = []
    for m in molds:
        dd = {d["section"]: d for d in M.docs(conn, m["id"])}
        s = M.ship(conn, m["id"])
        done, total = M.progress(conn, m["id"])
        row = [f"M{m['id']}", m["mold_no"], m["car"], "\n".join(x for x in (m["part_no"], m["part_name"]) if x),
               m["mold_vendor"], "\n".join(x for x in (m["dest"], m["local_molder"]) if x), m["status"]]
        for sec in secs:
            d = dd.get(sec)
            if not d:
                row.append("")
                continue
            dt = (d["doc_date"] or "")[5:].replace("-", "/")
            row.append(f"{M.SYMBOL.get(d['status'], '')} {dt}".strip())
        row += [f"{done}/{total}", s["etd"] if s else "", s["eta"] if s else "", s["atd"] if s else "", s["ata"] if s else "",
                "\n".join(M.alerts(conn, m, today)), m["note"]]
        rows.append((m, dd, row))
    for i, (m, dd, row) in enumerate(rows):
        r = 5 + i
        for ci, val in enumerate(row, 1):
            c = ws.cell(r, ci)
            h = heads[ci - 1]
            if h in ("ETD", "ETA", "출항", "도착"):
                dv = _d(val or "")
                c.value = dv
                if dv:
                    c.number_format = "m/d"
            else:
                c.value = val
        _style_body(ws, r, 1, len(heads), center_cols=set(range(7, 7 + len(secs) + 6)) | {1})
        c = ws.cell(r, 1)
        _link(c, f"M{m['id']}")
        c.font = Font(name=FONT, size=9, bold=True, color="0563C1", underline="single")
        st = ws.cell(r, 7)
        st.fill = PatternFill("solid", fgColor=MOLD_FILL.get(m["status"], "FFFFFF"))
        st.font = Font(name=FONT, size=9, bold=True)
        for k, sec in enumerate(secs):
            c = ws.cell(r, 8 + k)
            d = dd.get(sec)
            if d:
                c.fill = PatternFill("solid", fgColor=DOC_FILL.get(d["status"], "FFFFFF"))
                c.font = Font(name=FONT, size=9, bold=d["status"] == "미접수")
                files = [f for f in (d["files"] or "").split("\n") if f]
                if files and Path(files[0]).exists():
                    c.hyperlink = files[0]         # 클릭하면 파일 열기
                    c.font = Font(name=FONT, size=9, underline="single", color="1F3864")
        al = ws.cell(r, len(heads) - 1)
        if al.value:
            al.font = Font(name=FONT, size=9, bold=True, color="C00000")
        dn = ws.cell(r, 8 + len(secs))
        done_, total_ = map(int, dn.value.split("/"))
        dn.font = Font(name=FONT, size=9, bold=True, color="548235" if done_ == total_ else "C55A11")
    mm = [(5, 6), (10, 16), (8, 14), (16, 26), (9, 14), (10, 16), (8, 9)] + [(10, 10)] * len(secs) + \
         [(6, 6), (7, 7), (7, 7), (7, 7), (7, 7), (16, 30), (12, 30)]
    _fit(ws, [r for _, _, r in rows], 5, mm, wrap_cols={3, 5, len(heads) - 2, len(heads) - 1})
    ws.freeze_panes = "E5"
    if rows:
        ws.auto_filter.ref = f"A4:{get_column_letter(len(heads))}{4 + len(rows)}"
    ws.print_title_rows = "4:4"
    _print_setup(ws)

    _ship_sheet(wb, conn, M, molds, today)
    for lt, title in (("부품", "부품 리스트"), ("KD", "KD 리스트"), ("원소재", "원소재 리스트")):
        _items_sheet(wb, M.items(conn, list_type=lt), title, show_supply=(lt != "부품"))
    for m, dd, _row in rows:
        _detail(wb, conn, M, m, today)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def _ship_sheet(wb, conn, M, molds, today):
    ws = wb.create_sheet("선적 일정")
    data = []
    for m in molds:
        s = M.ship(conn, m["id"])
        if not s:
            continue
        if s["ata"]:
            dd = "도착"
        elif s["atd"]:
            dd = "운송 중"
        elif s["etd"]:
            n = (date.fromisoformat(s["etd"]) - today).days
            dd = f"D-{n}" if n > 0 else ("오늘" if n == 0 else f"{-n}일 지남")
        else:
            dd = "미정"
        data.append((s["etd"] or "9999", m, s, dd))
    data.sort(key=lambda x: (x[0], x[1]["id"]))
    _title(ws, "선적 일정", f"선적 예정 {sum(1 for x in data if x[3].startswith('D-') or x[3] == '오늘')}벌 · 운송 중 "
                         f"{sum(1 for x in data if x[3] == '운송 중')}벌 · 도착 {sum(1 for x in data if x[3] == '도착')}벌")
    heads = ["No", "금형번호", "품명", "이관 법인", "선적 계획", "ETD", "ETA", "출항", "도착", "D-day", "B/L No", "선박",
             "출발항 → 도착항", "포워더", "컨테이너", "비고"]
    _head_row(ws, 4, heads)
    rows = []
    for _, m, s, dd in data:
        rows.append([f"M{m['id']}", m["mold_no"], m["part_name"] or m["part_no"], m["dest"], s["plan_date"], s["etd"], s["eta"],
                     s["atd"], s["ata"], dd, s["bl_no"], s["vessel"],
                     " → ".join(x for x in (s["port_from"], s["port_to"]) if x), s["forwarder"], s["container"], s["note"]])
    for i, row in enumerate(rows):
        r = 5 + i
        for ci, val in enumerate(row, 1):
            c = ws.cell(r, ci)
            if 5 <= ci <= 9:
                dv = _d(val or "")
                c.value = dv
                if dv:
                    c.number_format = "m/d"
            else:
                c.value = val
        _style_body(ws, r, 1, len(heads), center_cols={1, 5, 6, 7, 8, 9, 10})
        _link(ws.cell(r, 1), f"M{row[0][1:]}")
        dc = ws.cell(r, 10)
        if "지남" in str(dc.value):
            dc.font = Font(name=FONT, size=9, bold=True, color="C00000")
        elif str(dc.value).startswith("D-") and int(str(dc.value)[2:]) <= 14:
            dc.font = Font(name=FONT, size=9, bold=True, color="C55A11")
    _fit(ws, rows, 5, [(5, 6), (10, 16), (12, 26), (8, 14)] + [(7, 7)] * 5 + [(7, 9), (10, 18), (10, 18), (12, 22),
                                                                                (8, 14), (8, 14), (10, 30)], wrap_cols={2, 15})
    ws.freeze_panes = "C5"
    _print_setup(ws)


def _items_sheet(wb, rows, title, show_supply):
    ws = wb.create_sheet(title)
    n_kd = sum(1 for r in rows if r["supply"] == "KD")
    n_lp = sum(1 for r in rows if r["supply"] == "LP")
    _title(ws, title, f"{len(rows)}건" + (f"   ·   KD {n_kd}   ·   LP {n_lp}" if show_supply else ""))
    heads = ["금형", "금형번호", "금형 품명", "품번", "품명", "규격 / 재질", "수량", "단위"] + (["KD/LP"] if show_supply else []) + \
            ["메이커", "비고", "출처"]
    _head_row(ws, 4, heads)
    data = []
    for r in rows:
        row = [f"M{r['mold_id']}", r["mold_no"], r["mold_part"], r["part_no"], r["name"], r["spec"], r["qty"], r["unit"]]
        if show_supply:
            row.append(r["supply"])
        row += [r["maker"], r["note"], r["source"]]
        data.append(row)
    for i, row in enumerate(data):
        for ci, val in enumerate(row, 1):
            ws.cell(5 + i, ci, val)
        _style_body(ws, 5 + i, 1, len(heads), center_cols={1, 7, 8, 9} if show_supply else {1, 7, 8})
        if show_supply:
            c = ws.cell(5 + i, 9)
            if c.value == "KD":
                c.fill = PatternFill("solid", fgColor="DDEBF7")
            elif c.value == "LP":
                c.fill = PatternFill("solid", fgColor="E2EFDA")
    mm = [(5, 6), (8, 16), (10, 22), (12, 18), (14, 30), (10, 24), (6, 10), (5, 6)] + ([(6, 6)] if show_supply else []) + \
         [(8, 16), (8, 24), (10, 26)]
    _fit(ws, data, 5, mm, wrap_cols={4, 5})
    ws.freeze_panes = "D5"
    if data:
        ws.auto_filter.ref = f"A4:{get_column_letter(len(heads))}{4 + len(data)}"
    _print_setup(ws)


def _detail(wb, conn, M, m, today):
    s = wb.create_sheet(f"M{m['id']}")
    s.sheet_view.showGridLines = False
    border = _thin()
    for col, w in zip("ABCDEFG", (14, 30, 14, 30, 12, 36, 20)):
        s.column_dimensions[col].width = w
    s.cell(1, 1, f"M{m['id']}  {m['mold_no']}  {m['part_name'] or m['part_no']}").font = Font(name=FONT, size=14, bold=True, color=C_HEAD)
    back = s.cell(1, 6, "← 이관 종합")
    _link(back, "이관 종합")
    back.font = Font(name=FONT, size=9, color="0563C1", underline="single")
    st = s.cell(2, 1, m["status"])
    st.fill = PatternFill("solid", fgColor=MOLD_FILL.get(m["status"], "FFFFFF"))
    st.font = Font(name=FONT, size=10, bold=True)
    st.alignment = Alignment(horizontal="center")
    al = M.alerts(conn, m, today)
    if al:
        s.cell(2, 2, "⚠ " + " / ".join(al)).font = Font(name=FONT, size=10, bold=True, color="C00000")

    def kv(row, pairs):
        for k, (key, val) in enumerate(pairs):
            ck, cv = s.cell(row, 1 + k * 2, key), s.cell(row, 2 + k * 2, val)
            for c, is_key in ((ck, True), (cv, False)):
                c.border = border
                c.alignment = Alignment(vertical="center", wrap_text=True)
                c.font = Font(name=FONT, size=9 if is_key else 10, bold=is_key, color="404040" if is_key else "000000")
                if is_key:
                    c.fill = PatternFill("solid", fgColor="EEF2F8")

    row = 4
    s.cell(row - 0, 1)
    info = [("금형번호", m["mold_no"]), ("차종", m["car"]), ("품번", m["part_no"]), ("품명", m["part_name"]),
            ("금형업체", m["mold_vendor"]), ("이관 법인", m["dest"]), ("현지 성형처", m["local_molder"]), ("캐비티", m["cavity"]),
            ("성형기 톤수", m["tonnage"]), ("금형 크기", m["mold_size"]), ("금형 중량", m["mold_weight"]), ("핫런너", m["hot_runner"]),
            ("재질", m["resin"]), ("이관 목표", m["target_date"])]
    for i in range(0, len(info), 2):
        kv(row, info[i:i + 2])
        row += 1
    if m["note"]:
        kv(row, [("비고", m["note"])])
        s.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
        s.row_dimensions[row].height = lines_needed(m["note"], 74) * 15 + 6
        row += 1

    row += 1
    s.cell(row, 1, "이관 서류 체크").font = Font(name=FONT, size=11, bold=True, color=C_HEAD)
    row += 1
    for ci, h in enumerate(["항목", "상태", "접수일", "버전", "기한", "파일", "비고"], 1):
        c = s.cell(row, ci, h)
        c.font = Font(name=FONT, size=9, bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=C_HEAD)
        c.alignment = Alignment(horizontal="center")
        c.border = border
    row += 1
    for d in M.docs(conn, m["id"]):
        files = [f for f in (d["files"] or "").split("\n") if f]
        vals = [d["section"], f"{M.SYMBOL.get(d['status'], '')} {d['status']}", (d["doc_date"] or "")[5:].replace("-", "/"),
                d["version"], (d["due"] or "")[5:].replace("-", "/"), "\n".join(Path(f).name for f in files),
                (d["note"] or "") + (f"  (#{d['mail_id']})" if d["mail_id"] else "")]
        for ci, val in enumerate(vals, 1):
            c = s.cell(row, ci, val)
            c.border = border
            c.font = Font(name=FONT, size=9)
            c.alignment = Alignment(vertical="center", wrap_text=True, horizontal="center" if ci in (2, 3, 4, 5) else "left")
        s.cell(row, 2).fill = PatternFill("solid", fgColor=DOC_FILL.get(d["status"], "FFFFFF"))
        if files and Path(files[0]).exists():
            s.cell(row, 6).hyperlink = files[0]
            s.cell(row, 6).font = Font(name=FONT, size=9, underline="single", color="0563C1")
        s.row_dimensions[row].height = max(lines_needed(vals[5], 36), lines_needed(vals[6], 20), 1) * 15 + 6
        row += 1

    sh = M.ship(conn, m["id"])
    if sh:
        row += 1
        s.cell(row, 1, "선적").font = Font(name=FONT, size=11, bold=True, color=C_HEAD)
        row += 1
        dt = lambda x: (x or "")[5:].replace("-", "/")  # noqa: E731
        for pairs in ([("선적 계획", dt(sh["plan_date"])), ("B/L No", sh["bl_no"])],
                      [("ETD", dt(sh["etd"])), ("ETA", dt(sh["eta"]))],
                      [("실제 출항", dt(sh["atd"])), ("실제 도착", dt(sh["ata"]))],
                      [("선박", sh["vessel"]), ("항구", " → ".join(x for x in (sh["port_from"], sh["port_to"]) if x))],
                      [("포워더", sh["forwarder"]), ("컨테이너", sh["container"])]):
            kv(row, pairs)
            row += 1

    cnt = {lt: len(M.items(conn, m["id"], lt)) for lt in M.LIST_TYPES}
    row += 1
    s.cell(row, 1, "리스트").font = Font(name=FONT, size=11, bold=True, color=C_HEAD)
    s.cell(row, 2, f"부품 {cnt['부품']}건 · KD {cnt['KD']}건 · 원소재 {cnt['원소재']}건 (각 리스트 시트 참고)").font = Font(name=FONT, size=9)
    row += 2
    s.cell(row, 1, "진행 이력").font = Font(name=FONT, size=11, bold=True, color=C_HEAD)
    row += 1
    for x in conn.execute("SELECT * FROM mold_log WHERE mold_id=? ORDER BY id", (m["id"],)):
        s.cell(row, 1, x["at"][5:16].replace("-", "/").replace("T", " ")).font = Font(name=FONT, size=9, color="606060")
        s.cell(row, 2, x["what"]).font = Font(name=FONT, size=9, bold=True)
        nc = s.cell(row, 3, (x["note"] or "") + (f"  (#{x['mail_id']})" if x["mail_id"] else ""))
        s.merge_cells(start_row=row, start_column=3, end_row=row, end_column=6)
        nc.font = Font(name=FONT, size=9)
        nc.alignment = Alignment(wrap_text=True, vertical="top")
        s.row_dimensions[row].height = max(lines_needed(x["what"], 30), lines_needed(nc.value, 90)) * 14 + 4
        row += 1
    _print_setup(s, landscape=False)
