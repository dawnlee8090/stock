---
description: 현지 금형 문제점 - 구글 드라이브의 외주처 문제점 엑셀 · 통합 리스트를 받아 앱에 반영 제안 (예: /local-sync, /local-sync BC4t CUV, /local-sync ASHA-2)
---
구글 드라이브에 있는 외주처별 문제점 관리 자료를 현지 금형 문제점(`local`)에 반영해줘. $ARGUMENTS
(차종 이름이면 그 차종, 외주처 이름이면 그 외주처만)

## 1. 현황
- `python -m mailhub local cars` 로 차종·외주처별 금형 수, 진행, 지연, 검증 T/O 필요를 먼저 확인한다.

## 2. 드라이브에서 찾기 (Google Drive 연결 필요)
- 폴더: 사용자가 알려준 폴더(예: '외주처 별 문제점 리스트 취합') 또는 차종 이름으로 `search_files` 검색.
- 대상 파일
  - **통합 리스트**: '통합 금형 문제점 전체 관리 리스트' (외주처 · 품번 · 품명 · 카테고리 · 발생 문제점 …)
  - **외주처 엑셀**: '<차종>_CAR_<외주처>_REV.n' (갑지 MASTER + 금형별 을지 + 사진)
  - '사본' 폴더의 파일은 예전 사본이므로 사용자가 말하지 않으면 제외한다.
- 수정 시각(modifiedTime)을 보여주고, 앱에 마지막으로 가져온 뒤 바뀐 파일만 고른다.

## 3. 내려받기 (data/local/inbox/ 에 저장, 저장소에 커밋하지 않음)
- 통합 리스트: `download_file_content` exportMimeType `text/csv` → `data/local/inbox/<차종>_통합리스트.csv`
- 외주처 엑셀: exportMimeType `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` 로 받으면 **을지 사진까지** 가져올 수 있다.
  - 구글 시트는 약 10MB 가 넘으면 내보내기가 거절된다(사진이 많은 외주처). 그때는
    `text/csv` 로 갑지(MASTER)만 받아 금형 목록·건수만 반영하고, 사용자에게 시트에서 **파일 > 다운로드 > Microsoft Excel(.xlsx)** 로
    받아 `data/local/inbox/` 에 넣어 달라고 안내한다 (사진은 그 파일로 가져온다).
- 받은 내용(base64)은 파일로 풀어 저장한다. 내용은 회사 기밀이므로 외부로 보내지 않는다.

## 4. 가져오기 제안 (실행은 사용자 확인 후)
표로 제안한다: | 파일 | 수정 시각 | 종류(외주처 엑셀/통합 리스트/갑지 CSV) | 외주처 | 제안 명령 |
- `python -m mailhub local import "data/local/inbox/<파일>" --car "<차종>" [--vendor <외주처>]`
  - 외주처는 파일 이름 `_CAR_외주처_REV` 나 갑지 제목에서 자동으로 읽는다. 갑지 CSV 의 제목 줄이 비어 있으면 `--vendor` 를 붙인다.
  - 순서: 통합 리스트 → 갑지 CSV → 외주처 엑셀(사진). 같은 문제점은 품번 · 문제점 · 발생일로 맞춰 합쳐진다.
  - 앱에서 고친 문제점(edited)은 덮어쓰지 않는다는 점을 알려준다.
- 실행 후 결과 숫자(신규 · 갱신 · 변경 없음 · 앱 수정 유지 · 사진)를 그대로 보고한다.

## 5. 정리
- `python -m mailhub local cars` 로 반영 후 현황을 다시 보여주고, 바뀐 점(새 문제점, 단계가 바뀐 건, 새 사진)을 요약한다.
- 바로 챙길 것: 수정 지연, 중요도 상 진행, **검증 T/O 필요**(금형 수정은 끝났는데 T/O 결과 없음) 상위 10건 —
  `python -m mailhub local list --car "<차종>" --status wait --json` 등. 근거는 L번호로 적는다.
- 업체에 보낼 내용이 있으면 메일 **초안만** 만든다 (발송은 사용자가 한다).
