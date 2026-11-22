# Clipper installer for Windows 10/11 (PowerShell).
#
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/k-a-l/clipper/main/scripts/install.ps1 | iex"
#
# What it does (each step is skipped if already done):
#   1. installs FFmpeg and Node.js with winget
#   2. installs uv (https://docs.astral.sh/uv/) - a Python tool manager; it fetches Python 3.12 itself
#   3. installs the `clipper` command from GitHub (or from this folder, if run inside a checkout)
#   4. creates %USERPROFILE%\Clipper\.env with a fresh encryption key
#
# Overrides: $env:CLIPPER_REPO (default k-a-l/clipper), $env:CLIPPER_REF (default main),
#            $env:CLIPPER_HOME (default %USERPROFILE%\Clipper), $env:CLIPPER_SKIP_SYSTEM = "1".
$ErrorActionPreference = "Stop"

$Repo = if ($env:CLIPPER_REPO) { $env:CLIPPER_REPO } else { "k-a-l/clipper" }
$Ref = if ($env:CLIPPER_REF) { $env:CLIPPER_REF } else { "main" }
$HomeDir = if ($env:CLIPPER_HOME) { $env:CLIPPER_HOME } else { Join-Path $env:USERPROFILE "Clipper" }

function Say($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "!!  $msg" -ForegroundColor Yellow }
function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

if ($env:CLIPPER_SKIP_SYSTEM -ne "1") {
    if ((Have ffmpeg) -and (Have node)) {
        Say "FFmpeg and Node.js already installed"
    } elseif (Have winget) {
        if (-not (Have ffmpeg)) {
            Say "Installing FFmpeg (winget)"
            winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
        }
        if (-not (Have node)) {
            Say "Installing Node.js LTS (winget)"
            winget install --id OpenJS.NodeJS.LTS -e --accept-source-agreements --accept-package-agreements
        }
        Refresh-Path
    } else {
        Warn "winget not found. Install FFmpeg (https://www.gyan.dev/ffmpeg/builds/) and Node.js (https://nodejs.org), add them to PATH, then re-run with `$env:CLIPPER_SKIP_SYSTEM='1'."
        exit 1
    }
}

if (Have uv) {
    Say "uv already installed"
} else {
    Say "Installing uv (Python tool manager)"
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    Refresh-Path
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

$ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { "" }
if ($ScriptDir -and (Test-Path (Join-Path $ScriptDir "..\pyproject.toml"))) {
    $Src = (Resolve-Path (Join-Path $ScriptDir "..")).Path
    Say "Installing clipper from this folder"
} else {
    $Src = "git+https://github.com/$Repo@$Ref"
    Say "Installing clipper from github.com/$Repo ($Ref)"
}
uv tool install --force --python 3.12 $Src
uv tool update-shell | Out-Null
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"

New-Item -ItemType Directory -Force -Path $HomeDir | Out-Null
$EnvFile = Join-Path $HomeDir ".env"
if (Test-Path $EnvFile) {
    Say "Keeping your existing settings in $EnvFile"
} else {
    $Key = (clipper gen-key | Select-Object -Last 1)
    @(
        "# Clipper settings - see docs/CREDENTIALS.md for how to fill these in."
        "TOKEN_ENCRYPTION_KEY=$Key"
        "YOUTUBE_CLIENT_ID="
        "YOUTUBE_CLIENT_SECRET="
        "YOUTUBE_PRIVACY_STATUS=private"
        "FACEBOOK_APP_ID="
        "FACEBOOK_APP_SECRET="
        "FACEBOOK_PAGE_ID="
        "FACEBOOK_VIDEO_STATE=DRAFT"
    ) | Set-Content -Encoding utf8 $EnvFile
    Say "Created $EnvFile (with a new encryption key)"
}

Say "Done! Start the dashboard with:"
Write-Host ""
Write-Host "    clipper ui --open"
Write-Host ""
Write-Host "    (open a new terminal first if 'clipper' isn't found)"
Write-Host "    Connect YouTube / Facebook: https://github.com/$Repo/blob/$Ref/docs/CREDENTIALS.md"
