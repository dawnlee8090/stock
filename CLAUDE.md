# mailhub — 업무 메일 검색/검토 도우미

사용자는 자동차 내·외장 사출부품 개발 담당자다. 고객사·금형업체·검사구업체와 메일로
E.O 변경, T/O 문제점, 금형 수정, 검사구, 금형 이관(현지 법인), 일정을 조율한다.

## 메일 찾기 (항상 이 도구로 검색)
- `python -m mailhub search <검색어...> --json --limit 20`
  - 공백 = AND, `"정확한 구문"`, `-제외어`
  - 옵션: `--from 보낸사람`, `--since/--until YYYY-MM-DD`, `--tag E.O|T/O|금형수정|검사구|품질문제|금형이관|일정|설계반영`,
    `--part 품번`, `--attach 첨부파일명`, `--subject`(제목만), `--todo`(조치필요만), `--sort relevance`
- `python -m mailhub show <id> --json --max-chars 8000` : 본문 전체
- `python -m mailhub todo --json [--days 30] [--include-replied]` : 미회신 조치필요 메일
- `python -m mailhub mark <id> --status <상태> --note "<메모>"` : 상태 기록 (사용자 확인 후에만)
- 결과가 적으면 동의어/영문 표기(E.O ↔ EO ↔ 설계변경, T/O ↔ TRY OUT ↔ 트라이)와 품번으로 다시 검색한다.

## 원칙
- 메일 내용은 회사 기밀이다. 외부 서비스로 보내거나 저장소에 커밋하지 않는다 (`data/` 는 gitignore).
- 메일 발송/전달은 하지 않는다. 회신·전달은 **초안만** 작성하고 사용자가 직접 보낸다.
- 답변에는 근거 메일 번호(#id)와 날짜를 함께 적는다.
