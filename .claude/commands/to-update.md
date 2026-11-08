---
description: T/O 문제점 대장 갱신 - T/O 결과 메일·보고서에서 문제점 등록 제안, 새 메일로 원인·대책·결과 갱신 제안 (예: /to-update, /to-update BC4t CUV)
---
T/O 문제점 대장을 최신 메일 기준으로 갱신해줘. $ARGUMENTS (차종 등 범위가 있으면 그 범위만)

## 1. 현황
- `python -m mailhub to check` (관련 메일 자동 연결 포함), `python -m mailhub to list --json`

## 2. 미등록 T/O 메일 → 문제점 등록 제안
- `python -m mailhub to inbox --json --days 60`
- 각 메일을 `show <id> --json --max-chars 12000` 으로 읽는다. T/O 결과 보고서(PPT/PDF/엑셀)는 `att_text` 에 있다.
- **불량 1가지 = 1행**으로 뽑는다: 차종 / 품번 / 품명 / T/O 차수·일자 / 금형업체 / 성형처 / 재질 / 불량 유형 / 부위 / 현상(수치 포함)
  / 원인(언급됐으면, 원인 구분 포함) / 대책(언급됐으면) / 담당 / 대책 목표일
- 불량 유형은 표준 이름으로: 싱크, 보이드, 웰드라인, 플로우마크, 은줄, 가스·탄화, 미성형, 버, 변형, 치수불량, 단차·갭, 크랙, 백화,
  광택·외관, 이물·흑점, 색차·이색, 젯팅, 취출·이젝터, 게이트, 조립불량, 감싸기·접착
- 이미 대장에 있는 문제(같은 품번+같은 불량+같은 차수)는 새로 만들지 말고 `to link` 를 제안한다.
- 표로 보여준 뒤 사용자가 확인한 것만 등록:
  `python -m mailhub to add --mail <메일번호> --car .. --part-no .. --part-name .. --stage T1 --date .. --defect .. --location .. --phenomenon ".." [--cause-cat .. --cause ".." --action-cat .. --cm ".." --owner .. --due ..]`
- 등록 결과에 **재발**이 나오면 이전 대책과 결과를 강조해서 보여준다 (이전 대책이 효과가 없었는지, 다른 원인인지).
- 대책이 없는 신규 문제점은 `/to-advice <T번호>` 로 개선안을 받을 수 있다고 안내한다.

## 3. 진행 중 문제점 → 갱신 제안
- `✉새 관련메일` 이 있는 항목은 `to show <T번호> --json` 으로 새 메일을 읽고 판단:
  - 원인 분석 결과 → `--cause-cat --cause`, 상태 `원인분석`/`대책수립`
  - 대책 확정·일정 → `--action-cat --cm --owner --due`, 상태 `대책수립`
  - 금형 수정 완료·조건 변경 적용 → 상태 `대책적용`, `--verify-due` (다음 T/O 일정)
  - 다음 T/O 결과 → OK 면 `--result ".." --effective 유효` 상태 `종결`, 부분 개선이면 `부분`, 그대로면 `무효` + 새 대책 필요
  - 설계 변경이 필요하면 E.O 항목과 연결 (`--eo <E번호>`), 없으면 `/eo-update` 로 등록 제안
- 제안은 표로: T번호 | 현재 → 제안 | 근거 메일(#번호, 날짜, 요약) | 실행 명령. 승인한 것만 실행.
- 대책 목표일·확인 목표일이 비어 있는 진행 중 항목은 따로 모아 기한을 제안한다 (기한이 없으면 지연 알림이 안 됨).

## 4. 사진
- 새로 등록한 항목: `python -m mailhub to img <T번호> --collect` (PST 메일이면 `--outlook`). 불량 사진이 없으면 캡처 후 `to img <T번호> --paste --caption ".."` 안내.

## 5. 마무리
- 요약: 진행 중 N건(상태별), 지연, 재발, 이번에 등록/갱신한 건
- 엑셀: `python -m mailhub to export data/TO_ISSUES.xlsx --collect --open`

기록을 바꾸는 명령(`to add/set/link/import`)은 사용자 확인 후에만 실행한다. 메일은 보내지 않는다.
