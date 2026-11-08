---
description: 금형 이관 종합장 갱신 - 받은 메일(사양서·성형조건·부품·선적·KD·원소재)을 금형별 항목에 반영 제안 (예: /mold-update, /mold-update 12345, /mold-update 멕시코)
---
금형 이관 종합장을 메일 기준으로 갱신해줘. $ARGUMENTS
(숫자면 그 메일 1건만, 글자면 이관 법인·차종 범위)

## 1. 현황
- `python -m mailhub mold check` (관련 메일 자동 연결 포함), `python -m mailhub mold list --json`

## 2. 처리할 메일
- 메일 번호가 주어졌으면 그 메일만, 아니면 `python -m mailhub mold inbox --json --days 60` + 진행 중 금형의 `✉새 메일`
- 각 메일을 `show <id> --json --max-chars 12000` 으로 읽는다 (첨부 엑셀·PDF 내용은 `att_text`, 첨부 파일명은 `attachments`).

## 3. 메일 → 항목 판단
각 메일이 **어느 금형(M번호)** 의 **어느 항목**인지 판단해서 표로 제안한다:
| 메일 | 금형 | 항목 | 내용 요약 | 제안 명령 |

- 금형 판단: 금형번호, 품번, 품명, 차종으로 `mold list --q ..` 와 맞춘다. 종합장에 없는 금형이면 등록을 먼저 제안:
  `python -m mailhub mold add --mail <메일> --mold-no .. --car .. --part-no .. --part-name .. --mold-vendor .. --dest .. [--cavity --tonnage --size --weight --hot-runner --resin]`
  (사양서에 캐비티·톤수·크기·중량·핫런너가 있으면 같이 채운다)
- 항목별 명령:
  - 금형사양서 / 성형조건표 / 부품리스트 / 기타 서류(도면·3D, 검사구·지그, 이관검수서 ...):
    `python -m mailhub mold doc <M번호> <항목> --mail <메일> --attach "<첨부파일 이름 일부>" [--version Rev1] [--status 확정]`
    (첨부를 data/molds/M번호/항목/ 에 보관, 자동 '접수'. 내용을 확인해 문제없으면 '확정' 제안, 수정 요청이 필요하면 '검토중' + `--note`)
  - KD리스트 / 원소재리스트(KD/LP) / 부품 리스트가 **엑셀·CSV 첨부**면 목록까지 불러온다:
    `python -m mailhub mold items <M번호> --type KD|원소재|부품 --mail <메일> --attach "<파일명 일부>"`
    불러온 뒤 `mold items <M번호> --type ..` 결과에서 KD/LP 구분이 비어 있거나 이상한 줄을 짚어준다.
  - 선적 일정(포워더·물류 메일, 인보이스/패킹리스트, B/L):
    `python -m mailhub mold ship <M번호> --plan .. --etd .. --eta .. [--atd .. --ata .. --bl .. --vessel .. --from 부산 --to .. --forwarder .. --container ..] --mail <메일>`
  - 상태 변화(현지 T/O 완료, 이관 완료 등): `python -m mailhub mold set <M번호> --status 현지T/O|이관완료 --note ".." --mail <메일>`
  - 한 메일에 여러 금형이 들어 있으면 금형별로 나눠서 제안한다 (선적 묶음, KD 통합 리스트 등).
- 사용자가 승인한 것만 실행한다.

## 4. 점검
- 선적(ETD) 2주 이내인데 미완료인 서류, 기한이 지난 서류, ETA 가 지났는데 도착 미확인인 금형을 따로 정리하고,
  필요한 요청 메일 초안(업체·현지 법인)을 `/mail-review` 형식(기한 포함)으로 제안한다. 보낸 뒤에는 `req add` 로 회신 추적.

## 5. 마무리
- 요약: 이번에 반영한 금형·항목, 서류 완료율, 다가오는 선적
- 엑셀: `python -m mailhub mold export data/MOLD_TRANSFER.xlsx --open`

기록을 바꾸는 명령(`mold add/set/doc/ship/items/link`)은 사용자 확인 후에만 실행한다. 메일은 보내지 않는다.
