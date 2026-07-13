# First-time setup for a clean clone. It keeps logins, downloads, models and output local.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$deps = Join-Path $root '.deps'
$downloader = Join-Path $deps 'douyin-downloader'

function Require-Command([string]$Name, [string]$Hint) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name is required. $Hint"
    }
}

Require-Command 'git' 'Install Git for Windows, then run this script again.'
Require-Command 'uv' 'Install uv, then run this script again.'

Push-Location $root
try {
    Write-Host 'Installing the desktop app dependencies...'
    uv sync
    if (-not (Test-Path (Join-Path $root 'config.yml'))) {
        Copy-Item (Join-Path $root 'config.example.yml') (Join-Path $root 'config.yml')
    }

    New-Item -ItemType Directory -Force -Path $deps | Out-Null
    if (-not (Test-Path (Join-Path $downloader '.git'))) {
        Write-Host 'Downloading the open-source Douyin downloader dependency...'
        git clone https://github.com/jiji262/douyin-downloader.git $downloader
    }

    Push-Location $downloader
    try {
        Write-Host 'Installing the downloader browser dependencies...'
        uv sync --extra browser
        uv run playwright install chromium
    }
    finally {
        Pop-Location
    }

    Write-Host ''
    Write-Host 'Setup complete. Start the app with launch-desktop-app.vbs.'
    Write-Host 'Then choose "Re-login to Douyin" inside the app before reading likes or favorites.'
}
finally {
    Pop-Location
}
