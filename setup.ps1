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

function Winget-Install($id, $what, $extra = @()) {
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Host "    winget is not available; install $what by hand (link above), then run setup again."
    return $false
  }
  if (-not (Ask "    Install $what now with winget?")) { return $false }
  winget install --id $id -e --accept-source-agreements --accept-package-agreements @extra | Out-Host
  Refresh-Path
  return $true
}

# ARM64 Windows (Snapdragon laptops): OpenCV, which the studio reads video with, has no Windows ARM64
# builds, so the studio needs the x64 Python there; it runs under Windows' built-in emulation.
$Arm64Host = ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") -or ($env:PROCESSOR_ARCHITEW6432 -eq "ARM64")
if (-not $Arm64Host -and $env:OS -eq "Windows_NT") {
  try { $Arm64Host = [bool](Get-CimInstance Win32_Processor -ErrorAction Stop | Where-Object { $_.Architecture -eq 12 }) }
  catch { }
}

function Find-Python([switch]$AllowArm64) {
  # "python" can be the Microsoft Store shortcut that only opens the Store; check it really runs 3.10+.
  # On ARM64 Windows an ARM64 Python only counts with -AllowArm64; the launcher's exact tags
  # (py -V:3.12) name the x64 builds there.
  $candidates = @(@("python"), @("py", "-3"))
  if ($Arm64Host) { foreach ($v in 14..10) { $candidates += ,@("py", "-V:3.$v") } }
  foreach ($cand in $candidates) {
    $c = Get-Command $cand[0] -ErrorAction SilentlyContinue
    if (-not $c) { continue }
    $pre = @($cand | Select-Object -Skip 1)
    $probe = $pre + @("-c", "import platform, sys; print(int(sys.version_info >= (3, 10)), platform.machine())")
    $out = & $c.Source @probe 2>$null
    $ok, $machine = "$out".Trim() -split '\s+', 2
    if ($ok -ne "1") { continue }
    if ($Arm64Host -and $machine -eq "ARM64" -and -not $AllowArm64) { continue }
    # the leading comma keeps PowerShell from unrolling a one-item array into a plain string
    return ,(@($c.Source) + $pre)
  }
  return $null
}

Write-Host "==> Checking what this computer has"

$py = $null
if (Test-Path ".venv\Scripts\python.exe") {
  # a project environment (as setup.sh makes on macOS and Linux) is used as it is
  $venvPy = (Resolve-Path ".venv\Scripts\python.exe").Path
  $ok = & $venvPy -c "import sys; print(int(sys.version_info >= (3, 10)))" 2>$null
  if ($ok -eq "1") { $py = @($venvPy); Write-Host "    using the project environment in .venv" }
}
if (-not $py) { $py = Find-Python }
if (-not $py) {
  $armPy = $null
  if ($Arm64Host) { $armPy = Find-Python -AllowArm64 }
  $arch = @()
  if ($Arm64Host) { $arch = @("--architecture", "x64") }
  if ($armPy) {
    Write-Host "!!  This is an ARM64 computer and the Python found is the ARM64 build. OpenCV (how the studio"
    Write-Host "    reads video) has no ARM64 builds for Windows, so the studio needs the x64 Python, which runs"
    Write-Host "    here under Windows' emulation."
    if (Winget-Install "Python.Python.3.12" "Python 3.12 (x64)" $arch) { $py = Find-Python }
    if (-not $py) {
      Write-Host "    Going on with the ARM64 Python: OpenCV will not install. Later, for the studio:"
      Write-Host "    winget install --id Python.Python.3.12 -e --architecture x64   and run setup.bat again."
      $py = $armPy
    }
  } else {
    Write-Host "!!  Python 3.10+ not found (https://www.python.org/downloads/ , tick 'Add python.exe to PATH')."
    if (Winget-Install "Python.Python.3.12" "Python 3.12" $arch) { $py = Find-Python }
  }
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
# Packages the studio cannot start without; the others switch features on (downloads, beats, shots,
# OCR, word timing, exact variable fonts) and only cost those features when they cannot be installed.
$corePackages = @("numpy", "opencv-python-headless", "pillow")
$newestWheels = 14    # newest Python 3.x that every package (OCR's onnxruntime, whisper's ctranslate2) has builds for
$pyMinor = 0; [void][int]::TryParse("$(Py -c 'import sys; print(sys.version_info[1])')".Trim(), [ref]$pyMinor)
$pyMachine = "$(Py -c 'import platform; print(platform.machine())')".Trim()
if ($pyMinor -gt $newestWheels) {
  Write-Host "    note: Python 3.$pyMinor is newer than some packages' builds; OCR or whisper may not install yet"
  Write-Host "          (everything else works; Python 3.$newestWheels gets them all)"
}

function Pip-Install {
  # for everyone using this Python (a python.org install for the current user takes it), else per user
  Py -m pip install -q @args | Out-Host
  if ($LASTEXITCODE -eq 0) { return $true }
  Py -m pip install -q --user @args | Out-Host
  return ($LASTEXITCODE -eq 0)
}

Py -m pip install -q --upgrade pip 2>$null | Out-Null
# pip installs a requirements file all or nothing: one package without a build for this computer (OCR or
# whisper on a brand-new Python, say) would leave every package out. Then each is installed on its own.
if (-not (Pip-Install -r requirements.txt)) {
  Write-Host "    some packages could not be installed together; installing them one by one"
  $missing = @(); $coreMissing = @()
  foreach ($line in Get-Content requirements.txt) {
    $req = ($line -replace '#.*', '').Trim()
    if (-not $req) { continue }
    if (-not (Pip-Install $req)) {
      $missing += $req
      $name = ((($req -split '[\s<>=!~;\[]')[0]).ToLower()) -replace '[_.]', '-'
      if ($corePackages -contains $name) { $coreMissing += $req }
    }
  }
  if ($coreMissing.Count -gt 0) {
    Write-Host ("!!  could not install: " + ($coreMissing -join ", ") + " (the studio needs it). See pip's messages above;")
    if ($pyMachine -eq "ARM64") {
      Write-Host "    this is the ARM64 Python, which OpenCV has no Windows builds for. Install the x64 Python"
      Write-Host "    (winget install --id Python.Python.3.12 -e --architecture x64), then run setup.bat again."
    } else {
      Write-Host "    a Python version that has builds for it (3.10 to 3.$newestWheels) fixes most cases."
    }
    exit 1
  }
  if ($missing.Count -gt 0) {
    Write-Host ("!!  not installed: " + ($missing -join ", ") + ". Everything else works; the health check below says what is missing.")
  }
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
