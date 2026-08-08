[CmdletBinding()]
param(
    [switch]$Apply,
    [string]$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')),
    [switch]$SkipManifestRefresh
)

$ErrorActionPreference = 'Stop'

function Same-Path([string]$Left, [string]$Right) {
    return [System.StringComparer]::OrdinalIgnoreCase.Equals([System.IO.Path]::GetFullPath($Left).TrimEnd([char[]]@('\', '/')), [System.IO.Path]::GetFullPath($Right).TrimEnd([char[]]@('\', '/')))
}
function Within-Root([string]$Root, [string]$Candidate) {
    $rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd([char[]]@('\', '/'))
    $candidatePath = [System.IO.Path]::GetFullPath($Candidate).TrimEnd([char[]]@('\', '/'))
    return (Same-Path $rootPath $candidatePath) -or $candidatePath.StartsWith($rootPath + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)
}
function Safe-Item([string]$Root, [string]$Path, [string]$Label, [bool]$Directory) {
    $item = Get-Item -LiteralPath $Path -Force
    if ($Directory -and -not $item.PSIsContainer) { throw "$Label must be a directory" }
    if (-not $Directory -and $item.PSIsContainer) { throw "$Label must be a file" }
    if (-not (Within-Root $Root $item.FullName)) { throw "$Label escapes project root" }
    $current = $item.FullName
    while ($true) {
        if (((Get-Item -LiteralPath $current -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "reparse point is not allowed: $current" }
        if (Same-Path $current $Root) { break }
        $current = [System.IO.Path]::GetDirectoryName($current)
        if (-not $current) { throw "$Label escapes project root" }
    }
    return $item.FullName
}
function Planned-Target([string]$Root, [string]$Path) {
    $full = [System.IO.Path]::GetFullPath($Path)
    if (-not (Within-Root $Root $full)) { throw "target escapes project root: $full" }
    if (Test-Path -LiteralPath $full) { Safe-Item $Root $full 'planned assembled module' $false | Out-Null; return $full }
    $parent = [System.IO.Path]::GetDirectoryName($full)
    while (-not (Test-Path -LiteralPath $parent)) {
        $parent = [System.IO.Path]::GetDirectoryName($parent)
        if (-not $parent) { throw "target has no project-local ancestor: $full" }
    }
    Safe-Item $Root $parent 'target ancestor' $true | Out-Null
    return $full
}
function Safe-PythonFiles([string]$Root, [string]$Directory) {
    Safe-Item $Root $Directory 'package directory' $true | Out-Null
    $files = @()
    $stack = New-Object 'System.Collections.Generic.Stack[string]'
    $stack.Push($Directory)
    while ($stack.Count -gt 0) {
        $current = $stack.Pop()
        Safe-Item $Root $current 'package directory' $true | Out-Null
        foreach ($child in Get-ChildItem -LiteralPath $current -Force) {
            if (($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "reparse point is not allowed: $($child.FullName)" }
            if ($child.PSIsContainer) { $stack.Push($child.FullName) }
            elseif ($child.Extension -ieq '.py') { $files += $child }
        }
    }
    return @($files)
}
function Relative-Child([string]$Root, [string]$Path) {
    if (-not (Within-Root $Root $Path) -or (Same-Path $Root $Path)) { throw "path is not a package child" }
    return [System.IO.Path]::GetFullPath($Path).Substring([System.IO.Path]::GetFullPath($Root).TrimEnd([char[]]@('\', '/')).Length).TrimStart([char[]]@('\', '/'))
}
function Content-Equal([string]$Source, [string]$Target) {
    return (Get-Item -LiteralPath $Source -Force).Length -eq (Get-Item -LiteralPath $Target -Force).Length -and (Get-FileHash -Algorithm SHA256 -LiteralPath $Source).Hash -eq (Get-FileHash -Algorithm SHA256 -LiteralPath $Target).Hash
}
function Test-CompleteGpuRuntime([string]$Root) {
    $artifacts = @(
        @{ Path = (Join-Path $Root '.runtime-build\runtimes\ocr-paddle-gpu'); Directory = $true; Label = 'GPU OCR runtime' },
        @{ Path = (Join-Path $Root '.runtime-build\manifests\runtimes\ocr-paddle-gpu.json'); Directory = $false; Label = 'GPU OCR manifest' },
        @{ Path = (Join-Path $Root '.runtime-build\manifests\requirements\ocr-paddle-gpu.lock'); Directory = $false; Label = 'GPU OCR manifest lock' },
        @{ Path = (Join-Path $Root 'packaging\requirements\ocr-paddle-gpu.lock'); Directory = $false; Label = 'GPU OCR lock' },
        @{ Path = (Join-Path $Root 'packaging\wheelhouse\ocr-paddle-gpu'); Directory = $true; Label = 'GPU OCR wheelhouse' }
    )
    $present = @($artifacts | Where-Object { Test-Path -LiteralPath $_.Path })
    if ($present.Count -eq 0) { return $false }
    if ($present.Count -ne $artifacts.Count) { throw 'GPU runtime artifacts are partial; refusing OCR synchronization' }
    foreach ($artifact in $artifacts) { Safe-Item $Root $artifact.Path $artifact.Label $artifact.Directory | Out-Null }
    return $true
}

$project = Safe-Item (Get-Item -LiteralPath $ProjectRoot -Force).FullName (Get-Item -LiteralPath $ProjectRoot -Force).FullName 'project root' $true
$scriptProject = Safe-Item (Join-Path $PSScriptRoot '..\..') (Join-Path $PSScriptRoot '..\..') 'script project root' $true
if ($SkipManifestRefresh -and (Same-Path $project $scriptProject)) { throw 'SkipManifestRefresh is allowed only for an isolated non-project fixture' }
$trees = @(
    @{ Source = 'workers\ocr\src\anima_ocr_worker'; Target = '.runtime-build\runtimes\ocr-paddle\Lib\site-packages\anima_ocr_worker' }
)
$runtimeIds = @('ocr-paddle')
if (Test-CompleteGpuRuntime $project) {
    $trees += @{ Source = 'workers\ocr\src\anima_ocr_worker'; Target = '.runtime-build\runtimes\ocr-paddle-gpu\Lib\site-packages\anima_ocr_worker' }
    $runtimeIds += 'ocr-paddle-gpu'
}
$changes = @()
foreach ($tree in $trees) {
    $source = Safe-Item $project (Join-Path $project $tree.Source) 'OCR source package' $true
    $targetRoot = Join-Path $project $tree.Target
    if (Test-Path -LiteralPath $targetRoot) { $target = Safe-Item $project $targetRoot 'assembled OCR package' $true } else { $target = Planned-Target $project $targetRoot }
    $sourceByRelative = @{}; foreach ($file in Safe-PythonFiles $project $source) { $sourceByRelative[(Relative-Child $source $file.FullName)] = $file }
    $targetByRelative = @{}; if (Test-Path -LiteralPath $target) { foreach ($file in Safe-PythonFiles $project $target) { $targetByRelative[(Relative-Child $target $file.FullName)] = $file } }
    foreach ($relative in $sourceByRelative.Keys) {
        $sourceFile = $sourceByRelative[$relative]; $targetFile = Planned-Target $project (Join-Path $target $relative)
        if (-not $targetByRelative.ContainsKey($relative)) { $changes += [pscustomobject][ordered]@{Action='Add';Source=$sourceFile.FullName;Target=$targetFile;Bytes=[Int64]$sourceFile.Length} }
        elseif (-not (Content-Equal $sourceFile.FullName $targetFile)) { $changes += [pscustomobject][ordered]@{Action='Update';Source=$sourceFile.FullName;Target=$targetFile;Bytes=[Int64]$sourceFile.Length} }
    }
    foreach ($relative in $targetByRelative.Keys) { if (-not $sourceByRelative.ContainsKey($relative)) { $file = $targetByRelative[$relative]; $changes += [pscustomobject][ordered]@{Action='Remove';Source=$null;Target=$file.FullName;Bytes=[Int64]$file.Length} } }
}
$changes = @($changes | Sort-Object Target)
$changes | Select-Object -First 100
$add = @($changes | Where-Object Action -eq 'Add').Count; $update = @($changes | Where-Object Action -eq 'Update').Count; $remove = @($changes | Where-Object Action -eq 'Remove').Count
$bytes = [Int64]0; foreach ($change in $changes) { $bytes += $change.Bytes }
Write-Host "OCR runtime sync plan: $add add, $update update, $remove remove, $bytes bytes."
if (-not $Apply) { return }
foreach ($change in $changes) {
    if ($change.Action -eq 'Remove') { Safe-Item $project $change.Target 'stale assembled module' $false | Out-Null; Remove-Item -LiteralPath $change.Target -Force; continue }
    Safe-Item $project $change.Source 'OCR source module' $false | Out-Null
    $target = Planned-Target $project $change.Target; $directory = [System.IO.Path]::GetDirectoryName($target); New-Item -ItemType Directory -Force -Path $directory | Out-Null
    Safe-Item $project $directory 'assembled OCR package directory' $true | Out-Null
    [System.IO.File]::Copy($change.Source, $target, $true)
}
if ($SkipManifestRefresh) { return }
$toolchain = Safe-Item $project (Join-Path $project '.toolchains\Python-3.11.15\PCbuild\amd64\python.exe') 'toolchain Python' $false
$generator = Safe-Item $project (Join-Path $project 'packaging\scripts\generate_runtime_manifests.py') 'runtime manifest generator' $false
$core = Safe-Item $project (Join-Path $project '.runtime-build\runtimes\core\python.exe') 'embedded Core Python' $false
$drift = Safe-Item $project (Join-Path $project 'tests\contract\test_assembled_tree_drift.py') 'assembled drift test' $false
$manifestArguments = @('--install-root', (Join-Path $project '.runtime-build'), '--requirements-root', (Join-Path $project 'packaging\requirements'), '--include-ocr-paddle', '--runtime-id', 'ocr-paddle')
if ($runtimeIds -contains 'ocr-paddle-gpu') { $manifestArguments += @('--include-ocr-paddle-gpu', '--runtime-id', 'ocr-paddle-gpu') }
& $toolchain -B -I $generator @manifestArguments
if ($LASTEXITCODE -ne 0) { throw "OCR runtime manifest generation failed with exit code $LASTEXITCODE" }
$previousDriftRuntimeIds = $env:ANIMA_DRIFT_RUNTIME_IDS
$hadDriftRuntimeIds = Test-Path -LiteralPath Env:ANIMA_DRIFT_RUNTIME_IDS
${env:ANIMA_DRIFT_RUNTIME_IDS} = ($runtimeIds -join ',')
try {
    & $core -B -I $drift
    if ($LASTEXITCODE -ne 0) { throw "assembled OCR drift verification failed with exit code $LASTEXITCODE" }
}
finally {
    if ($hadDriftRuntimeIds) {
        $env:ANIMA_DRIFT_RUNTIME_IDS = $previousDriftRuntimeIds
    }
    else {
        Remove-Item -LiteralPath Env:ANIMA_DRIFT_RUNTIME_IDS -ErrorAction SilentlyContinue
    }
}
