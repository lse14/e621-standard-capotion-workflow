[CmdletBinding()]
param(
    [switch]$Apply,
    [string]$ProjectRoot
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
}

function Assert-SafeProjectRoot([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.PSIsContainer) { throw 'project root must be a directory' }
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'project root must not be a reparse point'
    }
    return $item.FullName
}

$project = Assert-SafeProjectRoot $ProjectRoot
$python = Join-Path $project '.runtime-build\runtimes\core\python.exe'
$driver = Join-Path $project 'packaging\scripts\ocr_resource.py'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'embedded Core Python is unavailable' }
if (-not (Test-Path -LiteralPath $driver -PathType Leaf)) { throw 'OCR resource driver is unavailable' }

$arguments = @('-B', '-I', $driver, 'clean', '--project-root', $project)
if ($Apply) { $arguments += '--apply' }
& $python @arguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
