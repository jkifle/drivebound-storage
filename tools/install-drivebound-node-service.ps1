param(
    [string]$PackageRoot = (Join-Path $PSScriptRoot "..\node_service"),
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

$resolvedPackageRoot = (Resolve-Path $PackageRoot).Path
$distDir = Join-Path $resolvedPackageRoot "dist"

if (-not (Test-Path $distDir) -or -not (Get-ChildItem $distDir -Filter "drivebound_node_service-*.whl" -ErrorAction SilentlyContinue)) {
    Push-Location $resolvedPackageRoot
    & $Python -m pip install --upgrade build
    & $Python -m build
    Pop-Location
}

$wheel = Get-ChildItem $distDir -Filter "drivebound_node_service-*.whl" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $wheel) {
    throw "Unable to find a built Drivebound node wheel in $distDir"
}

& $Python -m pip install --upgrade $wheel.FullName

$pythonVersion = (& $Python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
$userScripts = Join-Path $env:APPDATA "Python\Python$pythonVersion\Scripts"
$currentUserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($currentUserPath -notmatch [regex]::Escape($userScripts)) {
    [Environment]::SetEnvironmentVariable("Path", "$userScripts;$currentUserPath", "User")
}

Write-Host "Drivebound node service installed successfully."
Write-Host "Add this to your PATH if needed: $userScripts"
Write-Host "Then run: drivebound-node-service install --platform auto --config $HOME\.drivebound-node\config.json"
