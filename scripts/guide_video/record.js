// 개발 업무 비서 — 실사용 가이드 동영상 녹화 (가상 데이터 예시 화면)
// 실행: xvfb-run -a -s "-screen 0 1600x900x24" node scripts/guide_video/record.js <출력.mp4>
// 앱의 예시 모드(mailhub/web/index.html 을 파일로 열면 가상 데이터)로 화면을 자동 조작하고
// ffmpeg x11grab 으로 녹화한다. 실제 메일 데이터는 쓰지 않는다.
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const path = require('path');

const OUT = process.argv[2] || 'guide.mp4';
const APP = 'file://' + path.resolve(__dirname, '../../mailhub/web/index.html');
const W = 1600, H = 900;                         // 앱 화면 기본 크기 그대로 녹화
const wait = (ms) => new Promise(r => setTimeout(r, ms));

const OVERLAY_CSS = `
#gv-cap { position: fixed; left: 50%; bottom: 26px; transform: translateX(-50%); z-index: 9999; max-width: 1180px;
  background: rgba(10,20,36,.92); color: #fff; font: 600 22px/1.45 "IBM Plex Sans KR", sans-serif; padding: 12px 26px;
  border-radius: 12px; box-shadow: 0 8px 30px rgba(0,0,0,.35); text-align: center; transition: opacity .35s; opacity: 0; pointer-events: none; }
#gv-cap b { color: #ffd166; font-weight: 700; }
#gv-cap.on { opacity: 1; }
#gv-chap { position: fixed; right: 22px; top: 76px; z-index: 9999; background: #0e5a78; color: #fff; pointer-events: none;
  font: 700 15px/1 "IBM Plex Sans KR", sans-serif; padding: 9px 14px; border-radius: 999px; opacity: 0; transition: opacity .35s; }
#gv-chap.on { opacity: .95; }
#gv-badge { position: fixed; right: 16px; top: 12px; z-index: 9998; font: 500 11px "IBM Plex Sans KR", sans-serif;
  color: #8a94a3; pointer-events: none; }
#gv-cursor { position: fixed; z-index: 10001; width: 26px; height: 26px; margin: -4px 0 0 -4px; pointer-events: none; left: -50px; top: -50px;
  background: no-repeat center/contain url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath d='M4 2l15 11-7 1-3 7z' fill='%23111' stroke='%23fff' stroke-width='1.6' stroke-linejoin='round'/%3E%3C/svg%3E"); }
.gv-ring { position: fixed; z-index: 10000; width: 44px; height: 44px; margin: -22px 0 0 -22px; border-radius: 50%; pointer-events: none;
  border: 3px solid #ff5a5f; animation: gvr .6s ease-out forwards; }
@keyframes gvr { from { transform: scale(.3); opacity: 1; } to { transform: scale(1.4); opacity: 0; } }
.gv-hl { outline: 3px solid #ff5a5f !important; outline-offset: 3px; border-radius: 8px; transition: outline-color .3s; }
#gv-card { position: fixed; inset: 0; z-index: 10002; display: flex; flex-direction: column; justify-content: center; padding: 0 120px;
  background: radial-gradient(1200px 700px at 80% 20%, #16426a 0%, #0b1a2e 60%, #07111f 100%); color: #fff; opacity: 0; transition: opacity .5s; pointer-events: none;
  font-family: "IBM Plex Sans KR", sans-serif; }
#gv-card.on { opacity: 1; }
#gv-card .eyebrow { font: 700 16px/1 "IBM Plex Sans KR"; letter-spacing: .08em; color: #7cc4ff; margin-bottom: 20px; }
#gv-card h1 { font: 700 54px/1.25 "IBM Plex Sans KR"; margin: 0 0 18px; }
#gv-card h1 em { font-style: normal; color: #ffd166; }
#gv-card p { font: 400 23px/1.6 "IBM Plex Sans KR"; color: #c9d7e6; margin: 0; max-width: 1150px; }
#gv-card .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-top: 34px; max-width: 1300px; }
#gv-card .grid.g2 { grid-template-columns: repeat(2, 1fr); }
#gv-card .box { background: rgba(255,255,255,.06); border: 1px solid rgba(255,255,255,.14); border-radius: 14px; padding: 20px 22px; }
#gv-card .box .k { font: 600 15px "IBM Plex Sans KR"; color: #9fc3e6; margin-bottom: 8px; }
#gv-card .box .v { font: 700 30px/1.25 "IBM Plex Sans KR"; color: #fff; }
#gv-card .box .v small { font-size: 18px; color: #c9d7e6; font-weight: 500; }
#gv-card .box .d { font: 400 16px/1.5 "IBM Plex Sans KR"; color: #c9d7e6; margin-top: 6px; }
#gv-card .note { font-size: 15px; color: #8aa3bd; margin-top: 22px; }
#gv-card .steps { display: flex; gap: 12px; margin-top: 30px; flex-wrap: wrap; }
#gv-card .steps span { background: rgba(124,196,255,.14); border: 1px solid rgba(124,196,255,.35); border-radius: 999px; padding: 9px 18px; font: 600 18px "IBM Plex Sans KR"; }
#demo-bar, #sent-bar { display: none !important; }
`;

async function main() {
  const browser = await chromium.launch({
    headless: false,
    ignoreDefaultArgs: ['--enable-automation'],
    args: ['--kiosk', `--window-size=${W},${H}`, '--window-position=0,0',
      '--disable-infobars', '--hide-scrollbars', '--no-first-run', '--disable-features=Translate'],
  });
  const ctx = await browser.newContext({ viewport: null, locale: 'ko-KR' });
  const page = await ctx.newPage();
  page.setDefaultTimeout(8000);
  const cdp = await ctx.newCDPSession(page);   // 탭·주소창 없이 전체 화면
  const { windowId } = await cdp.send('Browser.getWindowForTarget');
  await cdp.send('Browser.setWindowBounds', { windowId, bounds: { windowState: 'fullscreen' } });
  await wait(800);
  await page.goto(APP);
  await page.waitForSelector('#nav a');
  await page.evaluate(() => document.fonts.ready);
  await page.addStyleTag({ content: OVERLAY_CSS });
  await page.evaluate(() => {
    for (const id of ['gv-cap', 'gv-chap', 'gv-badge', 'gv-cursor', 'gv-card']) { const d = document.createElement('div'); d.id = id; document.body.appendChild(d); }
    document.getElementById('gv-badge').textContent = '시연 화면 · 가상 데이터 (실제 회사 메일 아님)';
    const cur = document.getElementById('gv-cursor');
    addEventListener('mousemove', e => { cur.style.left = e.clientX + 'px'; cur.style.top = e.clientY + 'px'; }, true);
    addEventListener('mousedown', e => { const r = document.createElement('div'); r.className = 'gv-ring'; r.style.left = e.clientX + 'px'; r.style.top = e.clientY + 'px'; document.body.appendChild(r); setTimeout(() => r.remove(), 700); }, true);
  });

  // ---------- 연출 도구 ----------
  let mx = 800, my = 450;
  const cap = async (html, ms = 0) => { await page.evaluate(h => { const c = document.getElementById('gv-cap'); c.innerHTML = h; c.classList.toggle('on', !!h); }, html); if (ms) await wait(ms); };
  const chap = (t) => page.evaluate(t => { const c = document.getElementById('gv-chap'); c.textContent = t; c.classList.toggle('on', !!t); }, t);
  const card = async (html, ms) => {
    await page.evaluate(h => { const c = document.getElementById('gv-card'); c.innerHTML = h; c.classList.add('on'); }, html);
    await wait(ms);
    await page.evaluate(() => document.getElementById('gv-card').classList.remove('on'));
    await wait(550);
  };
  const box = async (sel) => { const el = page.locator(sel).first(); await el.scrollIntoViewIfNeeded(); return el.boundingBox(); };
  const moveTo = async (sel, dx = 0.5, dy = 0.5) => {
    const b = await box(sel); if (!b) throw new Error('not found: ' + sel);
    const x = b.x + b.width * dx, y = b.y + b.height * dy;
    const steps = Math.max(12, Math.round(Math.hypot(x - mx, y - my) / 22));
    await page.mouse.move(x, y, { steps }); mx = x; my = y; await wait(250);
  };
  const click = async (sel, after = 900, dx, dy) => { await moveTo(sel, dx, dy); await page.mouse.down(); await page.mouse.up(); await wait(after); };
  const hl = async (sel, ms) => {
    await page.evaluate(s => document.querySelector(s)?.classList.add('gv-hl'), sel);
    await wait(ms);
    await page.evaluate(s => document.querySelector(s)?.classList.remove('gv-hl'), sel);
  };
  const scrollEl = (sel, top) => page.evaluate(([s, t]) => document.querySelector(s)?.scrollTo({ top: t, behavior: 'smooth' }), [sel, top]);
  const closeDrawer = async () => { await click('#layer .drawer-h [data-close]', 600); };

  // ---------- 녹화 시작 ----------
  await page.mouse.move(mx, my);
  await page.evaluate(() => { document.getElementById('gv-card').classList.add('on'); });
  await wait(300);
  const ff = spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'x11grab', '-draw_mouse', '0', '-framerate', '30', '-video_size', `${W}x${H}`, '-i', process.env.DISPLAY,
    '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', OUT], { stdio: ['pipe', 'inherit', 'inherit'] });
  await wait(800);
  try { await scenes(); } finally {
    ff.stdin.write('q'); ff.stdin.end();
    await new Promise(r => ff.on('close', r));
    await browser.close();
  }

  async function scenes() {
  // 0. 표지
  await card(`<div class="eyebrow">E-NOVATION · AI 업무 생산성 · 실사용 가이드</div>
    <h1>개발 업무 비서<br><em>놓친 메일은 3초 안에, 회신 초안은 1분 안에</em></h1>
    <p>고객사 · 금형업체 · 검사구업체 · 현지 법인과 오가는 E.O · T/O · 금형 수정 · 금형 이관 메일을<br>내 PC 한 곳에 모아 찾고, 근거를 정리하고, 회신 초안까지 만드는 개발 담당자 전용 비서</p>`, 5200);

  // 1. 문제
  await card(`<div class="eyebrow">지금 개발 담당자의 하루</div>
    <h1>메일은 쌓이고, <em>중요한 건 묻힙니다</em></h1>
    <div class="grid">
      <div class="box"><div class="k">메일함 용량 제한</div><div class="v">수동 삭제 · 개인 PST</div><div class="d">지난 E.O · T/O 이력이 PC마다 흩어져<br>담당자가 바뀌면 이력이 끊김</div></div>
      <div class="box"><div class="k">키워드 검색의 한계</div><div class="v">첨부 안 내용은 못 찾음</div><div class="d">품번이 엑셀 · PPT 첨부 안에만 있으면<br>메일을 하나씩 열어 확인</div></div>
      <div class="box"><div class="k">회신 누락 · 지연</div><div class="v">기한 관리가 기억에 의존</div><div class="d">업체 회신 기한 · E.O 금형 수정 일정 ·<br>T/O 대책 기한이 엑셀 · 수첩에 분산</div></div>
    </div>`, 7500);

  // 2. 오늘 화면
  await chap('① 출근 후 첫 화면 · 오늘');
  await cap('출근하면 <b>[오늘]</b> 한 화면에서 회신할 메일 · 업체 기한 · E.O / T/O 지연 · 금형 이관 확인 건을 봅니다', 2600);
  await moveTo('.tiles .tile:nth-child(1)'); await hl('.tiles .tile:nth-child(1)', 1200);
  await moveTo('.tiles .tile:nth-child(3)'); await hl('.tiles .tile:nth-child(3)', 1200);
  await cap('<b>지금 처리할 일</b>은 급한 순서로 자동 정렬 — 몇 일 지연됐는지까지 표시됩니다', 600);
  await moveTo('.rows .row:nth-child(1)'); await wait(900);
  await moveTo('.rows .row:nth-child(4)'); await wait(1500);
  await cap('오른쪽 <b>기한 · 일정</b>에서 항목을 누르면, 그 건에 관련된 메일이 바로 열립니다', 400);
  const ag = '.ag-item:has-text("FR BUMPER UPR 검토 회신")';
  await click(ag, 1600);
  await cap('E.O 번호 · 품번으로 연결된 메일만 모아서 — <b>"그 메일 어디 있지?"</b>를 없앱니다', 3600);
  await closeDrawer();

  // 3. 놓친 메일 빠르게 찾기
  await chap('② 놓친 메일 · 첨부 속 내용까지 3초 검색');
  await cap('품번 하나만 입력하면 <b>서버 메일 + 아웃룩 PST 백업 + 저장된 메일</b>을 한 번에 검색합니다', 300);
  await click('#q', 300);
  await page.keyboard.type('86511', { delay: 140 }); await wait(300);
  await page.keyboard.press('Enter'); await wait(1600);
  await cap('품번이 들어간 메일이 <b>최신순</b>으로 — 이 경우 금형업체 수정 검토서 회신이 바로 나옵니다', 2800);
  await click('#q', 200); await page.keyboard.press('Control+A');
  await cap('메일 본문에 없고 <b>첨부 엑셀 안에만</b> 있는 글자도 찾습니다 (예: KD 리스트의 부품명)', 300);
  await page.keyboard.type('INSERT NUT', { delay: 120 }); await wait(300);
  await page.keyboard.press('Enter'); await wait(1300);
  await click('.mrow[data-id="30350"]', 1200);
  await click('#reader details.att summary', 900);
  await hl('#reader details.att', 2600);
  await cap('', 300);

  // 4. 회신 필요 메일 → 근거 · 검토 · 회신 초안
  await chap('③ 회신 필요 메일 · 근거 정리 · 회신 초안');
  await click('#q', 200); await page.keyboard.press('Control+A'); await page.keyboard.press('Backspace');
  await page.keyboard.press('Enter'); await wait(500);   // 검색어 지우기 (전체 메일)
  await cap('<b>[회신 필요]</b> 폴더에는 내가 받는 사람이고 요청 · 기한이 있는 메일만 자동으로 모입니다', 300);
  await click('a[href="#mail-todo"]', 1600);
  await click('.mrow[data-id="30412"]', 1500);
  await cap('고객사 품질 요청 메일 — 본문 · 첨부(PPT) 내용 · 품번 · 기한이 한 화면에', 2400);
  await cap('<b>✦ 검토 · 회신 초안</b> 한 번 누르면 AI가 메일과 첨부를 읽고 정리합니다', 300);
  await click('#ai-review', 500);
  await wait(3800);
  await page.waitForSelector('#ai-out .ai-panel', { timeout: 8000 });
  await cap('① 요약과 <b>회신 기한</b> ② 요청사항 · 검토 주체 · 기한 표 ③ 고객사 <b>회신 초안</b>', 1200);
  await scrollEl('#reader', 160); await wait(2600);
  await scrollEl('#reader', 420); await wait(2800);
  await moveTo('#ai-copy'); await hl('#ai-copy', 300);
  await cap('<b>전체 복사</b> → 그룹웨어 메일에 붙여 넣고 내용 확인 후 <b>본인이 직접 발송</b> (자동 발송 없음)', 300);
  await click('#ai-copy', 2600);
  await cap('처리 상태도 버튼 하나 — <b>처리 완료 · 진행중 · 업체 검토중</b>으로 표시해 누락을 막습니다', 300);
  await scrollEl('#reader', 0); await wait(700);
  await moveTo('#reader [data-mark="진행중"]'); await hl('#reader .actions', 2200);

  // 5. E.O 메일 → 자동 기입 → E.O 리스트
  await chap('④ E.O 변경 · 자동 기입 · 금형 수정 추적');
  await cap('E.O 통보 메일은 <b>✦ E.O 반영</b>을 누르면 E.O 번호 · 품번 · 변경 내용 · 기한이 자동으로 채워집니다', 300);
  await click('.mrow[data-id="30377"]', 1300);
  await click('#ai-eo', 2400);
  await cap('담당자는 확인만 하고 <b>[등록]</b> → E.O 리스트에 들어갑니다 (기록은 항상 사람이 확인 후)', 3200);
  await closeDrawer();
  await cap('<b>[E.O 리스트]</b> 접수 → 검토 → 금형 수정 → 검증 → 완료 단계와 지연 · 임박을 한눈에', 300);
  await click('a[href="#eo"]', 2400);
  await click('tr[data-id="3"]', 1400);
  await cap('E.O별 진행 이력 · <b>관련 메일 자동 연결</b> · 금형업체 요청 · 검토 기한을 한 곳에서 관리', 1800);
  await scrollEl('#layer .drawer-b', 600); await wait(2600);
  await closeDrawer();

  // 6. 업체 요청
  await chap('⑤ 업체 요청 · 회신 기한 추적');
  await cap('금형 · 검사구 업체와 현지 법인에 요청한 건은 <b>회신 기한 D-day</b>로 추적됩니다', 300);
  await click('a[href="#req"]', 2200);
  await moveTo('tr[data-id="8"] td:nth-child(5)'); await wait(1600);
  await cap('같은 제목으로 회신이 오면 <b>자동으로 회신받음</b>, 기한이 지나면 <b>재요청 메일</b>을 아웃룩 창으로 준비', 300);
  await moveTo('tr[data-id="8"] [data-act="remind"]'); await hl('tr[data-id="8"] [data-act="remind"]', 2800);

  // 7. T/O 문제점
  await chap('⑥ T/O 문제점 · 재발 감지 · 과거 대책 추천');
  await cap('<b>[T/O 문제점]</b> 불량 1가지 = 1건으로 발생 → 원인 → 대책 → 효과 확인 → 종결까지 관리', 300);
  await click('a[href="#to"]', 2600);
  await moveTo('tr[data-id="3"] td:nth-child(2)'); await hl('tr[data-id="3"] td:nth-child(2)', 1600);
  await cap('같은 부품 · 같은 불량이 다시 나오면 <b>재발</b>로 표시하고, 과거에 효과 본 대책을 함께 보여줍니다', 300);
  await click('tr[data-id="3"]', 1800);
  await scrollEl('#layer .drawer-b', 380); await wait(2800);
  await scrollEl('#layer .drawer-b', 900); await wait(2600);
  await cap('불량 유형별 <b>원인 · 대책 가이드</b> (조건 → 금형 → 설계 → 재질 순) — 신입 · 인수인계에도 그대로 활용', 3200);
  await closeDrawer();

  // 8. 금형 이관
  await chap('⑦ 금형 이관 · 현지 법인 서류 체크');
  await cap('<b>[금형 이관]</b> 금형별 사양서 · 성형조건표 · 부품 · KD · 원소재 리스트 · 선적 일정을 매트릭스로', 300);
  await click('a[href="#mold"]', 2800);
  await cap('선적(ETD) 전에 빠진 서류가 있으면 <b>"선적 D-10, 미완료: 성형조건표…"</b>처럼 미리 알려줍니다', 300);
  await click('tr[data-id="1"] td:first-child', 1200);
  await wait(2800);
  await closeDrawer();

  // 9. 오늘로 복귀
  await chap('');
  await cap('매일 아침 자동 수집 → <b>[오늘]</b> 화면 하나로 하루 업무 시작', 300);
  await click('a[href="#today"]', 2600);
  await cap('', 200);

  // 10. 효과
  await card(`<div class="eyebrow">기대 효과 (1인 기준 · 시범 적용으로 실측 예정)</div>
    <h1>하루 <em>약 1시간</em>, 한 달 <em>약 20시간</em> 절감</h1>
    <div class="grid">
      <div class="box"><div class="k">메일 · 첨부 검색</div><div class="v">8분 → 1분 <small>/건</small></div><div class="d">하루 3건 기준 약 20분 절감<br>첨부 속 품번 · KD 리스트까지 검색</div></div>
      <div class="box"><div class="k">검토 정리 · 회신 초안</div><div class="v">15분 → 5분 <small>/건</small></div><div class="d">하루 2건 기준 약 20분 절감<br>요청사항 표 · 근거 메일 번호 포함</div></div>
      <div class="box"><div class="k">E.O · T/O · 이관 대장 · 보고</div><div class="v">주 2시간 → 30분</div><div class="d">하루 약 18분 절감<br>지연 · 임박 자동 표시, 엑셀 내보내기</div></div>
    </div>
    <p class="note">※ 건당 소요 시간은 개발 담당자 업무 기준 가정치이며, 시범 운영 1개월 동안 실측해 보완합니다.</p>`, 9000);

  // 11. 보안 · 확산
  await card(`<div class="eyebrow">보안 원칙 · 적용 방안</div>
    <h1>회사 메일은 <em>내 PC 밖으로 나가지 않습니다</em></h1>
    <div class="grid g2">
      <div class="box"><div class="k">보안</div><div class="d">· 메일 DB는 내 PC에만 저장 (127.0.0.1 전용 화면)<br>· 자동 발송 없음 — 초안까지만, 발송은 본인 확인 후<br>· 대장 기록 변경도 담당자 확인 후 반영</div></div>
      <div class="box"><div class="k">확산</div><div class="d">· 1단계: 개발팀 시범 적용 (설치 10분, 추가 비용 없음)<br>· 2단계: 팀 공용 E.O · T/O 이력 DB로 인수인계 자산화<br>· 3단계: 사내 AI 플랫폼 · 권한 관리와 연계</div></div>
    </div>
    <div class="steps"><span>놓치지 않고</span><span>빠르게 찾고</span><span>근거로 회신하고</span><span>이력으로 남긴다</span></div>`, 8500);

  await card(`<div class="eyebrow">E-NOVATION 제안</div><h1>개발 업무 비서<br><em>중요 메일은 놓치지 않고, 필요한 이력은 바로 찾는다</em></h1>`, 3500);

  }
}
main().catch(e => { console.error(e); process.exit(1); });
