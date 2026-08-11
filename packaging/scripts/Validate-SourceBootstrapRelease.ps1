[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$ProjectRoot
)

$ErrorActionPreference = 'Stop'

function Stop-ReleaseGate([string]$Message) {
    throw "Source-bootstrap release gate failed: $Message"
}

function Read-JsonFile([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        Stop-ReleaseGate "$Label is missing"
    }
    try {
        return [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
    } catch {
        Stop-ReleaseGate "$Label is not valid JSON"
    }
}

function Get-RequiredProperty([object]$Value, [string]$Name, [string]$Label) {
    $property = $Value.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value) {
        Stop-ReleaseGate "$Label is missing required field: $Name"
    }
    return $property.Value
}

function Assert-SafeRelative([string]$Value, [string]$Label) {
    if ([string]::IsNullOrWhiteSpace($Value)) { Stop-ReleaseGate "$Label is empty" }
    $normalized = $Value.Replace('/', '\')
    if ($normalized.StartsWith('\') -or $normalized -match '^[A-Za-z]:' -or [System.IO.Path]::IsPathRooted($normalized)) {
        Stop-ReleaseGate "$Label is not a safe relative path: $Value"
    }
    foreach ($part in ($normalized -split '\\')) {
        $device = ($part -split '\.')[0].ToUpperInvariant()
        if ([string]::IsNullOrEmpty($part) -or $part -in @('.', '..') -or $part.Contains(':') -or
            $part.EndsWith('.') -or $part.EndsWith(' ') -or
            $device -in @('CON', 'PRN', 'AUX', 'NUL', 'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9', 'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9')) {
            Stop-ReleaseGate "$Label is not a safe relative path: $Value"
        }
    }
    return $normalized
}

function Assert-Artifact([object]$Artifact, [string[]]$TopLevelHosts, [string]$Label) {
    foreach ($name in @('id', 'url', 'allowedHosts', 'sizeBytes', 'sha256', 'relativePath')) {
        [void](Get-RequiredProperty $Artifact $name $Label)
    }
    $url = [string]$Artifact.url
    try { $parsed = [System.Uri]$url } catch { Stop-ReleaseGate "$Label URL is invalid: $url" }
    if ($parsed.Scheme -ne 'https' -or [string]::IsNullOrEmpty($parsed.Host) -or $parsed.UserInfo) {
        Stop-ReleaseGate "$Label URL must use HTTPS without credentials: $url"
    }
    $artifactHosts = @($Artifact.allowedHosts | ForEach-Object { ([string]$_).ToLowerInvariant() })
    if ($artifactHosts.Count -eq 0 -or $artifactHosts.Count -ne (@($artifactHosts | Select-Object -Unique).Count)) {
        Stop-ReleaseGate "$Label allowed hosts are invalid"
    }
    foreach ($host in $artifactHosts) {
        if ($TopLevelHosts -notcontains $host) { Stop-ReleaseGate "$Label allowed host is not in the manifest allowlist: $host" }
    }
    if ($artifactHosts -notcontains $parsed.Host.ToLowerInvariant()) {
        Stop-ReleaseGate "$Label URL host is not allowed: $($parsed.Host)"
    }
    if ([Int64]$Artifact.sizeBytes -le 0) { Stop-ReleaseGate "$Label sizeBytes is invalid" }
    if ([string]$Artifact.sha256 -notmatch '^[a-f0-9]{64}$') { Stop-ReleaseGate "$Label SHA-256 is invalid" }
    [void](Assert-SafeRelative ([string]$Artifact.relativePath) "$Label relativePath")
    $hasRepository = $null -ne $Artifact.PSObject.Properties['repository']
    $hasRevision = $null -ne $Artifact.PSObject.Properties['revision']
    if ($hasRepository -or $hasRevision) {
        if (-not $hasRepository -or -not $hasRevision -or [string]::IsNullOrWhiteSpace([string]$Artifact.repository) -or
            [string]$Artifact.revision -notmatch '^[a-f0-9]{40}$') {
            Stop-ReleaseGate "$Label Hugging Face identity is incomplete"
        }
        if ($parsed.Host.ToLowerInvariant() -ne 'huggingface.co' -or
            -not $parsed.AbsolutePath.StartsWith('/' + [string]$Artifact.repository + '/resolve/' + [string]$Artifact.revision + '/', [System.StringComparison]::Ordinal)) {
            Stop-ReleaseGate "$Label does not use its full Hugging Face revision"
        }
    }
}

function Assert-Manifest([object]$Manifest) {
    $expectedFields = @('schemaVersion', 'releaseVersion', 'sourceCommit', 'allowedHosts', 'bootstrap', 'components', 'cleanup')
    $actualFields = @($Manifest.PSObject.Properties.Name)
    if ($actualFields.Count -ne $expectedFields.Count -or @($actualFields | Where-Object { $expectedFields -notcontains $_ }).Count -gt 0) {
        Stop-ReleaseGate 'install manifest top-level fields are invalid'
    }
    if ([Int32]$Manifest.schemaVersion -ne 1) { Stop-ReleaseGate 'install manifest schemaVersion is not 1' }
    $releaseVersion = [string](Get-RequiredProperty $Manifest 'releaseVersion' 'install manifest')
    if ([string]::IsNullOrWhiteSpace($releaseVersion)) { Stop-ReleaseGate 'install manifest releaseVersion is empty' }
    if ([string]$Manifest.sourceCommit -notmatch '^[a-f0-9]{40}$') { Stop-ReleaseGate 'install manifest sourceCommit is not a full commit SHA' }
    $hosts = @($Manifest.allowedHosts | ForEach-Object { ([string]$_).ToLowerInvariant() })
    if ($hosts.Count -eq 0 -or $hosts.Count -ne (@($hosts | Select-Object -Unique).Count)) { Stop-ReleaseGate 'install manifest allowedHosts are invalid' }

    $bootstrap = Get-RequiredProperty $Manifest 'bootstrap' 'install manifest'
    $bootstrapArtifact = Get-RequiredProperty $bootstrap 'artifact' 'bootstrap'
    Assert-Artifact $bootstrapArtifact $hosts 'bootstrap artifact'
    [void](Assert-SafeRelative ([string](Get-RequiredProperty $bootstrap 'entryRelativePath' 'bootstrap')) 'bootstrap entryRelativePath')
    if ([Int64](Get-RequiredProperty $bootstrap 'peakBytes' 'bootstrap') -le 0) { Stop-ReleaseGate 'bootstrap peakBytes is invalid' }

    $mandatory = @('core', 'caption-e621', 'classify-e621', 'replace-e621', 'nl', 'policy', 'export', 'token-budget', 'ocr-cpu', 'e621-indexes', 'e621-tagger', 'qwen3-tokenizer', 'quality-stack', 'ocr-models')
    $components = @($Manifest.components)
    if ($components.Count -eq 0) { Stop-ReleaseGate 'install manifest has no components' }
    $ids = @{}
    $targets = @{}
    $artifactTargets = @{}
    foreach ($component in $components) {
        $id = [string](Get-RequiredProperty $component 'componentId' 'component')
        if ($ids.ContainsKey($id)) { Stop-ReleaseGate "duplicate componentId: $id" }
        $ids[$id] = $true
        $target = Assert-SafeRelative ([string](Get-RequiredProperty $component 'targetRelativePath' "component $id")) "component $id targetRelativePath"
        if ($targets.ContainsKey($target.ToLowerInvariant())) { Stop-ReleaseGate "duplicate component target: $target" }
        $targets[$target.ToLowerInvariant()] = $true
        $variants = Get-RequiredProperty $component 'variants' "component $id"
        foreach ($variantProperty in $variants.PSObject.Properties) {
            $variant = $variantProperty.Name
            if ($variant -notin @('cpu', 'cuda', 'shared')) { Stop-ReleaseGate "component $id has an invalid variant: $variant" }
            $record = $variantProperty.Value
            if ([Int64](Get-RequiredProperty $record 'peakBytes' "component $id/$variant") -le 0) { Stop-ReleaseGate "component $id/$variant peakBytes is invalid" }
            $artifacts = @((Get-RequiredProperty $record 'artifacts' "component $id/$variant"))
            if ($artifacts.Count -eq 0) { Stop-ReleaseGate "component $id/$variant has no artifacts" }
            foreach ($artifact in $artifacts) {
                Assert-Artifact $artifact $hosts "component $id/$variant artifact"
                $artifactTarget = ([string]$artifact.relativePath).Replace('/', '\').ToLowerInvariant()
                if ($artifactTargets.ContainsKey($artifactTarget)) { Stop-ReleaseGate "duplicate artifact target: $artifactTarget" }
                $artifactTargets[$artifactTarget] = $true
                if ($variant -eq 'cpu' -and ([string]$artifact.id + ' ' + [string]$artifact.url) -match '(?i)(cuda|[+]cu[0-9]*|cudnn|nvidia[-_])') {
                    Stop-ReleaseGate "CPU variant contains a CUDA artifact: $id"
                }
            }
        }
    }
    foreach ($id in $mandatory) { if (-not $ids.ContainsKey($id)) { Stop-ReleaseGate "mandatory component is missing: $id" } }
    return $releaseVersion
}

function Assert-ReleaseArtifacts([object]$Release, [string]$ManifestVersion, [string[]]$ManifestHosts, [object]$BootstrapArtifact) {
    $expectedFields = @('schemaVersion', 'releaseVersion', 'artifacts')
    $actualFields = @($Release.PSObject.Properties.Name)
    if ($actualFields.Count -ne $expectedFields.Count -or @($actualFields | Where-Object { $expectedFields -notcontains $_ }).Count -gt 0) {
        Stop-ReleaseGate 'release-artifacts.json top-level fields are invalid'
    }
    if ([Int32]$Release.schemaVersion -ne 1 -or [string]$Release.releaseVersion -ne $ManifestVersion) {
        Stop-ReleaseGate 'release-artifacts.json does not match the install manifest releaseVersion'
    }
    $records = @($Release.artifacts)
    if ($records.Count -eq 0) { Stop-ReleaseGate 'release-artifacts.json has no published artifacts' }
    $ids = @{}
    $bootstrapRecord = $null
    foreach ($record in $records) {
        $id = [string](Get-RequiredProperty $record 'id' 'release artifact')
        if ($ids.ContainsKey($id)) { Stop-ReleaseGate "duplicate release artifact id: $id" }
        $ids[$id] = $true
        $url = [string](Get-RequiredProperty $record 'publishedUrl' "release artifact $id")
        try { $parsed = [System.Uri]$url } catch { Stop-ReleaseGate "release artifact URL is invalid: $url" }
        if ($parsed.Scheme -ne 'https' -or $parsed.UserInfo) { Stop-ReleaseGate "release artifact URL must use HTTPS: $url" }
        if ($ManifestHosts -notcontains $parsed.Host.ToLowerInvariant()) { Stop-ReleaseGate "release artifact URL host is not allowed: $($parsed.Host)" }
        if ([Int64](Get-RequiredProperty $record 'sizeBytes' "release artifact $id") -le 0 -or [string]$record.sha256 -notmatch '^[a-f0-9]{64}$') {
            Stop-ReleaseGate "release artifact identity is incomplete: $id"
        }
        if ($id -eq [string]$BootstrapArtifact.id) { $bootstrapRecord = $record }
    }
    if ($null -eq $bootstrapRecord) { Stop-ReleaseGate 'bootstrap artifact has no published Release identity' }
    if ([string]$bootstrapRecord.publishedUrl -ne [string]$BootstrapArtifact.url -or
        [Int64]$bootstrapRecord.sizeBytes -ne [Int64]$BootstrapArtifact.sizeBytes -or
        [string]$bootstrapRecord.sha256 -ne [string]$BootstrapArtifact.sha256) {
        Stop-ReleaseGate 'bootstrap artifact does not match release-artifacts.json'
    }
}

try {
    $root = [System.IO.Path]::GetFullPath($ProjectRoot)
    $manifestPath = Join-Path $root 'packaging\installer\install-manifest.json'
    $releasePath = Join-Path $root 'packaging\installer\release-artifacts.json'
    $frontendIndex = Join-Path $root 'frontend\dist\index.html'
    $noticesPath = Join-Path $root 'docs\THIRD_PARTY_NOTICES.md'
    $manifest = Read-JsonFile $manifestPath 'install-manifest.json'
    $release = Read-JsonFile $releasePath 'release-artifacts.json'
    $manifestVersion = Assert-Manifest $manifest
    $manifestHosts = @($manifest.allowedHosts | ForEach-Object { ([string]$_).ToLowerInvariant() })
    Assert-ReleaseArtifacts $release $manifestVersion $manifestHosts $manifest.bootstrap.artifact
    if (-not (Test-Path -LiteralPath $frontendIndex -PathType Leaf)) { Stop-ReleaseGate 'frontend/dist/index.html is missing' }
    if (-not (Test-Path -LiteralPath $noticesPath -PathType Leaf)) { Stop-ReleaseGate 'third-party notices are missing' }
    $notices = [System.IO.File]::ReadAllText($noticesPath, [System.Text.Encoding]::UTF8)
    if ($notices -match '(?i)license\s+unverified') { Stop-ReleaseGate 'third-party notices still contain an unverified license status' }
    Write-Output ("Source-bootstrap release gate passed for {0}; frontend/dist and published bootstrap identity are present." -f $manifestVersion)
    exit 0
} catch {
    Write-Output $_.Exception.Message
    exit 1
}
