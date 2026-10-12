# 파일 찾기 설치 — 이 폴더에서 실행:  powershell -ExecutionPolicy Bypass -File setup.ps1
# 기본은 검색만 설치한다 (파일을 옮기지 않음). 다운로드 자동 정리까지 원하면:  ... -File setup.ps1 -Sort
param([switch]$Sort)
$ErrorActionPreference = "Stop"
$dir = $PSScriptRoot

# 1. 파이썬 찾기
$py = $null
foreach ($c in @("python", "py")) {
    try { $py = (& $c -c "import sys; print(sys.executable)" 2>$null); if ($py) { break } } catch {}
}
if (-not $py -or $py -like "*WindowsApps*") {
    Write-Host "파이썬이 없어요. 먼저 설치해 주세요:  winget install Python.Python.3.12" -ForegroundColor Yellow
    exit 1
}
$pyw = Join-Path (Split-Path $py) "pythonw.exe"
Write-Host "파이썬: $py"

# 2. 필요한 부품 설치
& $py -m pip install --quiet --disable-pip-version-check -r "$dir\requirements.txt"

# 3. 설정 파일 (이미 있으면 그대로)
if (-not (Test-Path "$dir\config.json")) {
    Copy-Item "$dir\config.example.json" "$dir\config.json"
    Write-Host "config.json 을 만들었어요. 검색할 폴더·분류 규칙은 여기서 바꿔요."
}
$cfg = Get-Content "$dir\config.json" -Raw -Encoding UTF8 | ConvertFrom-Json

# 4. 켜져 있던 검색기 끄기 (새 코드로 다시 켜지도록)
Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -like "*server.pyw*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

# 5. 바탕화면 아이콘 + 단축키
$desk = [Environment]::GetFolderPath("Desktop")
$sh = New-Object -ComObject WScript.Shell
$l = $sh.CreateShortcut("$desk\파일 찾기.lnk")
$l.TargetPath = $pyw
$l.Arguments = "`"$dir\server.pyw`""
$l.WorkingDirectory = $dir
$l.IconLocation = "$env:SystemRoot\System32\shell32.dll,22"
$l.Hotkey = $cfg.'단축키'
$l.Save()
Write-Host "바탕화면에 '파일 찾기' 아이콘을 만들었어요. 단축키: $($cfg.'단축키')"

# 6. 다운로드 자동 정리 — -Sort 를 줬을 때만 (하루 한 번 오후 5시 30분, 꺼져 있었으면 다음에 켤 때)
$task = "파일찾기 - 다운로드 정리"
if ($Sort) {
    $a = New-ScheduledTaskAction -Execute $pyw -Argument "`"$dir\sorter.py`"" -WorkingDirectory $dir
    $t = New-ScheduledTaskTrigger -Daily -At 17:30
    $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    Register-ScheduledTask -TaskName $task -Action $a -Trigger $t -Settings $s -Force | Out-Null
    Write-Host "다운로드 자동 정리를 매일 오후 5시 30분에 하도록 등록했어요."
} elseif (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) {
    Write-Host "다운로드 자동 정리가 이미 켜져 있어요. 그대로 둡니다. (끄려면 작업 스케줄러에서 '$task' 삭제)"
}

# 7. 첫 색인
Write-Host "파일을 처음 읽는 중이에요. 파일이 많으면 몇 분 걸려요..."
$env:PYTHONIOENCODING = "utf-8"
& $py "$dir\indexer.py" index | Select-Object -Last 1
Write-Host "끝났어요. '파일 찾기' 아이콘이나 $($cfg.'단축키') 로 여세요." -ForegroundColor Green
