@echo off
REM Local mold app installer: double-click once. Creates the desktop / Start menu icon.
chcp 65001 > nul
set "LM_SELF=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -STA -Command "$s = Get-Content -LiteralPath $env:LM_SELF -Encoding UTF8 -Raw; Invoke-Expression $s.Substring($s.LastIndexOf('#PS-START'))"
echo.
pause
exit /b

#PS-START
# 현지 금형 육성 관리 - 바탕화면 앱 설치 (아래는 PowerShell)
# 1) 업무 비서(stock) 폴더 찾기: 이 파일 위치 → 바탕화면 · 시작 메뉴 · 작업 표시줄의 업무 비서 바로가기 → 흔한 위치 → 직접 고르기
# 2) 현지 금형 앱 파일이 없으면 최신 브랜치로 바꾸기 ([예]/[아니오] 창으로 물음. 한글 입력 상태와 상관없음)
# 3) python -m localmold shortcut 으로 바탕화면 · 시작 메뉴 아이콘을 만들고 앱 열기
# 진행 기록은 %TEMP%\localmold_install_log.txt. 중간에 멈추면 이유를 창으로 알리고 기록을 메모장으로 연다.
$BRANCH = 'claude/awesome-planck-4zwz73'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
Add-Type -AssemblyName System.Windows.Forms

$LOG = Join-Path $env:TEMP 'localmold_install_log.txt'
Set-Content -LiteralPath $LOG -Value ('현지 금형 앱 설치 기록 ' + (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')) -Encoding UTF8

function Say([string]$t) {
    Write-Host $t
    Add-Content -LiteralPath $LOG -Value $t -Encoding UTF8
}
function Show-Box([string]$text, [string]$buttons, [string]$icon) {
    $owner = New-Object System.Windows.Forms.Form
    $owner.TopMost = $true
    $r = [System.Windows.Forms.MessageBox]::Show($owner, $text, '현지 금형 앱 설치', $buttons, $icon)
    $owner.Dispose()
    return $r
}
function Ask([string]$text) {
    Say "[질문] $text"
    $yes = ((Show-Box $text 'YesNo' 'Question') -eq [System.Windows.Forms.DialogResult]::Yes)
    if ($yes) { Say '  → 예' } else { Say '  → 아니오' }
    return $yes
}
function Fail([string]$text) {
    Say ''
    Say "[중단] $text"
    [void](Show-Box ($text + "`n`n진행 기록을 메모장으로 엽니다. 캡처해서 Claude 에게 보내 주세요.") 'OK' 'Warning')
    Start-Process -FilePath 'notepad.exe' -ArgumentList ('"' + $LOG + '"')
    exit 1
}
function Invoke-Git([string[]]$gitArgs) {
    Say ('> git ' + ($gitArgs -join ' '))
    $out = & git -C $root @gitArgs 2>&1 | ForEach-Object { "$_" }
    $code = $LASTEXITCODE
    if ($out) { Say (($out | Out-String).TrimEnd()) }
    return $code
}
function Test-Root([string]$d) {
    if (-not $d) { return $false }
    return (Test-Path -LiteralPath (Join-Path $d 'mailhub\__main__.py'))
}

Say ''
Say '=== 현지 금형 육성 관리 - 바탕화면 앱 설치 ==='
Say ''

# ---- 1) 폴더 찾기 ----
$cands = New-Object System.Collections.ArrayList
$self = Split-Path -Parent $env:LM_SELF
[void]$cands.Add($self)
[void]$cands.Add((Split-Path -Parent $self))

$sh = New-Object -ComObject WScript.Shell
$lnkDirs = @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('CommonDesktopDirectory'),
             [Environment]::GetFolderPath('Programs'),
             (Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar'))
foreach ($d in $lnkDirs) {
    if (-not $d -or -not (Test-Path -LiteralPath $d)) { continue }
    foreach ($f in (Get-ChildItem -LiteralPath $d -Filter '*.lnk' -ErrorAction SilentlyContinue)) {
        try {
            $l = $sh.CreateShortcut($f.FullName)
            $t = ($l.TargetPath + ' ' + $l.Arguments)
            if ($t -match '([A-Za-z]:\\[^"]*?)\\scripts\\(app|localmold|daily_sync)\.bat') { [void]$cands.Add($Matches[1]) }
            elseif ($l.WorkingDirectory) { [void]$cands.Add($l.WorkingDirectory) }
        } catch { }
    }
}
foreach ($b in @($env:USERPROFILE, [Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('MyDocuments'), 'C:\', 'D:\')) {
    if ($b -and (Test-Path -LiteralPath $b)) { [void]$cands.Add((Join-Path $b 'stock')) }
}

$root = $null
foreach ($c in $cands) { if (Test-Root $c) { $root = (Resolve-Path -LiteralPath $c).Path; break } }

if (-not $root) {
    Say '업무 비서 폴더를 찾지 못했습니다. 폴더를 고르는 창을 엽니다.'
    $owner = New-Object System.Windows.Forms.Form
    $owner.TopMost = $true
    $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
    $dlg.Description = '업무 비서(mailhub)가 들어 있는 폴더를 골라 주세요 (예: stock)'
    if ($dlg.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
        $pick = $dlg.SelectedPath
        if (Test-Root $pick) { $root = $pick }
        elseif (Test-Root (Join-Path $pick 'stock')) { $root = (Join-Path $pick 'stock') }
    }
    $owner.Dispose()
}
if (-not $root) { Fail '업무 비서 폴더(안에 mailhub 폴더가 있는 곳)를 찾지 못했습니다. 이 파일을 업무 비서 폴더(stock) 안에 넣고 다시 더블클릭해 주세요.' }
Say "업무 비서 폴더: $root"
Set-Location -LiteralPath $root

# ---- 2) 현지 금형 앱 파일 확인 · 최신 브랜치로 ----
$ready = { Test-Path -LiteralPath (Join-Path $root 'localmold\shortcut.py') }
if (-not (& $ready)) {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail 'git 이 없어 최신 파일을 받을 수 없습니다. 업무 비서를 받을 때 쓴 방법으로 최신 파일을 받아 주세요.' }
    if (-not (Test-Path -LiteralPath (Join-Path $root '.git'))) { Fail '이 폴더는 git 으로 받은 폴더가 아니어서 자동으로 받을 수 없습니다.' }
    $cur = ((& git -C $root rev-parse --abbrev-ref HEAD 2>&1) | Out-String).Trim()
    Say "지금 브랜치: $cur"
    if ($cur -ne $BRANCH) {
        $q = "지금 브랜치($cur)에는 현지 금형 앱이 없습니다.`n`n앱이 있는 최신 브랜치로 바꿀까요?`n$BRANCH`n(업무 비서의 최신 기능도 모두 들어 있습니다)"
        if (-not (Ask $q)) { Fail '브랜치를 바꾸지 않아 설치를 멈췄습니다.' }
    }
    if ((Invoke-Git @('fetch', 'origin', $BRANCH)) -ne 0) { Fail 'GitHub 에서 최신 파일을 받지 못했습니다. 인터넷 연결과 GitHub 로그인을 확인해 주세요.' }
    if ($cur -ne $BRANCH) {
        if ((Invoke-Git @('checkout', $BRANCH)) -ne 0) {
            $chg = ((& git -C $root status --short 2>&1) | Select-Object -First 15 | Out-String).Trim()
            Say "PC 에서 바뀐 파일:`n$chg"
            $q = "PC 에서 고친 파일이 있어 브랜치를 바꾸지 못했습니다:`n`n$chg`n`n고친 내용을 잠시 보관(git stash)하고 계속할까요?`n(나중에 git stash pop 으로 되돌릴 수 있습니다)"
            if (-not $chg -or -not (Ask $q)) { Fail '브랜치를 바꾸지 못했습니다.' }
            if ((Invoke-Git @('stash', 'push', '-u', '-m', 'localmold-install')) -ne 0) { Fail '고친 파일을 보관하지 못했습니다.' }
            if ((Invoke-Git @('checkout', $BRANCH)) -ne 0) { Fail '브랜치를 바꾸지 못했습니다.' }
        }
    }
    if ((Invoke-Git @('merge', '--ff-only', "origin/$BRANCH")) -ne 0) { Say '(최신으로 맞추지 못했지만 계속합니다)' }
    if (-not (& $ready)) { Fail '브랜치는 바꿨지만 현지 금형 앱 파일이 보이지 않습니다.' }
    Say '현지 금형 앱 파일 준비 완료'
}

# ---- 3) 아이콘 만들기 · 앱 열기 ----
$exe = $null
$pyArgs = @()
if (Get-Command python -ErrorAction SilentlyContinue) { $exe = 'python' }
elseif (Get-Command py -ErrorAction SilentlyContinue) { $exe = 'py'; $pyArgs = @('-3') }
if (-not $exe) { Fail 'python 을 찾지 못했습니다. 업무 비서를 실행하는 PC 에서 다시 실행해 주세요.' }
$env:PYTHONIOENCODING = 'utf-8'
Say "> $exe -m localmold shortcut"
$out = & $exe @pyArgs -m localmold shortcut 2>&1 | ForEach-Object { "$_" }
$code = $LASTEXITCODE
if ($out) { Say (($out | Out-String).TrimEnd()) }
if ($code -ne 0) { Fail '바탕화면 아이콘을 만들지 못했습니다.' }

$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) '현지 금형 육성 관리.lnk'
Say ''
Say '완료: 바탕화면 · 시작 메뉴에 [현지 금형 육성 관리] 아이콘을 만들었습니다.'
[void](Show-Box "바탕화면 · 시작 메뉴에 [현지 금형 육성 관리] 아이콘을 만들었습니다.`n`n[확인]을 누르면 앱을 엽니다.`n(끌 때는 작업 표시줄의 작은 검은 창 [현지 금형]을 닫으세요)" 'OK' 'Information')
if (Test-Path -LiteralPath $lnk) { Start-Process -FilePath $lnk }
Say ''
Say '업무 비서의 [현지 금형]에 넣어 둔 자료가 있으면, 처음 한 번 아래 명령으로 옮기세요:'
Say '    python -m localmold from-mailhub'
