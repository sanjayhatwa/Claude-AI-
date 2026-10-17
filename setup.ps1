# Viral Hyper Agent setup for Windows (PowerShell).
#   .\setup.ps1            Python tools + fetch the full tool repos into tools\
#   .\setup.ps1 -Global    also copy the agents and skills into %USERPROFILE%\.claude
#   .\setup.ps1 -NoTools   skip fetching tools\ (the skills work without them)
param([switch]$Global, [switch]$NoTools)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
if (-not $py) { Write-Error "Python 3.10+ is required: https://www.python.org/downloads/ (tick 'Add to PATH')" }

Write-Host "==> Python packages"
& $py.Source -m pip install -q -r requirements.txt

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
  Write-Host "!!  ffmpeg not found. reel-mimic needs it: winget install Gyan.FFmpeg"
}

New-Item -ItemType Directory -Force workspace | Out-Null
New-Item -ItemType File -Force workspace\.gitkeep | Out-Null

if (-not $NoTools) {
  Write-Host "==> Fetching tool repos into tools\ (pinned versions)"
  New-Item -ItemType Directory -Force tools | Out-Null
  foreach ($line in Get-Content tools.lock) {
    $parts = $line -split '\s+'
    if ($parts.Count -lt 3) { continue }
    $name, $url, $rev = $parts
    if (Test-Path "tools\$name\.git") { Write-Host "    ${name}: already there"; continue }
    git clone -q --filter=blob:none $url "tools\$name"
    git -C "tools\$name" checkout -q $rev
    Write-Host "    $name @ $($rev.Substring(0,7))"
  }
}

if ($Global) {
  Write-Host "==> Copying agents and skills into $env:USERPROFILE\.claude"
  $dest = Join-Path $env:USERPROFILE ".claude"
  New-Item -ItemType Directory -Force "$dest\agents", "$dest\skills" | Out-Null
  Copy-Item -Recurse -Force .claude\agents\* "$dest\agents\"
  Copy-Item -Recurse -Force .claude\skills\* "$dest\skills\"
}

Write-Host ""
Write-Host "Done. Open Claude Code in this folder and say:  make me go viral"
