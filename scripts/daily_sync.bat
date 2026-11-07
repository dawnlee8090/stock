@echo off
REM 매일 자동 수집 - Windows 작업 스케줄러에 등록해서 사용
REM 그룹웨어 메일 서버에서 새 메일만 가져옴 (읽음 표시 안 바뀜) + 원본 .eml 보관
REM 먼저 'python -m mailhub imap-setup' 으로 비밀번호를 저장해 두어야 함
chcp 65001 > nul
cd /d "%~dp0.."
if not exist data mkdir data
echo ===== %date% %time% ===== >> data\sync.log
python -m mailhub ingest-imap --save-eml --no-prompt >> data\sync.log 2>&1
python -m mailhub todo >> data\sync.log 2>&1
