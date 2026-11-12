"""설정: localmold.toml 이 있으면 기본값 위에 덮어쓴다 (없어도 됨). 데이터는 모두 data/localmold/ 아래 (저장소에 올리지 않음)."""
from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path

DEFAULT_CONFIG: dict = {
    "db_path": "data/localmold/localmold.db",
    "photo_dir": "data/localmold/photos",      # 문제점 사진 (L번호별 폴더)
    "export_dir": "data/localmold/export",     # 엑셀 내보내기 (차종별 폴더)
    "inbox_dir": "data/localmold/inbox",       # 드라이브에서 받은 외주처 엑셀 · 통합 리스트
    "phone_dir": "data/localmold/phone",       # 휴대폰 사진 접수에서 받은 파일
    "port": 8766,                              # 업무 비서(8765)와 다른 주소
    # 업무 비서 설정 파일 (예전 데이터 옮기기 · 나중에 메일 이력 연결에 씀)
    "mailhub_config": "config.toml",
    # 휴대폰 사진 접수 페이지 (claude.ai 비공개 페이지)
    "phone_page": "https://claude.ai/artifact/N24XX6Sp2iByKyNKNR44G5",
}


def load_config(path: str | os.PathLike | None = None) -> dict:
    path = Path(path or os.environ.get("LOCALMOLD_CONFIG", "localmold.toml"))
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if path.exists():
        try:
            with open(path, "rb") as f:
                cfg.update(tomllib.load(f))
        except tomllib.TOMLDecodeError as e:
            raise SystemExit(f"[설정 오류] {path} 파일 {e}") from None
    return cfg


def mailhub_db(cfg: dict) -> Path:
    """업무 비서 DB 위치 (업무 비서 설정 파일의 archive.db_path, 없으면 기본 위치)."""
    p = Path(cfg.get("mailhub_config") or "config.toml")
    if p.exists():
        with open(p, "rb") as f:
            v = (tomllib.load(f).get("archive") or {}).get("db_path")
        if v:
            return Path(v)
    return Path("data/mailhub.db")
