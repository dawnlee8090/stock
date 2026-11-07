"""설정 로드: config.toml 이 있으면 기본값 위에 덮어쓴다."""
from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path

DEFAULT_CONFIG: dict = {
    "archive": {
        "db_path": "data/mailhub.db",
        "msg_dir": "data/msg",
        "attach_dir": "data/attachments",
    },
    # 내 메일 주소 (보낸 메일은 '조치 필요' 판정에서 제외)
    "me": {"addresses": [], "names": []},
    # 태그명 = 키워드 목록 (대소문자 무시, 제목/본문/첨부파일명 대상)
    "tags": {
        "E.O": ["E.O", "EO 변경", "EO변경", "설계변경", "설계 변경", "ECN", "ECO", "도면 변경", "도면변경"],
        "T/O": ["T/O", "TRY OUT", "TRYOUT", "트라이", "시사출", "초도품"],
        "금형수정": ["금형수정", "금형 수정", "수정금형", "육성", "금형 보완", "금형보완", "사상"],
        "검사구": ["검사구", "C/F", "CHECKING FIXTURE", "측정 치구", "측정치구"],
        "품질문제": ["불량", "웰드", "WELD", "싱크", "SINK", "변형", "WARPAGE", "BURR", "미성형",
                    "SHORT SHOT", "가스", "흑점", "은줄", "FLOW MARK", "단차", "갭", "GAP", "백화"],
        "금형이관": ["금형 이관", "금형이관", "이관 금형", "금형 선적", "금형선적", "금형 반출", "금형반출"],
        "일정": ["일정", "납기", "지연", "SOP", "PROTO", "PILOT", "M-CAR", "P-CAR"],
        "설계반영": ["설계 반영", "설계반영", "데이터 반영", "데이터반영", "3D 반영", "2D 반영", "CATIA", "STP", "STEP"],
    },
    # 조치(회신/검토)가 필요한 메일로 판단할 키워드
    "action": {
        "keywords": ["요청", "검토 부탁", "검토부탁", "검토 바랍", "검토바랍", "회신", "확인 부탁", "확인부탁",
                     "확인 바랍", "확인바랍", "긴급", "ASAP", "URGENT", "협조 부탁", "의견 부탁", "제출 바랍"],
    },
    # 품번 추출 정규식 (기본: 현대/기아 형식 예) 84710-AA000)
    "part_number": {"patterns": [r"\b\d{5}-[A-Z0-9]{5}\b"]},
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | os.PathLike | None = None) -> dict:
    path = Path(path or os.environ.get("MAILHUB_CONFIG", "config.toml"))
    if not path.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        with open(path, "rb") as f:
            return _merge(DEFAULT_CONFIG, tomllib.load(f))
    except tomllib.TOMLDecodeError as e:
        hint = ""
        if "overwrite" in str(e):
            hint = ("\n  → 같은 이름이 두 번 있습니다. 한 이름에 여러 키워드는 쉼표로 이어 쓰세요."
                    '\n    예) "금형업체" = ["@a-mold.co.kr", "A정밀", "@b-mold.co.kr"]')
        elif "Unclosed" in str(e) or "Expected" in str(e) or "Invalid" in str(e):
            hint = ('\n  → 따옴표("")나 대괄호([])가 빠졌는지, 쉼표가 맞는지 확인하세요.'
                    "\n    정규식 패턴은 작은따옴표(')로 감싸세요.")
        raise SystemExit(f"[설정 오류] {path} 파일 {e}{hint}") from None
