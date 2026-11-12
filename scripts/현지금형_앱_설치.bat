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
# 1) 업무 비서(stock) 폴더 찾기: 이 파일 위치 → 바탕화면 '업무 비서' 바로가기 → 흔한 위치 → 직접 고르기
# 2) 현지 금형 앱 파일이 없으면 최신 파일 받기 (git pull, 필요하면 브랜치 바꾸기는 물어보고)
# 3) python -m localmold shortcut 으로 바탕화면 · 시작 메뉴 아이콘을 만들고 앱 열기
$BRANCH = 'claude/awesome-planck-4zwz73'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

function Test-Root([string]$d) {
    if (-not $d) { return $false }
    return (Test-Path -LiteralPath (Join-Path $d 'mailhub\__main__.py'))
}

Write-Host ''
Write-Host '=== 현지 금형 육성 관리 - 바탕화면 앱 설치 ==='
Write-Host ''

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
    Write-Host '업무 비서 폴더를 찾지 못했습니다. 열리는 창에서 업무 비서(mailhub)가 들어 있는 폴더를 골라 주세요 (예: stock).'
    Add-Type -AssemblyName System.Windows.Forms
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
if (-not $root) {
    Write-Host '[중단] 업무 비서 폴더(안에 mailhub 폴더가 있는 곳)를 찾지 못했습니다.'
    Write-Host '       이 파일을 업무 비서 폴더(stock) 안에 넣고 다시 더블클릭해 주세요.'
    exit 1
}
Write-Host "업무 비서 폴더: $root"
Set-Location -LiteralPath $root

# ---- 2) 현지 금형 앱 파일 확인 · 최신 받기 ----
$ready = { Test-Path -LiteralPath (Join-Path $root 'localmold\shortcut.py') }
$hasGit = [bool](Get-Command git -ErrorAction SilentlyContinue) -and (Test-Path -LiteralPath (Join-Path $root '.git'))
if (-not (& $ready)) {
    if (-not $hasGit) {
        Write-Host '[중단] 현지 금형 앱 파일이 없고, 이 폴더는 git 으로 받은 폴더가 아니어서 자동으로 받을 수 없습니다.'
        Write-Host "       최신 파일(브랜치 $BRANCH)을 받아 이 폴더에 넣은 뒤 다시 실행해 주세요."
        exit 1
    }
    Write-Host '현지 금형 앱 파일이 없어 최신 파일을 받습니다 (git pull)...'
    git -C $root pull
    if (-not (& $ready)) {
        $cur = (git -C $root rev-parse --abbrev-ref HEAD).Trim()
        Write-Host ''
        Write-Host "지금 브랜치($cur)에는 현지 금형 앱이 없습니다. 앱이 있는 브랜치는 $BRANCH 입니다."
        Write-Host '(업무 비서의 지금까지 기능도 모두 들어 있는 최신 브랜치입니다)'
        $ans = Read-Host '이 브랜치로 바꿀까요? (Y/N)'
        if ($ans -match '^[Yy]') {
            git -C $root fetch origin $BRANCH
            git -C $root checkout $BRANCH
            git -C $root pull
        }
    }
    if (-not (& $ready)) {
        Write-Host '[중단] 현지 금형 앱 파일을 받지 못했습니다. 위의 git 메시지를 캡처해서 Claude 에게 보내 주세요.'
        Write-Host '       (고친 파일이 있어 브랜치를 못 바꾼 경우가 많습니다)'
        exit 1
    }
}

# ---- 3) 아이콘 만들기 · 앱 열기 ----
$py = $null
if (Get-Command python -ErrorAction SilentlyContinue) { $py = @('python') }
elseif (Get-Command py -ErrorAction SilentlyContinue) { $py = @('py', '-3') }
if (-not $py) {
    Write-Host '[중단] python 을 찾지 못했습니다. 업무 비서를 실행하는 PC 에서 다시 실행해 주세요.'
    exit 1
}
$exe = $py[0]
$pyArgs = @()
if ($py.Count -gt 1) { $pyArgs += $py[1..($py.Count - 1)] }
& $exe @pyArgs -m localmold shortcut
if ($LASTEXITCODE -ne 0) {
    Write-Host '[중단] 아이콘을 만들지 못했습니다. 위 메시지를 캡처해서 Claude 에게 보내 주세요.'
    exit 1
}
$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) '현지 금형 육성 관리.lnk'
Write-Host ''
Write-Host '완료: 바탕화면 · 시작 메뉴에 [현지 금형 육성 관리] 아이콘을 만들었습니다.'
if (Test-Path -LiteralPath $lnk) {
    Write-Host '앱을 엽니다... (작업 표시줄의 작은 검은 창 [현지 금형]을 닫으면 앱이 꺼집니다)'
    Start-Process -FilePath $lnk
}
Write-Host ''
Write-Host '업무 비서의 [현지 금형]에 넣어 둔 자료가 있으면, 처음 한 번 아래 명령으로 옮기세요:'
Write-Host '    python -m localmold from-mailhub'
