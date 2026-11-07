@echo off
REM 매일 자동 수집 - Windows 작업 스케줄러에 등록해서 사용
REM 최근 7일 메일만 다시 확인 (중복은 자동으로 건너뜀) + 원본 .msg 보관
chcp 65001 > nul
cd /d "%~dp0.."
for /f %%i in ('powershell -NoProfile -Command "(Get-Date).AddDays(-7).ToString('yyyy-MM-dd')"') do set SINCE=%%i
python -m mailhub ingest-outlook --since %SINCE% --save-msg >> data\sync.log 2>&1
python -m mailhub todo >> data\sync.log 2>&1
