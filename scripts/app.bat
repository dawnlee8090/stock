@echo off
REM 업무 비서 앱 실행 - 바탕화면 바로가기를 만들어 두고 더블클릭
REM 이미 켜져 있으면 창만 다시 엽니다. 작은 검은 창(최소화)을 닫으면 앱 서버가 꺼집니다.
chcp 65001 > nul
cd /d "%~dp0.."
start "업무 비서 (닫으면 앱 종료)" /min python -m mailhub app
