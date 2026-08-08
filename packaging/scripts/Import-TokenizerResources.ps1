[CmdletBinding()]
param(
    [switch]$Apply,
    [string]$ProjectRoot
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
}
$project = (Get-Item -LiteralPath $ProjectRoot -Force).FullName
$python = Join-Path $project '.runtime-build\runtimes\core\python.exe'
$driver = Join-Path $project 'packaging\scripts\tokenizer_resource.py'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'embedded Core Python is unavailable' }
if (-not (Test-Path -LiteralPath $driver -PathType Leaf)) { throw 'tokenizer resource driver is unavailable' }
$arguments = @('-B', '-I', $driver, 'import', '--project-root', $project)
if ($Apply) { $arguments += '--apply' }
& $python @arguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
