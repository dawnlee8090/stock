@echo off
REM 현지 금형 육성 관리 앱 실행 - 바탕화면 아이콘(python -m localmold shortcut)이 이 파일을 실행합니다
REM 이미 켜져 있으면 창만 다시 엽니다. 작은 검은 창(최소화)을 닫으면 앱 서버가 꺼집니다. 업무 비서와 따로 켜고 끕니다.
REM 켜지지 않으면 이유를 메모장으로 보여 줍니다 (아이콘은 최소화로 실행되어 검은 창의 메시지가 보이지 않으므로).
chcp 65001 > nul
cd /d "%~dp0.."
set "PYTHONIOENCODING=utf-8"
set "LM_MSG=%TEMP%\localmold_check.txt"
if not exist "localmold\__main__.py" goto nofiles
python -c "import localmold.webapp" > "%LM_MSG%" 2>&1
if errorlevel 1 goto broken
start "현지 금형 (닫으면 앱 종료)" /min python -m localmold app
exit /b 0

:nofiles
> "%LM_MSG%" echo [현지 금형 육성 관리] 앱 파일이 이 폴더에 없어서 켜지 못했습니다.
>> "%LM_MSG%" echo 폴더: %CD%
>> "%LM_MSG%" echo.
>> "%LM_MSG%" echo 업무 비서 폴더를 다른 브랜치로 바꾸면 현지 금형 앱 파일이 빠집니다.
>> "%LM_MSG%" echo 해결: 받아 둔 현지금형_앱_설치.bat 를 다시 더블클릭하세요. 브랜치를 바꿀지 묻는 창에서 [예].
>> "%LM_MSG%" echo 또는 이 폴더에서: git checkout claude/awesome-planck-4zwz73 다음 git pull
start "" notepad "%LM_MSG%"
exit /b 1

:broken
>> "%LM_MSG%" echo.
>> "%LM_MSG%" echo [현지 금형 육성 관리] 앱을 켜지 못했습니다. 위 내용을 캡처해서 Claude 에게 보내 주세요.
>> "%LM_MSG%" echo 폴더: %CD%
>> "%LM_MSG%" echo python 이 없다는 내용이면 업무 비서를 실행하는 PC 인지 확인해 주세요.
start "" notepad "%LM_MSG%"
exit /b 1
