@echo off
REM 바탕화면 · 시작 메뉴에 '현지 금형 육성 관리' 앱 아이콘을 만듭니다 (처음 한 번 더블클릭)
REM 저장소 폴더를 옮겼으면 다시 한 번 실행하세요.
chcp 65001 > nul
cd /d "%~dp0.."
python -m localmold shortcut
echo.
pause
