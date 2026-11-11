# 실사용 가이드 동영상 · 한 장 요약 이미지 만들기

앱의 예시 화면(가상 데이터)을 자동으로 조작하며 녹화합니다. 실제 메일 데이터는 쓰지 않습니다.
Linux(Xvfb) + Node Playwright + ffmpeg 가 필요합니다.

```bash
xvfb-run -a -s "-screen 0 1600x900x24" node scripts/guide_video/record.js guide.mp4
```

- `record.js` : 장면 순서(오늘 → 검색 → 회신 초안 → E.O → 업체 요청 → T/O → 금형 이관 → 효과·보안)와 자막
- `onepager.html` : 이노베이션 `내용` 칸에 넣을 한 장 요약 (같은 폴더에 `today.png` 캡처를 두고 1600x900 으로 캡처)
- 제안서 문안은 `docs/이노베이션_제안서.md`
