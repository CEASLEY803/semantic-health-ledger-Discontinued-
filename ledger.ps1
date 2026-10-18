#Requires -Version 5.1
<#
.SYNOPSIS
    Semantic Health Ledger - unified dev script.

.DESCRIPTION
    ledger.ps1 <command>

    Commands
    --------
    setup      One-time install: venv, pip, npm, .env, DB init.
    dev        Kill stale ports then launch Tauri dev mode (hot-reload).
    build      Production Tauri build (outputs installer to frontend/src-tauri/target/release).
    kill       Kill anything on ports 8787 / 3000 / 3001.
    help       Print this message.

.EXAMPLE
    .\ledger.ps1 setup
    .\ledger.ps1 dev
#>

param(
    [Parameter(Position = 0)]
    [ValidateSet('setup', 'dev', 'build', 'kill', 'help', '')]
    [string]$Command = 'help'
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# ── Shared helpers ─────────────────────────────────────────────────────────────

function Write-Step([string]$msg) {
    Write-Host "`n  $msg" -ForegroundColor Cyan
}

function Write-Ok([string]$msg) {
    Write-Host "  $msg" -ForegroundColor Green
}

function Write-Warn([string]$msg) {
    Write-Host "  WARNING: $msg" -ForegroundColor Yellow
}

function Kill-Ports {
    Write-Step "Freeing ports 8787 / 3000 / 3001..."
    foreach ($port in @(8787, 3000, 3001)) {
        $ids = (Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue).OwningProcess
        foreach ($id in $ids) {
            Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Milliseconds 600
    Write-Ok "Ports clear."
}

# ── Commands ──────────────────────────────────────────────────────────────────

function Invoke-Setup {
    Write-Host "`n=== Semantic Health Ledger - Setup ===" -ForegroundColor White

    # Python
    Write-Step "Checking Python..."
    $PYTHON = if ($env:PYTHON) { $env:PYTHON } else { 'python' }
    try {
        $pyVersion = & $PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
        if ($LASTEXITCODE -ne 0) { throw }
        Write-Ok "Python $pyVersion found."
    } catch {
        Write-Host "  ERROR: Python not found. Install Python 3.9+ and re-run." -ForegroundColor Red
        exit 1
    }

    # Virtual environment
    Write-Step "Setting up virtual environment..."
    if (-not (Test-Path '.venv')) {
        & $PYTHON -m venv .venv
    }
    Write-Ok "Virtual environment ready."

    # Python dependencies
    Write-Step "Installing Python dependencies..."
    & .\.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
    & .\.venv\Scripts\python.exe -m pip install -e . --quiet
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ERROR: pip install failed - see output above." -ForegroundColor Red
        exit 1
    }
    Write-Ok "Python dependencies installed."

    # .env
    Write-Step "Checking .env..."
    if (-not (Test-Path '.env')) {
        if (Test-Path '.env.example') {
            Copy-Item '.env.example' '.env'
            Write-Warn ".env created from .env.example - fill in your keys."
        } else {
            Write-Warn ".env.example not found - create .env manually."
        }
    } else {
        Write-Ok ".env already exists."
    }

    # Gemini API key prompt
    if ((Test-Path '.env') -and (Select-String -Path '.env' -Pattern '^GEMINI_API_KEY=\s*$' -Quiet)) {
        Write-Host ""
        $apiKey = Read-Host "  Enter your Gemini API key (aistudio.google.com)"
        if ($apiKey) {
            (Get-Content '.env') -replace '^GEMINI_API_KEY=.*', "GEMINI_API_KEY=$apiKey" |
                Set-Content '.env' -Encoding UTF8
            Write-Ok "API key saved."
        } else {
            Write-Warn "Skipped - edit .env and set GEMINI_API_KEY before launching."
        }
    }

    # Database
    Write-Step "Initialising database..."
    & .\.venv\Scripts\python.exe init_storage.py
    Write-Ok "Database ready."

    # Frontend
    Write-Step "Installing frontend dependencies..."
    if (Get-Command node -ErrorAction SilentlyContinue) {
        npm --prefix frontend install --legacy-peer-deps
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  ERROR: npm install failed - see output above." -ForegroundColor Red
            exit 1
        }
        Write-Ok "Frontend dependencies installed."
    } else {
        Write-Warn "Node.js not found - skipping. Install Node 18+ then run: npm --prefix frontend install"
    }

    # Register project root (for installed .exe builds)
    Write-Step "Registering project root..."
    $appDir = Join-Path $env:LOCALAPPDATA 'HealthLedger'
    if (-not (Test-Path $appDir)) { New-Item -ItemType Directory -Path $appDir | Out-Null }
    [System.IO.File]::WriteAllText(
        (Join-Path $appDir 'project_root.txt'),
        $PSScriptRoot,
        [System.Text.UTF8Encoding]::new($false)
    )
    Write-Ok "Project root registered: $PSScriptRoot"

    Write-Host "`n=== Setup complete ===" -ForegroundColor Green
    Write-Host "  Launch dev mode:     .\ledger.ps1 dev   (or double-click LaunchLedger.vbs)"
    Write-Host "  Build installer:     .\ledger.ps1 build`n"
}

function Invoke-Dev {
    Kill-Ports
    Write-Step "Starting Tauri dev mode..."
    Write-Host "  (Ctrl+C or close the window to stop)`n"
    Set-Location frontend
    npx tauri dev
}

function Invoke-Build {
    Write-Step "Building production installer..."
    Set-Location frontend
    npx tauri build
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ERROR: build failed - see output above." -ForegroundColor Red
        exit 1
    }

    # Copy the installer to the project root so it's easy to find
    $bundleDir = Join-Path $PSScriptRoot 'frontend\src-tauri\target\release\bundle\nsis'
    $installer  = Get-ChildItem -Path $bundleDir -Filter '*-setup.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($installer) {
        $dest = Join-Path $PSScriptRoot $installer.Name
        Copy-Item $installer.FullName $dest -Force
        Write-Ok "Installer copied to project root: $($installer.Name)"
    } else {
        Write-Warn "Could not find installer in $bundleDir - check the bundle dir manually."
    }
}

function Invoke-Help {
    Write-Host @"

  Semantic Health Ledger - ledger.ps1

  Usage:  .\ledger.ps1 <command>

  Commands:
    setup    One-time install (venv, pip, npm, .env, DB)
    dev      Kill ports + launch Tauri hot-reload dev mode
    build    Production Tauri build (outputs installer)
    kill     Free ports 8787 / 3000 / 3001
    help     Show this message

  Daily workflow:
    First time:   .\ledger.ps1 setup
    Every day:    .\ledger.ps1 dev   (or double-click LaunchLedger.vbs)

"@
}

# ── Dispatch ──────────────────────────────────────────────────────────────────

switch ($Command) {
    'setup' { Invoke-Setup }
    'dev'   { Invoke-Dev   }
    'build' { Invoke-Build }
    'kill'  { Kill-Ports   }
    default { Invoke-Help  }
}

