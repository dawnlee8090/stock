"""T/O 문제점 관리대장 엑셀.

시트: T/O 문제점(목록, 사진 썸네일) / 불량 통계(유형·원인·업체별, 재발) / T<번호> 상세(현상·원인·대책·이력·사진)
"""
from __future__ import annotations

import math
import tempfile
from datetime import date, datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .eo_excel import C_BAND, C_HEAD, FONT, PT_PER_LINE, _d, _link, _thin, _thumb, disp_len, lines_needed

C_GROUP = {"대상": "2F5597", "문제점": "C00000", "원인·대책": "548235", "진행": "BF8F00", "결과": "7F7F7F"}
STATUS_FILL = {"발생": "F8CBAD", "원인분석": "FFE699", "대책수립": "FFF2CC", "대책적용": "DDEBF7", "효과확인": "C6E0B4",
               "종결": "E7E6E6", "보류": "EDEDED"}

COLUMNS = [  # (키, 머리글, 그룹, 최소폭, 최대폭, 종류)
    ("id", "No", "대상", 5, 6, "link"),
    ("car", "차종", "대상", 8, 14, "text"),
    ("part", "품번 / 품명", "대상", 16, 26, "text"),
    ("to", "T/O", "대상", 7, 9, "text"),
    ("defect_type", "불량 유형", "문제점", 9, 11, "defect"),
    ("location", "부위", "문제점", 8, 16, "text"),
    ("phenomenon", "현상", "문제점", 20, 36, "text"),
    ("image", "사진", "문제점", 25, 25, "image"),
    ("cause", "원인", "원인·대책", 18, 34, "text"),
    ("countermeasure", "대책", "원인·대책", 20, 38, "text"),
    ("owner", "담당", "진행", 8, 14, "text"),
    ("status", "상태", "진행", 9, 9, "status"),
    ("alert", "지연 / 임박", "진행", 10, 14, "alert"),
    ("due", "대책\n목표", "진행", 7, 7, "date"),
    ("applied_at", "대책\n적용", "진행", 7, 7, "date"),
    ("verify_due", "확인\n목표", "진행", 7, 7, "date"),
    ("result", "효과 확인", "결과", 14, 30, "text"),
    ("effective", "효과", "결과", 6, 6, "effect"),
    ("refs", "재발 / E.O", "결과", 8, 12, "text"),
]
EFFECT_COLOR = {"유효": "548235", "부분": "BF8F00", "무효": "C00000"}


def build(conn, rows: list, path: Path, delay_of, soon_of, images_of, with_images: bool = True,
          title: str = "T/O 문제점 관리대장") -> Path:
    rows = sorted(rows, key=lambda r: r["id"])
    wb = Workbook()
    ws = wb.active
    ws.title = "TO 문제점"
    ws.sheet_view.showGridLines = False
    tmp = tempfile.TemporaryDirectory(prefix="mailhub_toxl_")
    tmpdir = Path(tmp.name)
    border = _thin()
    ncol = len(COLUMNS)

    open_rows = [r for r in rows if r["status"] != "종결"]
    n_delay = sum(1 for r in open_rows if delay_of(r))
    n_recur = sum(1 for r in rows if r["recur_of"])
    ws.cell(1, 1, title).font = Font(name=FONT, size=16, bold=True, color=C_HEAD)
    ws.cell(1, ncol - 3, f"작성 {datetime.now():%Y-%m-%d %H:%M}").font = Font(name=FONT, size=9, color="808080")
    ws.cell(2, 1, f"전체 {len(rows)}건   ·   진행 중 {len(open_rows)}건   ·   지연 {n_delay}건   ·   재발 {n_recur}건   ·   "
                  f"종결 {len(rows) - len(open_rows)}건").font = Font(name=FONT, size=10, bold=True,
                                                                       color="C00000" if n_delay else "404040")
    ws.row_dimensions[1].height = 28
    ws.row_dimensions[2].height = 20

    start = 1
    while start <= ncol:
        g = COLUMNS[start - 1][2]
        end = start
        while end < ncol and COLUMNS[end][2] == g:
            end += 1
        ws.merge_cells(start_row=3, start_column=start, end_row=3, end_column=end)
        c = ws.cell(3, start, g)
        c.font = Font(name=FONT, size=10, bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=C_GROUP[g])
        c.alignment = Alignment(horizontal="center", vertical="center")
        for ci in range(start, end + 1):
            ws.cell(3, ci).border = border
        start = end + 1
    for ci, (key, head, *_r) in enumerate(COLUMNS, 1):
        c = ws.cell(4, ci, head)
        c.font = Font(name=FONT, size=9, bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=C_HEAD)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = border
    ws.row_dimensions[3].height = 18
    ws.row_dimensions[4].height = 32

    data = []
    for r in rows:
        alert = delay_of(r)
        alert = f"⚠ {alert}" if alert else (f"⏰ {soon_of(r)}" if soon_of(r) else "")
        imgs = images_of(r["id"]) if with_images else []
        cause = (f"[{r['cause_cat']}] " if r["cause_cat"] else "") + (r["cause"] or "")
        cm = (f"[{r['action_cat']}] " if r["action_cat"] else "") + (r["countermeasure"] or "")
        refs = " ".join(x for x in (f"재발←T{r['recur_of']}" if r["recur_of"] else "",
                                    f"E{r['eo_id']}" if r["eo_id"] else "") if x)
        v = {k: (r[k] if k in r.keys() else "") for k, *_ in COLUMNS}
        v.update(part="\n".join(x for x in (r["part_no"], r["part_name"]) if x),
                 to=" ".join(x for x in (r["to_stage"], (r["to_date"] or "")[5:].replace("-", "/")) if x),
                 cause=cause, countermeasure=cm, alert=alert, refs=refs,
                 image=(f"외 {len(imgs) - 1}장" if len(imgs) > 1 else ""), _imgs=imgs)
        data.append(v)

    widths = {}
    for ci, (key, head, g, mn, mx, kind) in enumerate(COLUMNS, 1):
        if kind in ("date", "image", "link", "status", "effect"):
            w = mx
        else:
            longest = max([disp_len(line) for v in data for line in str(v.get(key) or "").split("\n")] + [0])
            w = min(mx, max(mn, longest + 2))
        widths[key] = w
        ws.column_dimensions[get_column_letter(ci)].width = w

    today = date.today()
    for i, v in enumerate(data):
        row = 5 + i
        band = PatternFill("solid", fgColor=C_BAND) if i % 2 else None
        for ci, (key, head, g, mn, mx, kind) in enumerate(COLUMNS, 1):
            val = v.get(key)
            c = ws.cell(row, ci)
            c.border = border
            c.font = Font(name=FONT, size=9)
            c.alignment = Alignment(vertical="center", wrap_text=True,
                                    horizontal="center" if kind in ("date", "status", "link", "defect", "effect") else "left")
            if band:
                c.fill = band
            if kind == "date":
                dv = _d(val or "")
                if dv:
                    c.value = dv
                    c.number_format = "m/d"
                    if key in ("due", "verify_due") and dv < today and v["alert"].startswith("⚠") and \
                            ((key == "due") == v["alert"][2:4].startswith("대책")):
                        c.font = Font(name=FONT, size=9, bold=True, color="C00000")
            elif kind == "status":
                c.value = val
                c.fill = PatternFill("solid", fgColor=STATUS_FILL.get(val, "FFFFFF"))
                c.font = Font(name=FONT, size=9, bold=True)
            elif kind == "defect":
                c.value = val
                c.font = Font(name=FONT, size=9, bold=True, color="C00000")
            elif kind == "effect":
                c.value = val
                c.font = Font(name=FONT, size=9, bold=True, color=EFFECT_COLOR.get(val, "000000"))
            elif kind == "alert":
                c.value = val
                if val:
                    c.font = Font(name=FONT, size=9, bold=True, color="C00000" if val.startswith("⚠") else "C55A11")
            elif kind == "link":
                c.value = f"T{val}"
                _link(c, f"T{val}")
                c.font = Font(name=FONT, size=9, bold=True, color="0563C1", underline="single")
            elif kind == "image":
                c.value = val
                c.alignment = Alignment(vertical="bottom", horizontal="right")
                c.font = Font(name=FONT, size=8, color="808080")
            else:
                c.value = val or ""
                if key == "refs" and val and "재발" in val:
                    c.font = Font(name=FONT, size=9, bold=True, color="C00000")
        need = max(lines_needed(str(v.get(k) or ""), widths[k]) for k, *_r, kind in COLUMNS if kind == "text")
        h = max(20.0, need * PT_PER_LINE + 6)
        if v["_imgs"]:
            ci = [k for k, *_ in COLUMNS].index("image") + 1
            try:
                x = _thumb(v["_imgs"][0]["path"], 150, 100, tmpdir, f"t{v['id']}")
                ws.add_image(x, f"{get_column_letter(ci)}{row}")
                h = max(h, x.height * 0.75 + (18 if v["image"] else 8))
            except Exception:
                pass
        ws.row_dimensions[row].height = min(h, 300)

    last = 4 + len(data)
    ws.freeze_panes = "E5"
    if data:
        ws.auto_filter.ref = f"A4:{get_column_letter(ncol)}{last}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = "3:4"

    _stats_sheet(wb, rows, delay_of)
    for v, r in zip(data, rows):
        _detail_sheet(wb, conn, r, v, delay_of, tmpdir, with_images)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    tmp.cleanup()
    return path


def _stats_sheet(wb, rows, delay_of):
    s = wb.create_sheet("불량 통계")
    s.sheet_view.showGridLines = False
    border = _thin()
    s.cell(1, 1, "T/O 불량 통계").font = Font(name=FONT, size=14, bold=True, color=C_HEAD)
    s.cell(2, 1, f"작성 {datetime.now():%Y-%m-%d %H:%M}   (재발 = 같은 품번·같은 불량이 다시 발생)").font = Font(
        name=FONT, size=9, color="808080")

    def table(r0, c0, title, head, items):
        s.cell(r0, c0, title).font = Font(name=FONT, size=11, bold=True, color=C_HEAD)
        for j, h in enumerate(head):
            c = s.cell(r0 + 1, c0 + j, h)
            c.font = Font(name=FONT, size=10, bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor=C_HEAD)
            c.alignment = Alignment(horizontal="center")
            c.border = border
        for i, it in enumerate(items, 2):
            for j, val in enumerate(it):
                c = s.cell(r0 + i, c0 + j, val)
                c.font = Font(name=FONT, size=10)
                c.border = border
                c.alignment = Alignment(horizontal="center" if j else "left")
        return r0 + len(items) + 3

    def group(key):
        g: dict[str, list] = {}
        for r in rows:
            g.setdefault(r[key] or "(미기재)", []).append(r)
        return sorted(g.items(), key=lambda kv: -len(kv[1]))

    def counts(items):
        return [(k, len(v), sum(1 for r in v if r["status"] != "종결"), sum(1 for r in v if r["recur_of"]),
                 sum(1 for r in v if delay_of(r))) for k, v in items]

    head = ["구분", "전체", "진행 중", "재발", "지연"]
    n1 = table(4, 1, "불량 유형별", head, counts(group("defect_type")))
    n2 = table(4, 7, "원인 구분별", head, counts(group("cause_cat")))
    table(max(n1, n2), 1, "금형업체별", head, counts(group("mold_vendor")))
    table(max(n1, n2), 7, "성형처별", head, counts(group("molder")))
    for col, w in zip("ABCDEFGHIJKL", (16, 8, 8, 8, 8, 3, 16, 8, 8, 8, 8, 3)):
        s.column_dimensions[col].width = w
    s.page_setup.orientation = "landscape"
    s.page_setup.fitToWidth = 1
    s.page_setup.fitToHeight = 0
    s.sheet_properties.pageSetUpPr.fitToPage = True


def _detail_sheet(wb, conn, r, v, delay_of, tmpdir: Path, with_images: bool):
    s = wb.create_sheet(f"T{r['id']}")
    s.sheet_view.showGridLines = False
    border = _thin()
    for col, w in zip("ABCD", (14, 40, 14, 40)):
        s.column_dimensions[col].width = w
    back = s.cell(1, 4, "← TO 문제점")
    _link(back, "TO 문제점")
    back.font = Font(name=FONT, size=9, color="0563C1", underline="single")
    back.alignment = Alignment(horizontal="right")
    name = " ".join(x for x in (r["car"], r["part_no"], r["part_name"]) if x)
    s.cell(1, 1, f"T{r['id']}  {name}  -  {r['defect_type']}").font = Font(name=FONT, size=14, bold=True, color=C_HEAD)
    st = s.cell(2, 1, r["status"])
    st.fill = PatternFill("solid", fgColor=STATUS_FILL.get(r["status"], "FFFFFF"))
    st.font = Font(name=FONT, size=10, bold=True)
    st.alignment = Alignment(horizontal="center")
    flags = [x for x in (f"⚠ {delay_of(r)}" if delay_of(r) else "", f"재발 (이전: T{r['recur_of']})" if r["recur_of"] else "") if x]
    if flags:
        s.cell(2, 2, "   ".join(flags)).font = Font(name=FONT, size=10, bold=True, color="C00000")

    def put(row, k1, v1, k2=None, v2=None, merge=False):
        cells = [(1, k1, True), (2, v1, False)] + ([] if merge else [(3, k2 or "", True), (4, v2 or "", False)])
        for col, val, key in cells:
            c = s.cell(row, col, val)
            c.border = border
            c.alignment = Alignment(vertical="center", wrap_text=True)
            c.font = Font(name=FONT, size=9 if key else 10, bold=key, color="404040" if key else "000000")
            if key:
                c.fill = PatternFill("solid", fgColor="EEF2F8")
        if merge:
            s.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
            s.row_dimensions[row].height = lines_needed(str(v1 or ""), 94) * 15 + 6
        else:
            s.row_dimensions[row].height = max(lines_needed(str(v1 or ""), 40), lines_needed(str(v2 or ""), 40)) * 15 + 6

    def dt(x):
        return x[5:10].replace("-", "/") if x else ""

    row = 4
    put(row, "T/O", v["to"], "금형업체 / 성형처", " / ".join(x for x in (r["mold_vendor"], r["molder"]) if x)); row += 1
    put(row, "재질", r["resin"], "부위", r["location"]); row += 1
    put(row, "현상", r["phenomenon"], merge=True); row += 1
    put(row, "원인", v["cause"], merge=True); row += 1
    put(row, "대책", v["countermeasure"], merge=True); row += 1
    put(row, "담당", r["owner"], "E.O 연결", f"E{r['eo_id']}" if r["eo_id"] else ""); row += 1
    put(row, "대책 목표", dt(r["due"]), "대책 적용", dt(r["applied_at"])); row += 1
    put(row, "확인 목표", dt(r["verify_due"]), "종결", dt(r["closed_at"])); row += 1
    put(row, "효과 확인", (f"[{r['effective']}] " if r["effective"] else "") + (r["result"] or ""), merge=True); row += 1
    if r["note"]:
        put(row, "메모", r["note"], merge=True); row += 1

    if r["recur_of"]:
        p = conn.execute("SELECT * FROM to_issues WHERE id=?", (r["recur_of"],)).fetchone()
        if p:
            row += 1
            s.cell(row, 1, f"이전 발생 (T{p['id']}, {p['to_stage']} {dt(p['to_date'])})").font = Font(
                name=FONT, size=11, bold=True, color="C00000")
            row += 1
            put(row, "이전 원인", p["cause"], merge=True); row += 1
            put(row, "이전 대책", p["countermeasure"], merge=True); row += 1
            put(row, "이전 결과", (f"[{p['effective']}] " if p["effective"] else "") + (p["result"] or ""), merge=True); row += 1

    logs = conn.execute("SELECT * FROM to_log WHERE to_id=? ORDER BY id", (r["id"],)).fetchall()
    row += 1
    s.cell(row, 1, "진행 이력").font = Font(name=FONT, size=11, bold=True, color=C_HEAD)
    row += 1
    for x in logs:
        s.cell(row, 1, x["at"][5:16].replace("-", "/").replace("T", " ")).font = Font(name=FONT, size=9, color="606060")
        ch = f"{x['from_status'] or '-'} → {x['to_status']}" if x["from_status"] != x["to_status"] else x["to_status"]
        s.cell(row, 2, ch).font = Font(name=FONT, size=9, bold=True)
        nc = s.cell(row, 3, (x["note"] or "") + (f"  (#{x['mail_id']})" if x["mail_id"] else ""))
        s.merge_cells(start_row=row, start_column=3, end_row=row, end_column=4)
        nc.font = Font(name=FONT, size=9)
        nc.alignment = Alignment(wrap_text=True, vertical="top")
        s.row_dimensions[row].height = lines_needed(nc.value, 54) * 14 + 4
        row += 1

    mails = conn.execute("SELECT m.id, m.sent_at, m.sender_name, m.subject FROM to_mails t JOIN mails m ON m.id=t.mail_id "
                         "WHERE t.to_id=? ORDER BY m.sent_at", (r["id"],)).fetchall()
    if mails:
        row += 1
        s.cell(row, 1, "관련 메일").font = Font(name=FONT, size=11, bold=True, color=C_HEAD)
        row += 1
        for m in mails:
            s.cell(row, 1, f"#{m['id']}  {dt(m['sent_at'])}").font = Font(name=FONT, size=9)
            s.cell(row, 2, m["sender_name"]).font = Font(name=FONT, size=9)
            sc = s.cell(row, 3, m["subject"])
            s.merge_cells(start_row=row, start_column=3, end_row=row, end_column=4)
            sc.font = Font(name=FONT, size=9)
            sc.alignment = Alignment(wrap_text=True, vertical="top")
            s.row_dimensions[row].height = lines_needed(m["subject"], 54) * 14 + 4
            row += 1

    if with_images and v["_imgs"]:
        row += 1
        s.cell(row, 1, f"사진 ({len(v['_imgs'])}장)").font = Font(name=FONT, size=11, bold=True, color=C_HEAD)
        row += 1
        for k, im in enumerate(v["_imgs"], 1):
            s.cell(row, 1, f"[{k}] {im['caption'] or ''}").font = Font(name=FONT, size=9, color="404040")
            s.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
            row += 1
            try:
                x = _thumb(im["path"], 620, 460, tmpdir, f"d{r['id']}_{k}")
                s.add_image(x, f"A{row}")
                span = math.ceil((x.height * 0.75) / 20) + 1
                for rr in range(row, row + span):
                    s.row_dimensions[rr].height = 20
                row += span
            except Exception:
                s.cell(row, 1, "(사진을 열 수 없음)")
                row += 1
    s.page_setup.orientation = "portrait"
    s.page_setup.fitToWidth = 1
    s.page_setup.fitToHeight = 0
    s.sheet_properties.pageSetUpPr.fitToPage = True
