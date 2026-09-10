param(
    [switch]$Android,
    [switch]$Docker
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Create the project virtual environment at .venv before validating."
}

function Invoke-ValidationStep {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][scriptblock]$Command
    )

    Write-Host "`n==> $Name" -ForegroundColor Cyan
    Push-Location -LiteralPath $WorkingDirectory
    try {
        & $Command
        if ($LASTEXITCODE -ne 0) {
            throw "$Name failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }
}

Invoke-ValidationStep "Guided Windows setup contract" $ProjectRoot {
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProjectRoot "tools\test-setup.ps1")
}

Invoke-ValidationStep "Private remote access contract" $ProjectRoot {
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProjectRoot "tools\test-remote-access.ps1")
}

Invoke-ValidationStep "Backend tests" (Join-Path $ProjectRoot "backend") {
    & $Python -m pytest tests -q
}

Invoke-ValidationStep "Alembic migration SQL" (Join-Path $ProjectRoot "backend") {
    & $Python -m alembic upgrade head --sql | Out-Null
}

Invoke-ValidationStep "Frontend lint" (Join-Path $ProjectRoot "frontend") {
    npm run lint
}

Invoke-ValidationStep "Frontend production build" (Join-Path $ProjectRoot "frontend") {
    npm run build
}

Invoke-ValidationStep "Mobile type check" (Join-Path $ProjectRoot "mobile") {
    npm run typecheck
}

if ($Docker) {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker is not available on PATH."
    }
    Invoke-ValidationStep "Development Docker Compose configuration" $ProjectRoot {
        docker compose config --quiet
    }
    Invoke-ValidationStep "Production Docker Compose configuration" $ProjectRoot {
        docker compose -f docker-compose.yml -f docker-compose.production.yml config --quiet
    }
}

if ($Android) {
    $AndroidJbr = "C:\Program Files\Android\Android Studio\jbr"
    if (-not (Test-Path -LiteralPath $AndroidJbr)) {
        throw "Android Studio JBR was not found at $AndroidJbr."
    }
    $env:JAVA_HOME = $AndroidJbr
    $env:NODE_ENV = "development"
    Invoke-ValidationStep "Android debug build" (Join-Path $ProjectRoot "mobile\android") {
        .\gradlew.bat app:assembleDebug -x lint -x test --configure-on-demand --build-cache -PreactNativeDevServerPort=8081 -PreactNativeArchitectures=x86_64
    }
}

Invoke-ValidationStep "Patch whitespace check" $ProjectRoot {
    git diff --check
}

Write-Host "`nAll requested validation gates passed." -ForegroundColor Green
