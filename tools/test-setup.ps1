$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$SetupScript = Join-Path $PSScriptRoot "setup-drivebound.ps1"
$TemporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ("drivebound-setup-contract-" + [Guid]::NewGuid().ToString("N"))

function Read-TestEnvironment {
    param([Parameter(Mandatory = $true)][string]$Path)
    $values = @{}
    Get-Content -LiteralPath $Path | ForEach-Object {
        if ($_ -match "^([^#=]+)=(.*)$") {
            $values[$matches[1]] = $matches[2]
        }
    }
    return $values
}

function Assert-SetupCondition {
    param(
        [Parameter(Mandatory = $true)][bool]$Condition,
        [Parameter(Mandatory = $true)][string]$Message
    )
    if (-not $Condition) {
        throw $Message
    }
}

function New-TestInstallation {
    param([Parameter(Mandatory = $true)][string]$Name)
    $root = Join-Path $TemporaryRoot $Name
    [void](New-Item -ItemType Directory -Path $root -Force)
    Copy-Item -LiteralPath (Join-Path $ProjectRoot ".env.example") -Destination (Join-Path $root ".env.example")
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "docker-compose.yml") -Destination (Join-Path $root "docker-compose.yml")
    [void](New-Item -ItemType Directory -Path (Join-Path $root "photos") -Force)
    return $root
}

function Invoke-TestInstallationSetup {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [switch]$RefreshPlaceholderSecrets
    )
    $arguments = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $SetupScript,
        "-Action", "Setup", "-NonInteractive", "-NoStart", "-NoBrowser",
        "-ProjectRootOverride", $Root,
        "-ExistingPhotosPath", (Join-Path $Root "photos"),
        "-ManagedDataPath", (Join-Path $Root "managed"),
        "-ProtectionDataPath", (Join-Path $Root "protection")
    )
    if ($RefreshPlaceholderSecrets) {
        $arguments += "-RefreshPlaceholderSecrets"
    }
    & powershell.exe @arguments | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Guided setup exited with code $LASTEXITCODE."
    }
}

try {
    [void](New-Item -ItemType Directory -Path $TemporaryRoot)

    $freshRoot = New-TestInstallation -Name "fresh"
    Invoke-TestInstallationSetup -Root $freshRoot
    $freshValues = Read-TestEnvironment -Path (Join-Path $freshRoot ".env")
    $firstMediaKey = $freshValues["MEDIA_ENCRYPTION_MASTER_KEY"]
    Assert-SetupCondition ($freshValues["POSTGRES_PASSWORD"] -ne "change-me") "Fresh setup retained the example database password."
    Assert-SetupCondition (-not $freshValues["JWT_SECRET"].StartsWith("replace-")) "Fresh setup retained the example JWT secret."
    Assert-SetupCondition ($firstMediaKey -ne "RZAIpFM5CAciKTy4UABCTvvyD83LfeB9r_ZR4z0LnKQ=") "Fresh setup retained the development media key."
    Assert-SetupCondition ($firstMediaKey.Length -eq 44 -and $firstMediaKey.EndsWith("=")) "Generated media key is not Fernet-compatible."
    Assert-SetupCondition (Test-Path -LiteralPath (Join-Path $freshRoot "protection\backups\account-deletion-suppressions")) "Deletion suppression ledger was not created."
    $compose = Get-Content -Raw -LiteralPath (Join-Path $freshRoot "docker-compose.user.yml")
    Assert-SetupCondition ($compose -match "/data/imports:ro") "Existing photos were not mounted read-only."
    Assert-SetupCondition ($compose -match "/data/replicas:ro") "Backend replicas were not mounted read-only."

    Invoke-TestInstallationSetup -Root $freshRoot
    $secondValues = Read-TestEnvironment -Path (Join-Path $freshRoot ".env")
    Assert-SetupCondition ($secondValues["MEDIA_ENCRYPTION_MASTER_KEY"] -eq $firstMediaKey) "A repeat setup changed the media-encryption key."
    Assert-SetupCondition ($secondValues["POSTGRES_PASSWORD"] -eq $freshValues["POSTGRES_PASSWORD"]) "A repeat setup changed the database password."

    $existingRoot = New-TestInstallation -Name "existing"
    Copy-Item -LiteralPath (Join-Path $existingRoot ".env.example") -Destination (Join-Path $existingRoot ".env")
    Invoke-TestInstallationSetup -Root $existingRoot
    $preservedValues = Read-TestEnvironment -Path (Join-Path $existingRoot ".env")
    Assert-SetupCondition ($preservedValues["MEDIA_ENCRYPTION_MASTER_KEY"] -eq "RZAIpFM5CAciKTy4UABCTvvyD83LfeB9r_ZR4z0LnKQ=") "Existing media key was rotated without explicit confirmation."

    Invoke-TestInstallationSetup -Root $existingRoot -RefreshPlaceholderSecrets
    $refreshedValues = Read-TestEnvironment -Path (Join-Path $existingRoot ".env")
    Assert-SetupCondition ($refreshedValues["MEDIA_ENCRYPTION_MASTER_KEY"] -ne "RZAIpFM5CAciKTy4UABCTvvyD83LfeB9r_ZR4z0LnKQ=") "Explicit fresh-install confirmation did not replace the placeholder key."

    Write-Host "Guided setup contract passed." -ForegroundColor Green
}
finally {
    if (Test-Path -LiteralPath $TemporaryRoot) {
        $resolved = [IO.Path]::GetFullPath($TemporaryRoot)
        $temp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
        if ($resolved.StartsWith($temp, [StringComparison]::OrdinalIgnoreCase) -and
            (Split-Path -Leaf $resolved).StartsWith("drivebound-setup-contract-")) {
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
    }
}
