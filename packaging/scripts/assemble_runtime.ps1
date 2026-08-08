[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BaseRuntime,
    [Parameter(Mandatory = $true)][string]$DestinationRuntime,
    [Parameter(Mandatory = $true)][string]$RequirementsLock,
    [Parameter(Mandatory = $true)][string]$Wheelhouse,
    [Parameter(Mandatory = $true)][string]$OwnerSource,
    [Parameter(Mandatory = $true)][string]$BuildPython,
    [string]$SharedSource,
    [switch]$KeepSetuptools
)

$ErrorActionPreference = 'Stop'
$base = (Resolve-Path -LiteralPath $BaseRuntime).Path
$destination = [System.IO.Path]::GetFullPath($DestinationRuntime)
$lock = (Resolve-Path -LiteralPath $RequirementsLock).Path
$wheels = (Resolve-Path -LiteralPath $Wheelhouse).Path
$owner = (Resolve-Path -LiteralPath $OwnerSource).Path
$builder = (Resolve-Path -LiteralPath $BuildPython).Path
$project = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
if (Test-Path -LiteralPath $destination) { throw "Destination runtime already exists: $destination" }
if ($SharedSource) {
    $shared = (Resolve-Path -LiteralPath $SharedSource).Path
    $projectPrefix = $project.TrimEnd([char[]]@('\', '/')) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $shared.StartsWith($projectPrefix, [System.StringComparison]::OrdinalIgnoreCase)) { throw "SharedSource escapes project root: $shared" }
    foreach ($item in @((Get-Item -LiteralPath $shared -Force), (Get-Item -LiteralPath $project -Force))) {
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "SharedSource reparse point is not allowed: $($item.FullName)" }
    }
    if (-not (Get-Item -LiteralPath $shared -Force).PSIsContainer) { throw "SharedSource must be a package directory: $shared" }
    if ((Split-Path -Leaf $owner) -ieq (Split-Path -Leaf $shared)) { throw "SharedSource package name duplicates owner package" }
    $sharedStack = New-Object 'System.Collections.Generic.Stack[string]'
    $sharedStack.Push($shared)
    while ($sharedStack.Count -gt 0) {
        $currentShared = $sharedStack.Pop()
        foreach ($child in Get-ChildItem -LiteralPath $currentShared -Force) {
            if (($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "SharedSource reparse point is not allowed: $($child.FullName)" }
            if ($child.PSIsContainer) { $sharedStack.Push($child.FullName) }
        }
    }
}

Copy-Item -LiteralPath $base -Destination $destination -Recurse
$python = Join-Path $destination 'python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Embedded python.exe is missing" }

# The explicitly supplied build interpreter is allowed only during assembly.
# The destination embedded interpreter never receives pip or an online source.
& $builder -B -I -m pip install --no-index --find-links $wheels --require-hashes --only-binary :all: --target (Join-Path $destination 'Lib\site-packages') -r $lock
if ($LASTEXITCODE -ne 0) { throw "Offline hash-checked dependency installation failed" }
Copy-Item -LiteralPath $owner -Destination (Join-Path $destination 'Lib\site-packages') -Recurse
if ($SharedSource) {
    $sharedTarget = Join-Path $destination ('Lib\site-packages\' + (Split-Path -Leaf $shared))
    if (Test-Path -LiteralPath $sharedTarget) { throw "SharedSource package name already exists in runtime: $sharedTarget" }
    Copy-Item -LiteralPath $shared -Destination (Join-Path $destination 'Lib\site-packages') -Recurse
}
$removablePattern = if ($KeepSetuptools) { '^(pip|wheel|pytest)([._-]|$)' } else { '^(pip|setuptools|wheel|pytest)([._-]|$)' }
Get-ChildItem -LiteralPath (Join-Path $destination 'Lib\site-packages') -Force |
    Where-Object { $_.Name -match $removablePattern } |
    Remove-Item -Recurse -Force
