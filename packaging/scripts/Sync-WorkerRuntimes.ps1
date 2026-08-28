[CmdletBinding()]
param(
    [switch]$Apply,
    [string]$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
)

$ErrorActionPreference = 'Stop'
$project = (Get-Item -LiteralPath $ProjectRoot -Force).FullName
$scriptRoot = (Get-Item -LiteralPath (Join-Path $PSScriptRoot '..\..') -Force).FullName
if ([System.StringComparer]::OrdinalIgnoreCase.Equals($project.TrimEnd('\'), $scriptRoot.TrimEnd('\')) -eq $false) {
    throw 'Sync-WorkerRuntimes must run against the project root'
}

$trees = @(
    @{ Runtime = 'caption-e621'; Source = 'workers\caption\src\anima_caption_worker'; Package = 'anima_caption_worker' },
    @{ Runtime = 'classify-e621'; Source = 'workers\classify\src\anima_classify_worker'; Package = 'anima_classify_worker' },
    @{ Runtime = 'replace-e621'; Source = 'workers\replace\src\anima_replace_worker'; Package = 'anima_replace_worker' },
    @{ Runtime = 'nl'; Source = 'workers\nl\src\anima_nl_worker'; Package = 'anima_nl_worker' },
    @{ Runtime = 'policy'; Source = 'workers\policy\src\anima_policy_worker'; Package = 'anima_policy_worker' }
)

$changes = @()
foreach ($tree in $trees) {
    $sourceRoot = Join-Path $project $tree.Source
    $targetRoot = Join-Path $project (".runtime-build\runtimes\{0}\Lib\site-packages\{1}" -f $tree.Runtime, $tree.Package)
    if (-not (Test-Path -LiteralPath $sourceRoot -PathType Container)) { throw "source package missing: $sourceRoot" }
    if (-not (Test-Path -LiteralPath $targetRoot -PathType Container)) { throw "assembled package missing: $targetRoot" }
    $sourceFiles = @{}
    Get-ChildItem -LiteralPath $sourceRoot -File -Recurse -Filter '*.py' | ForEach-Object {
        $sourceFiles[$_.FullName.Substring($sourceRoot.Length).TrimStart('\')] = $_
    }
    $targetFiles = @{}
    Get-ChildItem -LiteralPath $targetRoot -File -Recurse -Filter '*.py' | ForEach-Object {
        if ($_.FullName -notmatch '\\__pycache__\\') { $targetFiles[$_.FullName.Substring($targetRoot.Length).TrimStart('\')] = $_ }
    }
    foreach ($relative in $sourceFiles.Keys) {
        $source = $sourceFiles[$relative]
        $target = Join-Path $targetRoot $relative
        if (-not $targetFiles.ContainsKey($relative)) {
            $changes += [pscustomobject]@{ Action = 'Add'; Runtime = $tree.Runtime; Source = $source.FullName; Target = $target }
        }
        elseif ((Get-FileHash -Algorithm SHA256 -LiteralPath $source.FullName).Hash -ne (Get-FileHash -Algorithm SHA256 -LiteralPath $target).Hash) {
            $changes += [pscustomobject]@{ Action = 'Update'; Runtime = $tree.Runtime; Source = $source.FullName; Target = $target }
        }
    }
    foreach ($relative in $targetFiles.Keys) {
        if (-not $sourceFiles.ContainsKey($relative)) {
            $changes += [pscustomobject]@{ Action = 'Remove'; Runtime = $tree.Runtime; Source = $null; Target = $targetFiles[$relative].FullName }
        }
    }
}

$changes = @($changes | Sort-Object Target)
$changes | Format-Table -AutoSize
Write-Host ("Worker runtime sync plan: {0} add, {1} update, {2} remove." -f (@($changes | Where-Object Action -eq 'Add').Count, @($changes | Where-Object Action -eq 'Update').Count, @($changes | Where-Object Action -eq 'Remove').Count))
if (-not $Apply) { return }
foreach ($change in $changes) {
    if ($change.Action -eq 'Remove') {
        Remove-Item -LiteralPath $change.Target -Force
        continue
    }
    $parent = [System.IO.Path]::GetDirectoryName($change.Target)
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    [System.IO.File]::Copy($change.Source, $change.Target, $true)
}

$toolchain = Join-Path $project '.toolchains\Python-3.11.15\PCbuild\amd64\python.exe'
$generator = Join-Path $project 'packaging\scripts\generate_runtime_manifests.py'
$installRoot = Join-Path $project '.runtime-build'
$requirementsRoot = Join-Path $project 'packaging\requirements'
foreach ($runtime in $trees.Runtime) {
    & $toolchain -B -I $generator --install-root $installRoot --requirements-root $requirementsRoot --runtime-id $runtime
    if ($LASTEXITCODE -ne 0) { throw "runtime manifest generation failed for $runtime with exit code $LASTEXITCODE" }
}
