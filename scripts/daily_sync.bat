@echo off
REM 매일 자동 수집 - Windows 작업 스케줄러에 등록해서 사용
REM 그룹웨어 메일 서버에서 POP3 미리보기로 새 메일만 가져옴 (안 읽음 표시 유지, 서버 메일 삭제 안 함)
REM 먼저 'python -m mailhub pop-test' 를 한 번 실행해 비밀번호가 저장되어 있어야 함
REM IMAP 권한이 생기면 아래 ingest-pop 줄을 ingest-imap 으로 바꾸면 보낸 메일도 수집됨
chcp 65001 > nul
cd /d "%~dp0.."
if not exist data mkdir data
echo ===== %date% %time% ===== >> data\sync.log
python -m mailhub ingest-pop --save-eml --no-prompt >> data\sync.log 2>&1
python -m mailhub req check --notify --ask >> data\sync.log 2>&1
python -m mailhub todo >> data\sync.log 2>&1
