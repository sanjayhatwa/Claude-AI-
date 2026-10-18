# Viral Hyper Agent setup for Windows (PowerShell). Easiest: double-click setup.bat.
#   .\setup.ps1            everything: Python packages, fonts, face model, tool repos, health check
#   .\setup.ps1 -Global    also copy the agents and skills into %USERPROFILE%\.claude
#   .\setup.ps1 -NoTools   skip fetching the extra tool repos into tools\
#   .\setup.ps1 -Yes       install missing prerequisites with winget without asking
param([switch]$Global, [switch]$NoTools, [switch]$Yes)
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

function Ask($question) {
  if ($Yes) { return $true }
  $a = Read-Host "$question [Y/n]"
  return ($a -eq "" -or $a -match "^[Yy]")
}

function Refresh-Path {
  # winget installs update the registry PATH, not this window's copy of it
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

function Winget-Install($id, $what) {
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Host "    winget is not available; install $what by hand (link above), then run setup again."
    return $false
  }
  if (-not (Ask "    Install $what now with winget?")) { return $false }
  winget install --id $id -e --accept-source-agreements --accept-package-agreements
  Refresh-Path
  return $true
}

function Find-Python {
  # "python" can be the Microsoft Store shortcut that only opens the Store; check it really runs 3.10+
  foreach ($cmd in @("python", "py")) {
    $c = Get-Command $cmd -ErrorAction SilentlyContinue
    if (-not $c) { continue }
    $probe = @("-c", "import sys; print(int(sys.version_info >= (3, 10)))")
    if ($cmd -eq "py") { $probe = @("-3") + $probe }
    $ok = & $c.Source @probe 2>$null
    if ($ok -eq "1") {
      # the leading comma keeps PowerShell from unrolling a one-item array into a plain string
      if ($cmd -eq "py") { return ,@($c.Source, "-3") }
      return ,@($c.Source)
    }
  }
  return $null
}

Write-Host "==> Checking what this computer has"

$py = Find-Python
if (-not $py) {
  Write-Host "!!  Python 3.10+ not found (https://www.python.org/downloads/ , tick 'Add python.exe to PATH')."
  if (Winget-Install "Python.Python.3.12" "Python 3.12") { $py = Find-Python }
  if (-not $py) { Write-Host "    Install Python, close this window and run setup.bat again."; exit 1 }
}
$pyExe = $py[0]; $pyArgs = @($py | Select-Object -Skip 1)
function Py { & $pyExe @pyArgs @args }
Write-Host ("    Python:  " + (Py --version))

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
  Write-Host "!!  FFmpeg not found (needed to read and render video)."
  Winget-Install "Gyan.FFmpeg" "FFmpeg" | Out-Null
}
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) { Write-Host "    FFmpeg:  found" }
else { Write-Host "    FFmpeg:  still missing; the studio cannot render until it is installed (winget install Gyan.FFmpeg)" }

$hasGit = [bool](Get-Command git -ErrorAction SilentlyContinue)
if (-not $hasGit -and -not $NoTools) {
  Write-Host "!!  Git not found (only needed for the optional tool repos: ReelMimic, Harvey, gstack, REA)."
  if (Winget-Install "Git.Git" "Git") { $hasGit = [bool](Get-Command git -ErrorAction SilentlyContinue) }
}

if (Get-Command claude -ErrorAction SilentlyContinue) { Write-Host "    Claude Code: found" }
else {
  Write-Host "!!  Claude Code not found. The studio still renders without it, but planning, scripts and"
  Write-Host "    'Ask Claude' need it. Install it from https://code.claude.com and run 'claude' once to log in."
}

Write-Host "==> Python packages"
Py -m pip install -q --upgrade pip 2>$null | Out-Null
Py -m pip install -q -r requirements.txt
if ($LASTEXITCODE -ne 0) {
  Write-Host "    retrying as a per-user install"
  Py -m pip install -q --user -r requirements.txt
  if ($LASTEXITCODE -ne 0) { Write-Host "!!  pip could not install the packages; see the messages above."; exit 1 }
}

New-Item -ItemType Directory -Force workspace | Out-Null
New-Item -ItemType File -Force workspace\.gitkeep | Out-Null

Write-Host "==> Studio assets: free caption fonts (Google Fonts, OFL) and the face-tracking model"
Py -m vstudio fonts install "Montserrat" "Anton" "Bebas Neue" "Poppins" "Archivo Black" "Bangers" `
  "Playfair Display" "Space Grotesk" "Permanent Marker" "Courier Prime"
if ($LASTEXITCODE -ne 0) { Write-Host "!!  font download failed (offline?); system fonts will be used" }
Py -c "from vstudio import reframe; print('    face model:', reframe.ensure_model())"
if ($LASTEXITCODE -ne 0) { Write-Host "!!  face model download failed; reframing falls back to centre crops" }

if (-not $NoTools) {
  if (-not $hasGit) {
    Write-Host "==> Skipping tools\ (no Git). Install Git and run setup again to add ReelMimic and the others."
  } else {
    Write-Host "==> Fetching tool repos into tools\ (pinned versions)"
    New-Item -ItemType Directory -Force tools | Out-Null
    foreach ($line in Get-Content tools.lock) {
      $parts = $line -split '\s+'
      if ($parts.Count -lt 3) { continue }
      $name, $url, $rev = $parts
      if (Test-Path "tools\$name\.git") { Write-Host "    ${name}: already there"; continue }
      git clone -q --filter=blob:none $url "tools\$name"
      if ($LASTEXITCODE -ne 0) { Write-Host "!!  could not clone $name; skipping"; continue }
      git -C "tools\$name" checkout -q $rev
      Write-Host "    $name @ $($rev.Substring(0,7))"
    }
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
Write-Host "==> Health check"
Py -m vstudio doctor

Write-Host ""
Write-Host "Done."
Write-Host "  Local studio (web app):   double-click studio.bat   -> http://127.0.0.1:4320"
Write-Host "  Claude Code agent:        open Claude Code in this folder and say:  make me go viral"
Write-Host "  Anything marked fail/warn above shows its fix next to it."
