# mailhub — 업무 메일 검색/검토 도우미

사용자는 자동차 내·외장 사출부품 개발 담당자다. 고객사·금형업체·검사구업체와 메일로
E.O 변경, T/O 문제점, 금형 수정, 검사구, 금형 이관(현지 법인), 일정을 조율한다.

## 메일 찾기 (항상 이 도구로 검색)
- `python -m mailhub search <검색어...> --json --limit 20`
  - 공백 = AND, `"정확한 구문"`, `-제외어`
  - 옵션: `--from 보낸사람`, `--since/--until YYYY-MM-DD`, `--tag E.O|T/O|금형수정|검사구|품질문제|금형이관|일정|설계반영`,
    `--part 품번`, `--attach 첨부파일명`, `--subject`(제목만), `--todo`(조치필요만), `--sort relevance`, `--group`(같은 건은 최신 1통만)
- `python -m mailhub show <id> --json --max-chars 8000` : 본문 전체 + `att_text`(첨부 엑셀/PPT/PDF 내용)
  - 검색 결과 snippet 이 `[첨부내용]` 으로 시작하면 첨부파일 안에서 찾은 것이다
- `python -m mailhub todo --json [--days 30] [--include-replied]` : 미회신 조치필요 메일
- `python -m mailhub mark <id> --status <상태> --note "<메모>"` : 상태 기록 (사용자 확인 후에만)
- `search --mine [--to 상대]` : 내가 보낸 메일 (회신 말투 참고). 말투 가이드는 `data/my_style.md` (`/mail-style` 로 생성)
- 업체 요청 추적: `req add --to 주소 --subject "보낸 제목" --due 11/12 [--vendor --type --mail]`,
  `req list [--all] [--json]`, `req check`(업체 회신 자동 확인), `req remind <R번호> [--preview]`(아웃룩 창에 재요청 메일),
  `req update <R번호> --status 완료|취소 --due ...`
- E.O 리스트: `eo list [--status open|all|상태] [--car] [--q] [--json]`, `eo show <E번호> [--json]`(이력·관련메일·업체요청),
  `eo inbox`(미등록 E.O 메일), `eo check`(새 메일 자동 연결 + 지연·임박), `eo export data/EO_LIST.xlsx`,
  `eo add --mail N --eo-no --car --part-no --part-name --desc ..`, `eo set <E번호> --status 접수|검토중|수정요청|수정중|수정완료|검증중|완료|수정불필요|보류|취소 [--note --mail --mold-due ...]`,
  `eo link <E번호> <메일번호...>`, 업체 요청을 E.O 에 연결: `req add ... --eo <E번호>`. 갱신 절차는 `/eo-update`
  참고 그림: `eo img <E번호> [--collect [--outlook]] [--paste --caption ..] [--add 파일] [--remove 번호]`,
  엑셀: `eo export data/EO_LIST.xlsx [--collect] [--open]` (목록 썸네일 + E번호별 상세 시트)
- T/O 문제점 대장: `to list [--status open|all] [--defect] [--q] [--json]`, `to show <T번호> [--json]`(재발 이력·관련메일·유사사례),
  `to similar <T번호>` 또는 `to similar --defect .. --part-name .. --resin ..`, `to guide <불량유형>`(원인·대책 가이드),
  `to inbox`, `to check`, `to add --defect .. --phenomenon .. --part-no ..`, `to set <T번호> --status 발생|원인분석|대책수립|대책적용|효과확인|종결|보류 ..`,
  `to img`, `to export data/TO_ISSUES.xlsx`. 절차는 `/to-update`, 개선 조언은 `/to-advice`
  T/O 운영 규칙은 `docs/TO_관리가이드.md`, 주간 점검은 `/to-weekly`
- 금형 이관 종합장: `mold list [--dest] [--q] [--json]`, `mold show <M번호> [--json]`, `mold inbox`, `mold check`,
  `mold add --mold-no .. --part-name .. --dest ..`, `mold doc <M번호> <금형사양서|성형조건표|부품리스트|선적일정|KD리스트|원소재리스트|기타> --mail N --attach "파일명 일부" [--status 확정]`,
  `mold items <M번호> --type KD|원소재|부품 --mail N --attach ..`(첨부 엑셀 불러오기), `mold ship <M번호> --etd .. --eta .. --atd .. --ata .. --bl ..`,
  `mold set <M번호> --status ..`, `mold export data/MOLD_TRANSFER.xlsx`. 메일 반영 절차는 `/mold-update`
- 주제별 이력 정리: `/mail-history <주제>` (예: CONSOLE 문제점, 품번) - 이력표·문제점별 현황·미결·다음 조치, 근거 #id.
  앱 [이력 정리] 화면: 검색 + 같은 건(스레드) 메일을 건별·날짜순으로, 메일 클릭 = 원본 팝업, 업체 메일 초안(틀 / Claude 다듬기) → Outlook 창·업체 요청 등록
- 결과가 적으면 동의어/영문 표기(E.O ↔ EO ↔ 설계변경, T/O ↔ TRY OUT ↔ 트라이)와 품번으로 다시 검색한다.

- 앱 화면: `python -m mailhub app` (mailhub/web/index.html + mailhub/webapp.py, 127.0.0.1 전용). 화면이나 API 를 바꾸면 두 파일의
  APP_VERSION 을 같이 올린다 (예전 엔진이 켜져 있으면 새 실행이 자동으로 교체). 화면을 바꾸면 같은 파일이
  아티팩트 예시(가상 데이터)로도 쓰이므로 예시 데이터(buildDemo)와 API 응답 형태를 함께 맞춘다.

## 원칙
- 메일 내용은 회사 기밀이다. 외부 서비스로 보내거나 저장소에 커밋하지 않는다 (`data/` 는 gitignore).
- 메일 발송/전달은 하지 않는다. 회신·전달은 **초안만** 작성하고 사용자가 직접 보낸다.
  자동 발송 기능은 없다. 재요청은 `req remind <R번호>`(아웃룩 창) 또는 `--ask`(확인 창)로만 하고, 발송은 사용자가 창에서 결정한다.
- `req add`, `mark`, `req update`, `eo add/set/link/import`, `to add/set/link/import`, `mold add/set/doc/ship/items/link` 처럼 기록을 바꾸는 명령은 사용자 확인 후 실행한다.
- 답변에는 근거 메일 번호(#id)와 날짜를 함께 적는다.
