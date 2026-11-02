[CmdletBinding()]
param(
    [string]$Python = '',
    [string]$DistPath = 'dist/2.0.0',
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($env:OS -ne 'Windows_NT') {
    throw 'Build the Windows application on Windows.'
}

$ProjectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
if ([string]::IsNullOrWhiteSpace($Python)) {
    $LocalPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    $Python = if (Test-Path -LiteralPath $LocalPython) { $LocalPython } else { 'python' }
}

Push-Location -LiteralPath $ProjectRoot
try {
    $DistributionRoot = [IO.Path]::GetFullPath((Join-Path $ProjectRoot $DistPath))
    if (-not $SkipTests) {
        & $Python -m pytest -q tests
        if ($LASTEXITCODE -ne 0) { throw 'Tests failed; build stopped.' }
    }

    & $Python scripts\package_source.py --output (Join-Path $DistributionRoot 'MetaSeries-Organiser-Source.zip')
    if ($LASTEXITCODE -ne 0) { throw 'Source archive creation failed.' }

    & $Python -m PyInstaller --noconfirm --clean --distpath $DistributionRoot --workpath build\2.0.0 packaging\MetaSeries-Organiser.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }

    $OutputDirectory = Join-Path $DistributionRoot 'MetaSeries-Organiser'
    foreach ($Document in @('README.md', 'LICENSE', 'ATTRIBUTION.md', 'CHANGELOG.md')) {
        Copy-Item -LiteralPath (Join-Path $ProjectRoot $Document) -Destination $OutputDirectory
    }
    Copy-Item -LiteralPath (Join-Path $ProjectRoot 'docs') -Destination $OutputDirectory -Recurse -Force

    $ArchivePath = Join-Path $DistributionRoot 'MetaSeries-Organiser-Windows.zip'
    Compress-Archive -LiteralPath $OutputDirectory -DestinationPath $ArchivePath -Force
    $ArchiveHash = (Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $ChecksumPath = "$ArchivePath.sha256"
    [IO.File]::WriteAllText($ChecksumPath, "$ArchiveHash  $([IO.Path]::GetFileName($ArchivePath))`n", [Text.Encoding]::ASCII)
    Write-Host "Built: $OutputDirectory"
    Write-Host "Archive: $ArchivePath"
    Write-Host "Checksum: $ChecksumPath"
}
finally {
    Pop-Location
}
