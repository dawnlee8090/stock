"""E.O 리스트 엑셀 (보기 좋게).

시트
  - E.O LIST : 제목·요약 줄, 항목 그룹 머리글, 상태 색, 지연 강조, 대표 그림 썸네일, 내용에 맞춘 열 너비·행 높이
  - 요약     : 상태별·차종별 건수, 지연 목록
  - E<번호>  : 항목별 상세 (기본 정보, 일정, 이력, 관련 메일, 참고 그림 크게)
"""
from __future__ import annotations

import math
import tempfile
import unicodedata
from datetime import date, datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

FONT = "맑은 고딕"
C_HEAD = "1F3864"       # 진한 남색
C_GROUP = {"기본 정보": "2F5597", "변경 내용": "548235", "진행 현황": "BF8F00", "일정": "C55A11", "참고": "7F7F7F"}
C_BAND = "F7F9FC"
C_LINE = "D9D9D9"
STATUS_FILL = {"접수": "FFF2CC", "검토중": "FFE699", "수정요청": "DDEBF7", "수정중": "BDD7EE", "수정완료": "C6E0B4",
               "검증중": "A9D08E", "완료": "E7E6E6", "수정불필요": "EDEDED", "보류": "F8CBAD", "취소": "EDEDED"}

# (키, 머리글, 그룹, 최소폭, 최대폭, 종류)
COLUMNS = [
    ("id", "No", "기본 정보", 5, 6, "link"),
    ("car", "차종", "기본 정보", 8, 14, "text"),
    ("part_no", "품번", "기본 정보", 12, 18, "text"),
    ("part_name", "품명", "기본 정보", 12, 26, "text"),
    ("eo_no", "E.O No", "기본 정보", 9, 16, "text"),
    ("change_desc", "변경 내용", "변경 내용", 24, 46, "text"),
    ("image", "참고 그림", "변경 내용", 25, 25, "image"),
    ("status", "상태", "진행 현황", 9, 10, "status"),
    ("alert", "지연 / 임박", "진행 현황", 11, 16, "alert"),
    ("mold_vendor", "금형업체", "진행 현황", 9, 16, "text"),
    ("mold_action", "수정방식", "진행 현황", 8, 12, "text"),
    ("cf", "검사구", "진행 현황", 8, 16, "text"),
    ("cost", "수정비", "진행 현황", 8, 14, "text"),
    ("received_at", "접수", "일정", 7, 7, "date"),
    ("review_due", "검토회신\n기한", "일정", 8, 8, "date"),
    ("ordered_at", "수정\n요청", "일정", 7, 7, "date"),
    ("mold_due", "수정완료\n목표", "일정", 8, 8, "date"),
    ("mold_done_at", "수정\n완료", "일정", 7, 7, "date"),
    ("verify_due", "검증\n목표", "일정", 7, 7, "date"),
    ("verified_at", "검증\n완료", "일정", 7, 7, "date"),
    ("apply_point", "적용 시점", "일정", 9, 16, "text"),
    ("mails", "관련 메일", "참고", 10, 16, "text"),
    ("note", "메모", "참고", 16, 40, "text"),
]

THUMB_W, THUMB_H = 150, 100        # 목록 썸네일 (픽셀)
DETAIL_W = 620                      # 상세 시트 그림 폭 (픽셀)
PX_PER_CHAR = 7.0                   # 열 너비 1 ≈ 7px
PT_PER_LINE = 15.0


def _link(cell, sheet: str) -> None:
    """같은 파일 안의 시트로 이동하는 링크."""
    from openpyxl.worksheet.hyperlink import Hyperlink
    cell.hyperlink = Hyperlink(ref=cell.coordinate, location=f"'{sheet}'!A1", display=str(cell.value))


def disp_len(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def lines_needed(text: str, width: float) -> int:
    if not text:
        return 1
    usable = max(1.0, width - 1.5)
    return sum(max(1, math.ceil(disp_len(line) / usable)) for line in str(text).split("\n"))


def _d(v: str):
    try:
        return date.fromisoformat(v[:10]) if v else None
    except ValueError:
        return None


def _thin():
    s = Side(style="thin", color=C_LINE)
    return Border(left=s, right=s, top=s, bottom=s)


def _thumb(src: str, w: int, h: int, tmpdir: Path, tag: str):
    """원본 그림 → 지정 크기 안에 맞춘 JPEG (엑셀 용량 줄이기). openpyxl Image 반환."""
    from openpyxl.drawing.image import Image as XLImage
    from PIL import Image
    im = Image.open(src).convert("RGB")
    im.thumbnail((w * 2, h * 2))     # 화면 확대 시에도 선명하게 2배 해상도
    p = tmpdir / f"{tag}.jpg"
    im.save(p, "JPEG", quality=85)
    x = XLImage(str(p))
    scale = min(w / im.width, h / im.height)
    x.width, x.height = int(im.width * scale), int(im.height * scale)
    return x


def build(conn, rows: list, path: Path, delay_of, soon_of, images_of, with_images: bool = True,
          title: str = "E.O 관리 리스트") -> Path:
    rows = sorted(rows, key=lambda r: r["id"])
    wb = Workbook()
    ws = wb.active
    ws.title = "E.O LIST"
    ws.sheet_view.showGridLines = False
    tmp = tempfile.TemporaryDirectory(prefix="mailhub_xl_")
    tmpdir = Path(tmp.name)
    border = _thin()
    ncol = len(COLUMNS)
    today = date.today()

    # ---- 1~2행: 제목, 요약 ----
    open_rows = [r for r in rows if r["status"] not in ("완료", "수정불필요", "취소")]
    n_delay = sum(1 for r in open_rows if delay_of(r))
    ws.cell(1, 1, title).font = Font(name=FONT, size=16, bold=True, color=C_HEAD)
    ws.cell(1, ncol - 3, f"작성 {datetime.now():%Y-%m-%d %H:%M}").font = Font(name=FONT, size=9, color="808080")
    summary = f"전체 {len(rows)}건   ·   진행 중 {len(open_rows)}건   ·   지연 {n_delay}건   ·   완료 {sum(1 for r in rows if r['status'] == '완료')}건"
    c = ws.cell(2, 1, summary)
    c.font = Font(name=FONT, size=10, bold=True, color="C00000" if n_delay else "404040")
    ws.row_dimensions[1].height = 28
    ws.row_dimensions[2].height = 20

    # ---- 3행: 그룹 머리글, 4행: 열 머리글 ----
    start = 1
    while start <= ncol:
        g = COLUMNS[start - 1][2]
        end = start
        while end < ncol and COLUMNS[end][2] == g:
            end += 1
        ws.merge_cells(start_row=3, start_column=start, end_row=3, end_column=end)
        cell = ws.cell(3, start, g)
        cell.font = Font(name=FONT, size=10, bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=C_GROUP[g])
        cell.alignment = Alignment(horizontal="center", vertical="center")
        for ci in range(start, end + 1):
            ws.cell(3, ci).border = border
        start = end + 1
    for ci, (key, head, g, *_rest) in enumerate(COLUMNS, 1):
        cell = ws.cell(4, ci, head)
        cell.font = Font(name=FONT, size=9, bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=C_HEAD)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border
    ws.row_dimensions[3].height = 18
    ws.row_dimensions[4].height = 32

    # ---- 값 만들기 ----
    data = []
    for r in rows:
        mails = [str(m[0]) for m in conn.execute("SELECT mail_id FROM eo_mails WHERE eo_id=? ORDER BY mail_id",
                                                  (r["id"],))]
        cf = " / ".join(x for x in (r["cf_impact"], r["cf_vendor"]) if x)
        alert = delay_of(r)
        alert = f"⚠ {alert}" if alert else (f"⏰ {soon_of(r)}" if soon_of(r) else "")
        imgs = images_of(r["id"]) if with_images else []
        v = {k: (r[k] if k in r.keys() else "") for k, *_ in COLUMNS}
        v.update(cf=cf, alert=alert, mails=", ".join("#" + m for m in mails[-6:]) + (" …" if len(mails) > 6 else ""),
                 image=(f"외 {len(imgs) - 1}장" if len(imgs) > 1 else ""), _imgs=imgs)
        data.append(v)

    # ---- 열 너비: 내용 길이에 맞추되 최소·최대 범위 안 ----
    widths = {}
    for ci, (key, head, g, mn, mx, kind) in enumerate(COLUMNS, 1):
        if kind in ("date", "image", "link", "status"):
            w = mx
        else:
            longest = max([disp_len(line) for v in data for line in str(v.get(key) or "").split("\n")] + [0])
            w = min(mx, max(mn, longest + 2))
        widths[key] = w
        ws.column_dimensions[get_column_letter(ci)].width = w

    # ---- 데이터 행 ----
    for i, v in enumerate(data):
        row = 5 + i
        band = PatternFill("solid", fgColor=C_BAND) if i % 2 else None
        for ci, (key, head, g, mn, mx, kind) in enumerate(COLUMNS, 1):
            val = v.get(key)
            cell = ws.cell(row, ci)
            cell.border = border
            cell.font = Font(name=FONT, size=9)
            cell.alignment = Alignment(vertical="center", wrap_text=True,
                                       horizontal="center" if kind in ("date", "status", "link") else "left")
            if band:
                cell.fill = band
            if kind == "date":
                dv = _d(val or "")
                if dv:
                    cell.value = dv
                    cell.number_format = "m/d"
                    # 지난 기한(아직 해당 단계)이면 빨간 글씨
                    late_field = {"검토": "review_due", "금형": "mold_due", "검증": "verify_due"}.get(
                        v["alert"][2:4] if v["alert"].startswith("⚠") else "", "")
                    if key == late_field and dv < today:
                        cell.font = Font(name=FONT, size=9, bold=True, color="C00000")
            elif kind == "status":
                cell.value = val
                cell.fill = PatternFill("solid", fgColor=STATUS_FILL.get(val, "FFFFFF"))
                cell.font = Font(name=FONT, size=9, bold=True)
            elif kind == "alert":
                cell.value = val
                if val.startswith("⚠"):
                    cell.font = Font(name=FONT, size=9, bold=True, color="C00000")
                elif val:
                    cell.font = Font(name=FONT, size=9, bold=True, color="C55A11")
            elif kind == "link":
                cell.value = f"E{val}"
                _link(cell, f"E{val}")
                cell.font = Font(name=FONT, size=9, bold=True, color="0563C1", underline="single")
            elif kind == "image":
                cell.value = val
                cell.alignment = Alignment(vertical="bottom", horizontal="right")
                cell.font = Font(name=FONT, size=8, color="808080")
            else:
                cell.value = val or ""
        # 행 높이: 가장 긴 칸 기준, 썸네일이 있으면 그 높이 이상
        need = max(lines_needed(str(v.get(k) or ""), widths[k]) for k, *_r, kind in COLUMNS if kind == "text")
        h = max(20.0, need * PT_PER_LINE + 6)
        if v["_imgs"]:
            ci = [k for k, *_ in COLUMNS].index("image") + 1
            try:
                x = _thumb(v["_imgs"][0]["path"], THUMB_W, THUMB_H, tmpdir, f"t{v['id']}")
                ws.add_image(x, f"{get_column_letter(ci)}{row}")
                h = max(h, x.height * 0.75 + (18 if v["image"] else 8))  # 아래에 '외 N장' 글자 자리
            except Exception:
                pass
        ws.row_dimensions[row].height = min(h, 300)

    last = 4 + len(data)
    ws.freeze_panes = "F5"
    if data:
        ws.auto_filter.ref = f"A4:{get_column_letter(ncol)}{last}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = "3:4"

    _summary_sheet(wb, rows, delay_of)
    for v, r in zip(data, rows):
        _detail_sheet(wb, conn, r, v, delay_of, tmpdir, with_images)

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    tmp.cleanup()
    return path


def _summary_sheet(wb, rows, delay_of):
    from .eo import STATUSES
    s = wb.create_sheet("요약")
    s.sheet_view.showGridLines = False
    border = _thin()
    s.cell(1, 1, "E.O 현황 요약").font = Font(name=FONT, size=14, bold=True, color=C_HEAD)
    s.cell(2, 1, f"작성 {datetime.now():%Y-%m-%d %H:%M}").font = Font(name=FONT, size=9, color="808080")

    def table(r0, c0, head, items):
        for j, h in enumerate(head):
            c = s.cell(r0, c0 + j, h)
            c.font = Font(name=FONT, size=10, bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor=C_HEAD)
            c.alignment = Alignment(horizontal="center")
            c.border = border
        for i, it in enumerate(items, 1):
            for j, val in enumerate(it):
                c = s.cell(r0 + i, c0 + j, val)
                c.font = Font(name=FONT, size=10)
                c.border = border
                c.alignment = Alignment(horizontal="center" if j else "left", vertical="center", wrap_text=True)
        return r0 + len(items) + 2

    items = []
    for st in STATUSES:
        n = sum(1 for r in rows if r["status"] == st)
        if n:
            items.append((st, n, sum(1 for r in rows if r["status"] == st and delay_of(r))))
    nxt = table(4, 1, ["상태", "건수", "지연"], items)
    for i, it in enumerate(items, 1):
        s.cell(4 + i, 1).fill = PatternFill("solid", fgColor=STATUS_FILL.get(it[0], "FFFFFF"))
    cars: dict[str, list] = {}
    for r in rows:
        cars.setdefault(r["car"] or "(차종 없음)", []).append(r)
    table(4, 5, ["차종", "전체", "진행 중", "지연"],
          [(k, len(v), sum(1 for r in v if r["status"] not in ("완료", "수정불필요", "취소")),
            sum(1 for r in v if delay_of(r))) for k, v in sorted(cars.items())])
    delayed = [r for r in rows if delay_of(r)]
    if delayed:
        s.cell(nxt, 1, "지연 항목").font = Font(name=FONT, size=11, bold=True, color="C00000")
        table(nxt + 1, 1, ["E번호", "차종", "품번 / 품명", "상태", "지연", "금형업체"],
              [(f"E{r['id']}", r["car"], f"{r['part_no']} {r['part_name']}".strip(), r["status"], delay_of(r),
                r["mold_vendor"]) for r in delayed])
    for col, w in zip("ABCDEFGH", (12, 10, 34, 10, 16, 14, 10, 8)):
        s.column_dimensions[col].width = w
    s.page_setup.orientation = "landscape"
    s.page_setup.fitToWidth = 1
    s.page_setup.fitToHeight = 0
    s.sheet_properties.pageSetUpPr.fitToPage = True


def _detail_sheet(wb, conn, r, v, delay_of, tmpdir: Path, with_images: bool):
    s = wb.create_sheet(f"E{r['id']}")
    s.sheet_view.showGridLines = False
    border = _thin()
    s.column_dimensions["A"].width = 14
    s.column_dimensions["B"].width = 40
    s.column_dimensions["C"].width = 14
    s.column_dimensions["D"].width = 40
    back = s.cell(1, 4, "← E.O LIST")
    _link(back, "E.O LIST")
    back.font = Font(name=FONT, size=9, color="0563C1", underline="single")
    back.alignment = Alignment(horizontal="right")
    name = " ".join(x for x in (r["car"], r["part_no"], r["part_name"]) if x)
    s.cell(1, 1, f"E{r['id']}  {name}").font = Font(name=FONT, size=14, bold=True, color=C_HEAD)
    st = s.cell(2, 1, r["status"])
    st.fill = PatternFill("solid", fgColor=STATUS_FILL.get(r["status"], "FFFFFF"))
    st.font = Font(name=FONT, size=10, bold=True)
    st.alignment = Alignment(horizontal="center")
    if delay_of(r):
        s.cell(2, 2, f"⚠ {delay_of(r)}").font = Font(name=FONT, size=10, bold=True, color="C00000")

    def kv(row, k1, v1, k2="", v2=""):
        for col, val, is_key in ((1, k1, True), (2, v1, False), (3, k2, True), (4, v2, False)):
            c = s.cell(row, col, val)
            c.border = border
            c.alignment = Alignment(vertical="center", wrap_text=True)
            if is_key:
                c.font = Font(name=FONT, size=9, bold=True, color="404040")
                c.fill = PatternFill("solid", fgColor="EEF2F8")
            else:
                c.font = Font(name=FONT, size=10)
        s.row_dimensions[row].height = max(lines_needed(str(v1 or ""), 40), lines_needed(str(v2 or ""), 40)) * 15 + 6

    def dt(x):
        return x[5:10].replace("-", "/") if x else ""

    rows = [("E.O No", r["eo_no"], "발행처", r["customer"]),
            ("품번", r["part_no"], "품명", r["part_name"]),
            ("금형업체", r["mold_vendor"], "수정방식", r["mold_action"]),
            ("검사구", v["cf"], "수정비", r["cost"]),
            ("적용 시점", r["apply_point"], "관련 메일", v["mails"]),
            ("접수", dt(r["received_at"]), "검토회신 기한", dt(r["review_due"])),
            ("수정요청", dt(r["ordered_at"]), "수정완료 목표", dt(r["mold_due"])),
            ("수정완료", dt(r["mold_done_at"]), "검증 목표", dt(r["verify_due"])),
            ("검증완료", dt(r["verified_at"]), "", "")]
    row = 4
    kv(row, "변경 내용", r["change_desc"])
    s.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
    s.row_dimensions[row].height = lines_needed(r["change_desc"] or "", 94) * 15 + 6
    row += 1
    for k1, v1, k2, v2 in rows:
        kv(row, k1, v1, k2, v2)
        row += 1
    if r["note"]:
        kv(row, "메모", r["note"])
        s.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
        s.row_dimensions[row].height = lines_needed(r["note"], 94) * 15 + 6
        row += 1

    logs = conn.execute("SELECT * FROM eo_log WHERE eo_id=? ORDER BY id", (r["id"],)).fetchall()
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

    mails = conn.execute(
        "SELECT m.id, m.sent_at, m.sender_name, m.subject FROM eo_mails em JOIN mails m ON m.id=em.mail_id "
        "WHERE em.eo_id=? ORDER BY m.sent_at", (r["id"],)).fetchall()
    if mails:
        row += 1
        s.cell(row, 1, "관련 메일").font = Font(name=FONT, size=11, bold=True, color=C_HEAD)
        row += 1
        for m in mails:
            s.cell(row, 1, f"#{m['id']}  {m['sent_at'][5:10].replace('-', '/')}").font = Font(name=FONT, size=9)
            s.cell(row, 2, m["sender_name"]).font = Font(name=FONT, size=9)
            sc = s.cell(row, 3, m["subject"])
            s.merge_cells(start_row=row, start_column=3, end_row=row, end_column=4)
            sc.font = Font(name=FONT, size=9)
            sc.alignment = Alignment(wrap_text=True, vertical="top")
            s.row_dimensions[row].height = lines_needed(m["subject"], 54) * 14 + 4
            row += 1

    if with_images and v["_imgs"]:
        row += 1
        s.cell(row, 1, f"참고 그림 ({len(v['_imgs'])}장)").font = Font(name=FONT, size=11, bold=True, color=C_HEAD)
        row += 1
        for k, im in enumerate(v["_imgs"], 1):
            s.cell(row, 1, f"[{k}] {im['caption'] or ''}").font = Font(name=FONT, size=9, color="404040")
            s.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
            row += 1
            try:
                x = _thumb(im["path"], DETAIL_W, 460, tmpdir, f"d{r['id']}_{k}")
                s.add_image(x, f"A{row}")
                # 그림 높이만큼 행 비우기 (행 높이 20pt 기준)
                rows_span = math.ceil((x.height * 0.75) / 20) + 1
                for rr in range(row, row + rows_span):
                    s.row_dimensions[rr].height = 20
                row += rows_span
            except Exception:
                s.cell(row, 1, "(그림을 열 수 없음)")
                row += 1
    s.page_setup.orientation = "portrait"
    s.page_setup.fitToWidth = 1
    s.page_setup.fitToHeight = 0
    s.sheet_properties.pageSetUpPr.fitToPage = True
