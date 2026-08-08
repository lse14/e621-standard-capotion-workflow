[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PythonSourceRoot,
    [Parameter(Mandatory = $true)][string]$OutputRoot,
    [string]$WindowsSdkVersion = '10.0.22621.0',
    [switch]$ReuseExistingBuild
)

$ErrorActionPreference = 'Stop'
$source = (Resolve-Path -LiteralPath $PythonSourceRoot).Path
$output = [System.IO.Path]::GetFullPath($OutputRoot)

function Assert-NoReparseAncestor {
    param([Parameter(Mandatory = $true)][string]$Path)

    $current = $Path
    while ($true) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -Force -LiteralPath $current
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Output root must not be a reparse point or have a reparse ancestor: $Path"
            }
        }
        $parent = [System.IO.Directory]::GetParent($current)
        if ($null -eq $parent -or $parent.FullName -eq $current) { break }
        $current = $parent.FullName
    }
}

$pcbuild = Join-Path $source 'PCbuild\build.bat'
if (-not $ReuseExistingBuild -and -not (Test-Path -LiteralPath $pcbuild)) { throw "Expected CPython PCbuild/build.bat under $source" }
Assert-NoReparseAncestor -Path $output
if (Test-Path -LiteralPath $output) {
    $outputItem = Get-Item -Force -LiteralPath $output
    if (-not $outputItem.PSIsContainer) { throw "Output root must be an ordinary directory: $output" }
    if (-not $ReuseExistingBuild) { throw "Output root already exists: $output" }
}
$sdkLib = "C:\Program Files (x86)\Windows Kits\10\Lib\$WindowsSdkVersion"
if (-not $ReuseExistingBuild -and -not (Test-Path -LiteralPath $sdkLib -PathType Container)) { throw "Requested Windows SDK is not installed: $WindowsSdkVersion" }
$patchLevel = Get-Content -LiteralPath (Join-Path $source 'Include\patchlevel.h') -Raw
if ($patchLevel -notmatch '#define\s+PY_VERSION\s+"3\.11\.15"') { throw 'CPython source must be exactly 3.11.15' }

if (-not $ReuseExistingBuild) {
    $previousSdk = $env:WindowsTargetPlatformVersion
    try {
        $env:WindowsTargetPlatformVersion = $WindowsSdkVersion
        & $pcbuild -p x64 -c Release
        if ($LASTEXITCODE -ne 0) { throw "CPython 3.11.15 build failed" }
    }
    finally {
        $env:WindowsTargetPlatformVersion = $previousSdk
    }
}

$built = Join-Path $source 'PCbuild\amd64'
$python = Join-Path $built 'python.exe'
$dll = Join-Path $built 'python311.dll'
if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $dll)) { throw "Release interpreter artifacts are missing" }

$base = Join-Path $output 'runtimes\_base'
Assert-NoReparseAncestor -Path $base
if (Test-Path -LiteralPath $base) { throw "Base runtime output already exists: $base" }
New-Item -ItemType Directory -Path $base -Force | Out-Null
Get-ChildItem -LiteralPath $built -File |
    Where-Object { $_.Extension -in @('.exe', '.dll', '.pyd') } |
    Copy-Item -Destination $base
Copy-Item -LiteralPath (Join-Path $source 'Lib') -Destination (Join-Path $base 'Lib') -Recurse
if (Test-Path -LiteralPath (Join-Path $built 'python311.zip')) {
    Copy-Item -LiteralPath (Join-Path $built 'python311.zip') -Destination $base
}
Remove-Item -LiteralPath (Join-Path $base 'Lib\ensurepip') -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path (Join-Path $base 'Lib\site-packages') -Force | Out-Null
Set-Content -LiteralPath (Join-Path $base 'python311._pth') -Value "python311.zip`n.`nLib`nLib/site-packages`n" -NoNewline -Encoding utf8
