"""앱에서 Claude Code 를 불러 쓰기 (내 PC 의 claude 명령, 비대화형 -p 모드).

- 검토·회신 초안: .claude/commands/mail-review.md 내용을 그대로 프롬프트로 써서 결과(마크다운)를 data/reviews/<메일번호>.md 에 저장
- E.O 자동 기입: 메일·첨부에서 품번별 E.O 항목을 JSON 으로 뽑기 (등록은 사용자가 화면에서 확인 후)
Claude 에게는 읽기 명령(mailhub show/search)만 허용한다. 기록을 바꾸거나 메일을 보내는 명령은 허용하지 않는다.
claude 가 없으면 메일 정보만으로 기본값을 채운다 (품번별 1행).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 프로젝트 폴더 (.claude/commands, CLAUDE.md 가 있는 곳)
READ_TOOLS = ["Bash(python -m mailhub show:*)", "Bash(python -m mailhub search:*)", "Bash(python -m mailhub eo list:*)",
              "Bash(python -m mailhub to similar:*)", "Read"]
TIMEOUT = 600

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def find_claude() -> str | None:
    p = shutil.which("claude")
    if p:
        return p
    for c in (Path.home() / ".local" / "bin" / "claude.exe", Path.home() / ".local" / "bin" / "claude"):
        if c.exists():
            return str(c)
    return None


def _command_prompt(name: str, args: str) -> str:
    """.claude/commands/<name>.md 의 본문에 $ARGUMENTS 를 넣은 프롬프트."""
    text = (ROOT / ".claude" / "commands" / f"{name}.md").read_text(encoding="utf-8")
    text = re.sub(r"^---.*?---\s*", "", text, flags=re.S)
    return text.replace("$ARGUMENTS", args)


def run_claude(prompt: str, tools: list[str] | None = None, timeout: int = TIMEOUT) -> str:
    exe = find_claude()
    if not exe:
        raise RuntimeError("이 PC 에서 Claude Code(claude 명령)를 찾지 못했습니다")
    cmd = [exe, "-p", prompt, "--output-format", "text", "--allowedTools", *(tools or READ_TOOLS)]
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if r.returncode != 0 and not r.stdout.strip():
        raise RuntimeError((r.stderr or "Claude 실행 실패").strip()[:500])
    return r.stdout.strip()


# ---------------- 작업(백그라운드) ----------------
def _start(key: str, fn) -> dict:
    with _lock:
        job = _jobs.get(key)
        if job and job["state"] == "running":
            return job
        job = {"state": "running", "started": datetime.now().isoformat(timespec="seconds")}
        _jobs[key] = job

    def work():
        try:
            job["result"] = fn()
            job["state"] = "done"
        except Exception as e:  # 화면에 사유 표시
            job["state"] = "error"
            job["error"] = str(e)
        job["finished"] = datetime.now().isoformat(timespec="seconds")

    threading.Thread(target=work, daemon=True).start()
    return job


def job(key: str) -> dict | None:
    return _jobs.get(key)


# ---------------- 검토·회신 초안 ----------------
def review_path(cfg: dict, mail_id: int) -> Path:
    return Path(cfg["archive"]["db_path"]).parent / "reviews" / f"{mail_id}.md"


def saved_review(cfg: dict, mail_id: int) -> dict | None:
    p = review_path(cfg, mail_id)
    if not p.exists():
        return None
    return {"text": p.read_text(encoding="utf-8"),
            "at": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="minutes")}


def start_review(cfg: dict, mail_id: int) -> dict:
    def fn():
        prompt = _command_prompt("mail-review", str(mail_id)) + (
            "\n\n[앱에서 실행 중] 결과만 마크다운으로 출력한다. 파일을 만들거나 기록을 바꾸는 명령(mark, req add, eo add 등)은"
            " 실행하지 말고 '다음 조치'에 명령으로만 적는다.")
        text = run_claude(prompt)
        p = review_path(cfg, mail_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return {"text": text, "at": datetime.now().isoformat(timespec="minutes")}
    return _start(f"review:{mail_id}", fn)


# ---------------- E.O 자동 기입 ----------------
EO_KEYS = ["eo_no", "car", "part_no", "part_name", "change_desc", "customer", "apply_point", "review_due", "note"]

EO_PROMPT = """메일 #{mid} 에서 E.O(설계변경) 항목을 뽑아줘.
1. `python -m mailhub show {mid} --json --max-chars 15000` 으로 본문과 첨부 내용(att_text, 첨부 E.O LIST 엑셀·PDF)을 읽는다.
2. 품번(부품)별로 1개씩, 아래 키를 가진 JSON 배열만 출력한다. 설명 문장 없이 ```json 코드 블록 하나만.
   - eo_no: E.O 번호 (없으면 "")
   - car: 차종
   - part_no: 품번 전체 형식 (LH/RH 한 쌍은 86525/26-BNGA0 처럼 가능, 모르면 "")
   - part_name: 품명
   - change_desc: 변경 내용 (무엇을 어떻게, 치수 포함, 한 줄)
   - customer: 발행처 (고객사·설계팀)
   - apply_point: 적용 시점 (메일에 있으면)
   - review_due: 메일에 회신 기한이 있으면 YYYY-MM-DD (올해 {year}년 기준), 없으면 ""
   - note: 확인이 필요한 점 (짧게)
3. 이미 E.O 리스트에 있는 품번인지 `python -m mailhub eo list --q <품번> --json` 으로 확인해서, 있으면 note 에 "기존 E<번호> 와 같음" 이라고 적는다.
기록을 바꾸는 명령은 실행하지 않는다."""


def parse_json_block(text: str) -> list[dict]:
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, flags=re.S) or re.search(r"(\[\s*\{.*\}\s*\])", text, flags=re.S)
    if not m:
        raise ValueError("Claude 결과에서 항목 목록(JSON)을 찾지 못했습니다")
    items = json.loads(m.group(1))
    return [{k: str(it.get(k) or "") for k in EO_KEYS} for it in items if isinstance(it, dict)]


def fallback_eo_items(conn, mail_id: int) -> list[dict]:
    """Claude 없이: 메일의 품번마다 1행, 변경 내용은 제목으로."""
    from .classify import thread_key
    r = conn.execute("SELECT subject, part_nos, sender_name FROM mails WHERE id=?", (mail_id,)).fetchone()
    if not r:
        return []
    parts = [p for p in (r["part_nos"] or "").split(",") if p][:15] or [""]
    eo_no = ""
    m = re.search(r"\b(E\.?O[\-]?\d[\w\-]{3,})", r["subject"] or "", re.I)   # EO26-0871, E.O-1234
    if m:
        eo_no = m.group(1).upper()
    car = ""
    m = re.match(r"\s*\[?([A-Z]{2}\d[A-Za-z0-9]*(?:\s+(?:CUV|5DR|HEV|EV|N|PE|N-LINE))*)", thread_key(r["subject"] or "").upper())
    if m:
        car = m.group(1)
    return [{"eo_no": eo_no, "car": car, "part_no": p, "part_name": "", "change_desc": r["subject"], "customer": r["sender_name"],
             "apply_point": "", "review_due": "", "note": "자동 추출(기본값) - 내용 확인 필요"} for p in parts]


def start_eo_extract(cfg: dict, mail_id: int, conn_factory) -> dict:
    def fn():
        if not find_claude():
            c = conn_factory()
            try:
                return {"items": fallback_eo_items(c, mail_id), "by": "기본값"}
            finally:
                c.close()
        text = run_claude(EO_PROMPT.format(mid=mail_id, year=datetime.now().year))
        return {"items": parse_json_block(text), "by": "Claude"}
    return _start(f"eo:{mail_id}", fn)


# ---------------- 주제별 이력 정리 · 업체 메일 초안 ----------------
def history_key(q: str, ids: list[int]) -> str:
    import hashlib
    return hashlib.sha1(f"{q}|{','.join(map(str, sorted(ids)))}".encode("utf-8")).hexdigest()[:12]


def history_path(cfg: dict, key: str) -> Path:
    return Path(cfg["archive"]["db_path"]).parent / "history" / f"{key}.md"


def saved_history(cfg: dict, key: str) -> dict | None:
    p = history_path(cfg, key)
    if not re.fullmatch(r"[0-9a-f]{12}", key) or not p.exists():
        return None
    return {"text": p.read_text(encoding="utf-8"),
            "at": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="minutes")}


def _ids_line(ids: list[int]) -> str:
    return ", ".join(f"#{i}" for i in ids[:120])


def start_history(cfg: dict, q: str, ids: list[int]) -> tuple[str, dict]:
    key = history_key(q, ids)

    def fn():
        prompt = _command_prompt("mail-history", q) + (
            f"\n\n[대상 메일] {_ids_line(ids)}\n[앱에서 실행 중] 위 형식의 마크다운만 출력한다.")
        text = run_claude(prompt)
        p = history_path(cfg, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return {"text": text, "at": datetime.now().isoformat(timespec="minutes")}
    return key, _start(f"history:{key}", fn)


DRAFT_PROMPT = """업체에 보낼 메일 초안을 써줘. 자동차 내·외장 사출부품 개발 담당자(나)가 보내는 메일이다.

주제: {q}
받는사람: {to}
대상 메일(근거): {ids}
회신 기한: {due}
추가로 요청할 내용: {note}

1. 각 메일을 `python -m mailhub show <id> --json --max-chars 3000` 로 읽는다.
2. data/my_style.md 가 있으면 읽고(Read) 내 말투·인사·서명을 따른다. 없으면 `python -m mailhub search --mine --to <받는사람 도메인> --json --limit 5` 로 내가 이 업체에 보낸 메일 말투를 참고한다.
3. 목적: 그동안 요청한 문제점과 업체 회신 이력을 짧게 정리해 공유하고, 아직 미결인 항목의 원인·개선대책·적용 일정(필요하면 금형 수정 일정)·검증 결과를 기한까지 회신 요청.
4. 규칙: 메일 번호(#)는 본문에 쓰지 않는다(업체는 모름) - 날짜와 메일 제목으로 가리킨다. 메일에 없는 사실은 넣지 않는다. 정중하지만 간결하게. 메일을 보내거나 기록을 바꾸는 명령은 실행하지 않는다.

출력은 아래 형식만:
제목: <제목>
본문:
<본문>
"""


def parse_draft(text: str) -> dict:
    m = re.search(r"제목\s*[:：]\s*(.+)", text)
    subject = m.group(1).strip() if m else ""
    b = re.search(r"본문\s*[:：]\s*\n?(.*)", text, flags=re.S)
    body = (b.group(1) if b else text).strip()
    body = re.sub(r"^```\w*\n|\n```$", "", body).strip()
    return {"subject": subject, "body": body + "\n"}


def start_draft(cfg: dict, q: str, ids: list[int], to: str, due: str, note: str) -> tuple[str, dict]:
    key = history_key(f"draft|{q}|{to}|{due}|{note}", ids)

    def fn():
        text = run_claude(DRAFT_PROMPT.format(q=q, to=to or "(업체 담당자)", ids=_ids_line(ids), due=due or "3영업일 후",
                                              note=note or "없음"))
        return parse_draft(text) | {"at": datetime.now().isoformat(timespec="minutes")}
    return key, _start(f"draft:{key}", fn)
