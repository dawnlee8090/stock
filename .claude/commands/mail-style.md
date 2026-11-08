---
description: 내가 보낸 메일들에서 회신 말투·서명·자주 쓰는 표현을 뽑아 data/my_style.md 로 정리 (처음 1회, 이후 가끔 갱신)
---
내가 보낸 메일을 분석해서 회신 초안에 쓸 "내 말투 가이드"를 만들어줘. $ARGUMENTS

1. 보낸 메일을 상대별로 골고루 모은다 (각 명령 결과에서 서로 다른 건 위주로):
   - `python -m mailhub search --mine --limit 40 --json`
   - 고객사(현대·기아 등)에게: `python -m mailhub search --mine --to hyundai --limit 15 --json` (도메인/이름은 결과를 보고 조정)
   - 금형업체·검사구업체에게: `python -m mailhub search --mine --tag 금형수정 --limit 15 --json`, `--tag 검사구`
   - 사내에게: `python -m mailhub search --mine --to seoyoneh --limit 15 --json`
   - 대표적인 메일 8~12통은 `python -m mailhub show <id> --json --max-chars 3000` 으로 본문을 읽는다.
     (인용된 이전 메일 부분은 빼고, 내가 새로 쓴 부분만 본다)
2. 아래 항목을 정리해서 `data/my_style.md` 에 저장한다 (data 폴더는 저장소에 올라가지 않음):
   - 상대별(고객사 / 금형·검사구 업체 / 사내 / 해외법인·영문) 인사말과 첫 문장 패턴
   - 요청할 때, 기한을 줄 때, 독촉할 때, 사과·지연 안내할 때 자주 쓰는 표현 (실제 문장 예시 2~3개씩)
   - 맺음말, 서명 블록 (그대로 복사)
   - 문체 특징: 존댓말 수준, 번호 매기기/표 사용 습관, 문장 길이, 자주 쓰는 약어(E.O, T/O, C/F 등)
   - 쓰지 않는 표현이나 피해야 할 점
3. 마지막에 요약 5줄과 함께 "회신 초안은 이제 이 가이드를 따른다"고 알려준다.
메일 내용 자체(단가·도면 정보 등)는 가이드에 옮기지 말고, 말투와 형식만 뽑는다.
