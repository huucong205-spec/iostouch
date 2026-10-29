$ErrorActionPreference = "Stop"
$PortalUrl = "http://127.0.0.1:8765"
$ToolDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Test-IOSTouch {
    try {
        $response = Invoke-RestMethod -Uri "$PortalUrl/api/health" -TimeoutSec 1
        return $response.ok -eq $true
    } catch {
        return $false
    }
}

function Open-IOSTouchWindow {
    $appProfile = Join-Path $env:LOCALAPPDATA "IOSTouch\EdgeProfile"
    $existing = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -match '^msedge(\.exe)?$' -and $_.CommandLine -like "*--user-data-dir=$appProfile*"
    }
    if ($existing) { return }

    $edgeCandidates = @(
        (Join-Path ${env:ProgramFiles(x86)} "Microsoft\Edge\Application\msedge.exe"),
        (Join-Path $env:ProgramFiles "Microsoft\Edge\Application\msedge.exe")
    )
    $edge = $edgeCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if ($edge) {
        New-Item -ItemType Directory -Path $appProfile -Force | Out-Null
        Start-Process -FilePath $edge -ArgumentList @("--user-data-dir=$appProfile", "--app=$PortalUrl", "--new-window")
    } else {
        Start-Process $PortalUrl
    }
}

if (Test-IOSTouch) {
    Open-IOSTouchWindow
    exit 0
}

$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show("Không tìm thấy Python 3.11.", "IOSTouch") | Out-Null
    exit 1
}

& $pythonCommand.Source -c "import pymobiledevice3, fastapi, rapidocr" 2>$null
if ($LASTEXITCODE -ne 0) {
    & $pythonCommand.Source -m pip install -r (Join-Path $ToolDir "requirements.txt")
}

Start-Process -FilePath $pythonCommand.Source -ArgumentList @("portal.py") -WorkingDirectory $ToolDir -WindowStyle Hidden

$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Milliseconds 250
    if (Test-IOSTouch) { $ready = $true; break }
}
if (-not $ready) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show("IOSTouch không khởi động được. Hãy kiểm tra Python và requirements.txt.", "IOSTouch") | Out-Null
    exit 1
}

Open-IOSTouchWindow
