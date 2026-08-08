[CmdletBinding()]
param(
    [switch]$Apply,
    [string]$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
)

$ErrorActionPreference = 'Stop'
$project = (Get-Item -LiteralPath $ProjectRoot -Force).FullName
$python = Join-Path $project '.runtime-build\runtimes\core\python.exe'
$driver = Join-Path $project 'packaging\scripts\ocr_gpu_resource.py'
$arguments = @('-B', '-I', $driver, '--project-root', $project, '--action', 'reset')
if ($Apply) { $arguments += '--apply' }
& $python @arguments
exit $LASTEXITCODE
