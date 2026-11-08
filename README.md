# mailhub — 업무 메일 통합 보관 · 검색 · 검토 자동화

회사 메일함 용량(5GB) 한계로 흩어진 **서버 메일 + 아웃룩 PST 백업 + 저장된 .msg/.eml** 을
내 PC 의 검색 DB 하나로 모으고, **E.O / T/O / 금형수정 / 검사구 / 금형이관** 등으로 자동 분류해서
몇 초 안에 찾고, Claude Code 로 **검토 · 업체 전달문 · 회신 초안**까지 만드는 도구입니다.

> 메일 데이터는 모두 내 PC 의 `data/` 폴더에만 저장되며 저장소(GitHub)에는 올라가지 않습니다.

---

## 단계별 자동화 로드맵

| 단계 | 내용 | 상태 |
|---|---|---|
| **1. 통합 보관 · 검색** | 아웃룩(서버+PST) / .msg / .eml → 로컬 DB, 중복 제거, 한글 전문검색, 원본 열기, 엑셀 내보내기 | ✅ 이번 버전 |
| **2. 자동 분류 · 할 일 관리** | 태그(E.O·T/O·금형수정·검사구·품질·이관·일정·설계반영), 품번 추출, ★조치필요 판정, 상태/메모 관리 | ✅ 이번 버전 (규칙 기반) |
| **3. Claude Code 검토 보조** | `/mail-search`, `/mail-review`, `/mail-todo` 로 이력 요약 · 요청사항 표 · 업체 전달문/회신 초안 | ✅ 이번 버전 (사람이 실행) |
| 4. 매일 자동 수집 | 작업 스케줄러로 매일 수집 + 아침에 할 일 목록 | ✅ 스크립트 제공 (`scripts/daily_sync.bat`) |
| 5. 첨부파일 내용 검색 | 엑셀/워드/PPT/PDF 첨부 안의 글자·품번까지 검색 (E.O 리스트, T/O 보고서) | ✅ `index-attachments` |
| 6-1. E.O 리스트 관리 | 접수→검토→금형수정→검증 단계·기한·지연 알림, 미등록 E.O 메일 표시, 관련 메일 자동 연결, 엑셀 | ✅ `eo`, `/eo-update` |
| 6-2. T/O 문제점·개선 이력 DB | 문제점→원인→대책→효과 이력, 재발 감지, 유사 사례, 불량별 대책 가이드, 조언 | ✅ `to`, `/to-update`, `/to-advice` |
| 6-3. 금형 이관 종합장 | 금형별 사양서·성형조건표·부품·선적·KD·원소재(KD/LP) 체크, 첨부 보관·엑셀 불러오기, 선적 전 알림 | ✅ `mold`, `/mold-update` |
| 7. 초안 자동 생성 → 아웃룩 임시보관함 | 새 메일 도착 시 검토 초안을 아웃룩 **임시보관함**에 만들어 두기 (발송은 본인이 확인 후) | ⏳ 예정 |

자동 발송은 의도적으로 하지 않습니다. 고객사/업체 대응은 **초안까지 자동, 발송은 사람 확인** 이 원칙입니다.

---

## 1. 설치 (Windows PC, 최초 1회)

1. Python 3.11 이상 설치 (python.org, 설치 시 *Add python.exe to PATH* 체크)
2. 이 폴더를 내려받은 뒤, 폴더에서 PowerShell 실행:
   ```powershell
   pip install -r requirements.txt
   copy config.example.toml config.toml
   ```
3. `config.toml` 열어서 `[me] addresses` 에 내 메일 주소, 고객사/금형업체/검사구업체 도메인을 입력

## 2. 메일 가져오기

아웃룩을 켜 둔 상태에서 실행합니다.

```powershell
# 아웃룩에 연결된 사서함/PST 목록 확인
python -m mailhub ingest-outlook --list

# 전체 가져오기 (서버 사서함 + 연결된 모든 PST). 처음 한 번은 오래 걸림
python -m mailhub ingest-outlook --save-msg

# 아웃룩에 안 붙어 있는 옛날 PST 도 추가
python -m mailhub ingest-outlook --pst "D:\메일백업\2023.pst" "D:\메일백업\2024.pst" --store 2023

# 폴더에 저장해 둔 .msg / .eml 파일
python -m mailhub ingest-files "D:\프로젝트\XX1\메일"

# 그룹웨어에서 내려받은 메일 zip 도 그대로 (원본은 data/eml/imported 에 보관)
python -m mailhub ingest-files "D:\메일백업\읽은메일.zip"
```

- 같은 메일이 서버와 PST 에 둘 다 있어도 **한 번만** 저장됩니다 (Message-ID 기준).
- `--save-msg` : 원본을 `data/msg/연/월/` 에 .msg 로 보관 → 서버에서 지워도 원본을 열 수 있어 **회사 메일함 용량을 비울 수 있습니다.**
- `--save-attachments` : 첨부파일도 `data/attachments/` 에 저장.
- 삭제된 항목 · 정크 메일 폴더는 건너뜁니다.

## 2-1. 그룹웨어 메일 서버에서 직접 가져오기 (권장, 매일 자동화용)

그룹웨어 웹메일을 쓰는 경우, 아웃룩 백업 없이 메일 서버(IMAP)에서 바로 가져옵니다.
**폴더를 읽기 전용으로 열고 미리보기 방식으로 가져오기 때문에 그룹웨어의 '안 읽음' 표시가 바뀌지 않습니다.**

1. 그룹웨어 **환경설정 → 메일 → POP3/IMAP 설정**에서 IMAP 사용을 켜고 서버 주소·포트를 확인
2. `config.toml` 의 `[imap]` 에 `host`, `user` 입력 (비밀번호는 적지 않음)
3. 접속 확인 + 비밀번호 저장 (Windows 자격 증명 관리자에 저장됨):
   ```powershell
   python -m mailhub imap-setup
   ```
4. 첫 수집 (PST 로 이미 가져온 기간은 Message-ID 로 자동 중복 제거):
   ```powershell
   python -m mailhub ingest-imap --since 2026-10-01 --save-eml
   ```
5. 이후에는 옵션 없이 실행하면 **새 메일만** 가져옵니다: `python -m mailhub ingest-imap --save-eml`

받은 메일과 **보낸 메일**이 함께 들어오므로 "이미 회신한 건"이 할 일 목록에서 자동으로 빠집니다.

## 3. 검색

```powershell
python -m mailhub search 크래쉬패드 E.O                 # 공백 = 그리고(AND)
python -m mailhub search "게이트 위치" -견적             # "구문" 정확히, -제외어
python -m mailhub search 웰드 --tag T/O --since 2026-01-01
python -m mailhub search --part 84710-AA000              # 품번 이력 전체
python -m mailhub search 보스 높이 --group                # 같은 건(RE:/FW:)은 최신 1통만
python -m mailhub search --from 금형업체 --has-attach
python -m mailhub search --attach "EO LIST"              # 첨부파일명으로
python -m mailhub search 검사구 --csv 검사구메일.csv     # 엑셀로 저장
python -m mailhub show 1234                              # 내용 보기
python -m mailhub open 1234                              # 아웃룩(또는 .msg)으로 원본 열기
python -m mailhub stats                                  # 보관 현황
```

## 4. 할 일(조치필요) 관리

내가 **받는사람(To)** 에 있고, 제목/최신 본문에 "검토 부탁, 요청, 긴급…" 등이 있으면 ★조치필요로 표시됩니다.
참조(CC)로만 받은 메일, 같은 건에 **내가 이미 회신한 메일**은 할 일 목록에서 자동으로 빠집니다 (`config.toml` 의 `[me]` 에 주소·이름 입력 필요).

```powershell
python -m mailhub todo                                   # 최근 30일 미회신 조치필요 메일
python -m mailhub todo --days 90 --include-replied       # 기간 늘리기, 회신한 건도 보기
python -m mailhub mark 1234 --status 업체검토중 --note "금형업체 11/15 회신 예정"
python -m mailhub mark 1234 1240 --status 완료
python -m mailhub mark 1300 --action 0                   # 조치 불필요로 변경
python -m mailhub search --todo --tag E.O                # E.O 관련 미결
```

상태 예시: `진행중` → `업체검토중` → `설계반영` → `완료` / `보류` / `제외`

## 5. Claude Code 로 검토 · 초안 작성

이 폴더에서 Claude Code 를 열고:

| 명령 | 하는 일 |
|---|---|
| `/mail-search 84710 크래쉬패드 E.O 경과` | 관련 메일을 찾아 시간순 경과 · 미결 사항 정리 |
| `/mail-review 1234` | 요청사항 표, 검토 주체, 금형업체/검사구업체 전달문, 고객사 회신 초안, 설계 반영 가능 여부 |
| `/mail-todo` | 미처리 메일 우선순위 정리 (오늘 할 일) |

자연어로 물어봐도 됩니다: *"작년 XX1 도어트림 T/O 때 싱크 문제 어떻게 대책했었지?"*

## 6. 매일 자동 수집

`scripts/daily_sync.bat` 을 Windows **작업 스케줄러**에 등록 (예: 매일 08:00 / 12:00 / 17:00).
그룹웨어 메일 서버에서 새 메일만 가져오고 `data/sync.log` 에 할 일 목록을 남깁니다 (먼저 `imap-setup` 필요).

## 7. 첨부파일 내용 검색

새로 수집되는 메일(POP3/IMAP/.eml/.msg)은 첨부 내용이 **자동으로** 검색됩니다.
이미 들어있는 메일은 한 번 추출해 주세요 (중간에 멈춰도 다시 실행하면 이어서 진행):

```powershell
python -m mailhub index-attachments              # 원본(.eml) 보관된 메일
python -m mailhub index-attachments --outlook    # 아웃룩(PST) 메일 - 아웃룩 켠 상태에서
python -m mailhub retag                          # 첨부 안의 품번을 --part 검색에 반영
```

- 대상: `.xlsx .xlsm .docx .pptx .pdf .txt .csv` (옛날 형식 xls/doc/ppt, 이미지, 도면 파일은 파일명으로만 검색)
- 검색 결과에서 본문이 아니라 첨부에서 찾은 경우 `[첨부내용]` 으로 표시되고, `show 번호` 아래쪽에 첨부 내용이 나옵니다.

## 8. 업체 요청 추적 (기한 · 회신 자동 확인 · 알림 · 재요청)

금형 수정 검토서, 수정 진행, 검사구 수정 등 업체에 요청한 건을 기한과 함께 등록하면:
- 매일 자동 수집 때 그 업체에서 **같은 제목(RE:)으로 회신이 오면 자동으로 '회신받음'** 처리
- 기한이 지났거나 오늘·내일인 건은 **윈도우 알림**
- 기한이 지난 건은 매일 아침 **확인 창**이 뜨고, 재요청 메일 내용을 고친 뒤 직접 결정
  **[발송]**(한 번 더 확인 후 발송) · **[아웃룩에서 열기]** · **[기한 연장 +2영업일]** · **[건너뛰기]**(다음 날 다시 물음)
- 자동 발송은 없습니다.

```powershell
python -m mailhub req add --to kim@abc-mold.co.kr --vendor ABC정밀 --subject "BC4t CUV 범퍼 보스 높이 금형수정 검토 요청" --due 11/12 --type 금형수정검토
python -m mailhub req list                 # 진행 중인 요청 (D-day)
python -m mailhub req check --notify       # 회신 확인 + 알림 (매일 자동 실행됨)
python -m mailhub req remind 3 --preview   # 재요청 메일 미리보기
python -m mailhub req remind 3             # 아웃룩 새 메일 창에 띄우기
python -m mailhub req remind 3 --ask       # 확인 창 (내용 수정 → 발송/아웃룩/연장/건너뛰기)
python -m mailhub req check --ask          # 기한 지난 건 전부 하나씩 확인 창 (매일 자동 실행됨)
python -m mailhub req update 3 --status 완료
```

- `--due` 는 `11/12`, `2026-11-12`, `+3`(3일 뒤), `+3영업일` 형식.
- `--subject` 는 업체에 **실제로 보낸 메일 제목**과 같아야 회신을 자동으로 찾습니다.
- 재요청 문구·서명은 `config.toml` 의 `[me] intro / signature`, `[reminder]` 에서 설정.

## 9. E.O 리스트 관리

품번(부품)별로 E.O 를 등록하고 **접수 → 검토중 → 수정요청 → 수정중 → 수정완료 → 검증중 → 완료**
(또는 수정불필요 / 보류 / 취소) 단계로 관리합니다. 단계가 바뀌면 날짜와 이력이 자동으로 남습니다.

```powershell
# 등록 (E.O 메일 번호를 같이 주면 그 메일이 연결됨)
python -m mailhub eo add --mail 19162 --eo-no EO26-1234 --car "BC4t CUV" --part-no 86511-BN200 --part-name "FR BUMPER UPR" --desc "스크류 보스 높이 2mm 상향" --review-due 11/12

# 진행
python -m mailhub eo set 3 --status 수정요청 --mold-vendor ABC정밀 --mold-action 육성 --mold-due 11/25 --note "수정비 협의 중"
python -m mailhub eo set 3 --status 수정완료
python -m mailhub eo set 3 --status 완료 --note "T/O 치수 OK"

python -m mailhub eo list                    # 진행 중 (⚠지연 ⏰임박 ✉새 관련메일)
python -m mailhub eo show 3                  # 이력 + 관련 메일 + 업체 요청
python -m mailhub eo inbox                   # 리스트에 아직 없는 E.O 메일 (누락 방지)
python -m mailhub eo img 3 --collect         # 관련 메일의 도면 캡처·PPT 슬라이드·엑셀 그림 모으기
python -m mailhub eo img 3 --paste --caption "보스 단면"   # Win+Shift+S 캡처를 바로 붙여넣기
python -m mailhub eo img 3 --remove 2         # 필요 없는 그림 빼기
python -m mailhub eo export data\EO_LIST.xlsx --collect --open   # 그림까지 모아서 엑셀로
```

- **누락 방지**: E.O 태그 메일 중 리스트에 연결 안 된 메일은 `eo inbox` 와 매일 확인에 표시됩니다.
- **관련 메일 자동 연결**: 등록된 E.O 의 번호·품번이 나오는 메일, 연결된 메일의 회신(RE:/FW:)은 자동으로 연결되고 `✉` 로 표시됩니다.
- **지연 알림**: 현재 단계의 기한(검토 회신 / 금형 수정 / 검증)이 지나면 매일 아침 알림.
- **업체 요청 연결**: `req add ... --eo 3` 으로 등록하면 업체 회신이 오는 즉시 E.O 이력에 "업체 회신 도착"이 남습니다.
- **Claude Code** `/eo-update`: 미등록 E.O 메일과 첨부 E.O LIST 를 읽어 품번별 등록안, 새 메일 기준 상태 변경안을 표로 제안 → 확인한 것만 반영.
- **엑셀 구성**: `E.O LIST`(그룹 머리글·상태 색·지연 강조·대표 그림 썸네일·내용에 맞춘 열/행 크기, No 클릭 시 상세로 이동),
  `요약`(상태별·차종별·지연 목록), `E번호` 상세 시트(기본 정보·일정·진행 이력·관련 메일·참고 그림 크게).
- **참고 그림 출처**: 메일 본문에 붙여 넣은 캡처, 첨부 이미지, 첨부 PPT(파워포인트가 있으면 슬라이드 통째로, 없으면 PPT 안의 그림),
  첨부 엑셀·워드 안의 그림. 작은 로고·아이콘은 자동 제외, 같은 그림은 한 번만.
- 기존 엑셀 E.O 리스트가 있으면 `eo export x.csv` 와 같은 열 이름으로 맞춰 CSV(UTF-8) 저장 후 `eo import 파일.csv`.

## 10. T/O 문제점 · 개선 이력

T/O 에서 나온 문제점을 **불량 1가지 = 1건**으로 등록하고 **발생 → 원인분석 → 대책수립 → 대책적용 → 효과확인 → 종결** 로 관리합니다.

```powershell
python -m mailhub to add --car "BC4t CUV" --part-no 86511-BN200 --part-name "FR BUMPER UPR" --stage T2 --date 10/28 --resin PP-TD20 --defect 변형 --location "상단 플랜지" --phenomenon "휨 1.2mm (규격 ±0.5)" --due 11/5
python -m mailhub to set 2 --status 대책수립 --cause-cat 금형 --cause "고정측 냉각 부족" --action-cat 금형수정 --cm "냉각 채널 추가 + 역변형 0.8mm" --verify-due 11/20
python -m mailhub to set 2 --status 종결 --result "T3 휨 0.3mm OK" --effective 유효

python -m mailhub to list                       # 진행 중 (⚠지연 ⏰임박 ↻재발 ✉새 메일)
python -m mailhub to show 2                     # 상세 + 비슷한 과거 사례
python -m mailhub to similar --defect 싱크 --part-name "BRKT BPR" --resin PP-TD20   # 과거 사례 검색
python -m mailhub to guide 웰드                  # 불량 유형별 원인·대책 가이드
python -m mailhub to export data\TO_ISSUES.xlsx --collect --open   # 문제점 대장 엑셀 (사진·불량 통계)
```

- **불량 유형 자동 표준화**: "SINK", "싱크마크", "함몰" → `싱크` 처럼 21개 표준 유형으로 묶어 통계·검색이 됩니다.
- **재발 감지**: 같은 품번(LH/RH 포함)이나 같은 품명에서 같은 불량이 다시 등록되면 `↻재발` 표시와 함께 **이전 원인·대책·효과**를 보여줍니다.
- **유사 사례**: 불량 유형·품번·품명·재질·부위가 비슷한 과거 사례를 점수순으로, **효과가 확인된 대책**을 우선 보여줍니다.
- **Claude Code** `/to-update`: T/O 결과 메일·보고서에서 문제점 등록안, 새 메일 기준 원인·대책·결과 갱신안 제안.
  `/to-advice 2`: 과거 개선 이력 + 유사 사례 + 가이드를 근거로 **조건 → 금형 → 설계 → 재질 순 개선안**(비용·부작용·검증 방법 포함).
- 운영 규칙(입력·종결 기준, 재발 대응): [docs/TO_관리가이드.md](docs/TO_관리가이드.md), 주간 점검: Claude `/to-weekly`
- 엑셀: `TO 문제점`(사진 썸네일, 재발 강조) · `불량 통계`(유형·원인·금형업체·성형처별 건수, 재발, 지연) · `T번호` 상세(이전 발생 비교, 이력, 사진).

## 11. 금형 이관 종합장

현지 법인으로 이관하는 금형별로 **금형사양서 · 성형조건표 · 부품리스트 · 선적일정 · KD리스트 · 원소재리스트(KD/LP)** 를
체크리스트로 관리합니다 (● 확정 ◐ 접수·검토중 ○ 요청 ✕ 미접수 － 해당없음). 메일로 받을 때마다 하나씩 반영합니다.

```powershell
python -m mailhub mold add --mold-no MD-001 --car "BC4t CUV" --part-no 86511-BN200 --part-name "FR BUMPER UPR" --mold-vendor A금형 --dest 멕시코

# 메일로 받은 서류: 첨부를 data\molds\M1\항목\ 에 보관하고 자동 '접수'
python -m mailhub mold doc 1 금형사양서 --mail 21345 --attach 사양서
python -m mailhub mold doc 1 금형사양서 --status 확정 --version Rev1

# KD / 원소재 / 부품 리스트: 첨부 엑셀을 바로 불러오기 (머리글 자동 인식, KD/LP 구분)
python -m mailhub mold items 1 --type KD --mail 21350 --attach KD
python -m mailhub mold items 1 --type 원소재 --file "D:\자료\원소재 리스트.xlsx"

# 선적: 출항(--atd) 입력 시 상태 '선적', 도착(--ata) 시 '현지도착' 자동
python -m mailhub mold ship 1 --etd 11/18 --eta 12/20 --from 부산 --to Manzanillo --bl HDMU1234567

python -m mailhub mold list                  # 금형별 서류 체크·선적·확인 필요
python -m mailhub mold export data\MOLD_TRANSFER.xlsx --open
```

- **확인 필요 알림** (매일 아침): 선적 2주 이내인데 미완료 서류, 서류 기한 지남, ETD 지났는데 출항 미확인, ETA 지났는데 도착 미확인.
- **누락 방지**: 금형이관 메일 중 종합장에 연결 안 된 메일은 `mold inbox`, 금형번호·품번·같은 건 회신은 자동 연결.
- 기본 6개 외 항목도 이름만 주면 추가됩니다: `mold doc 1 도면·3D --mail ..`, `mold doc 1 검사구·지그 --status 해당없음`
- **엑셀**: `이관 종합`(금형별 체크 매트릭스, 칸 클릭 시 보관 파일 열림) · `선적 일정`(ETD 순, D-day) ·
  `부품/KD/원소재 리스트`(전 금형 통합, KD/LP 색 구분) · `M번호` 상세(사양·서류·선적·이력).
- **Claude Code** `/mold-update`: 받은 메일이 어느 금형의 어느 항목인지 판단해서 반영 명령을 제안 (승인한 것만 실행).

## 12. 분류 기준 수정

`config.toml` 의 `[tags]`, `[action]`, `[part_number]` 를 고친 뒤:
```powershell
python -m mailhub retag
```

---

### 참고
- 검색: SQLite FTS5(trigram) 전문검색. 3글자 이상 단어는 인덱스로 즉시, 2글자 단어(예: `금형`)는 전체 스캔(수만 건도 1~2초).
- 아웃룩 연동은 Windows + 아웃룩 데스크톱 + `pywin32` 필요. `.eml/.msg` 가져오기와 검색은 OS 무관.
- 테스트: `python -m unittest discover -s tests`
