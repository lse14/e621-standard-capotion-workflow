[CmdletBinding()]
param(
    [switch]$Run,
    [switch]$Reset,
    [switch]$Resume,
    [string]$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
)

$ErrorActionPreference = 'Stop'

if ($Reset -and $Resume) {
    throw '-Reset and -Resume cannot be used together.'
}

function Test-PathWithin([string]$Root, [string]$Candidate) {
    $normalizedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd([char[]]@('\', '/'))
    $normalizedCandidate = [System.IO.Path]::GetFullPath($Candidate).TrimEnd([char[]]@('\', '/'))
    if ([System.StringComparer]::OrdinalIgnoreCase.Equals($normalizedRoot, $normalizedCandidate)) {
        return $true
    }
    $prefix = $normalizedRoot + [System.IO.Path]::DirectorySeparatorChar
    return $normalizedCandidate.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
}

function Assert-ExistingProjectPath([string]$Root, [string]$Path, [string]$Label, [bool]$Directory) {
    $item = Get-Item -LiteralPath $Path -Force
    if ($Directory -and -not $item.PSIsContainer) {
        throw "$Label must be a directory: $($item.FullName)"
    }
    if (-not $Directory -and $item.PSIsContainer) {
        throw "$Label must be a file: $($item.FullName)"
    }
    if (-not (Test-PathWithin $Root $item.FullName)) {
        throw "$Label escapes project root: $($item.FullName)"
    }
    $current = $item.FullName
    while ($true) {
        $currentItem = Get-Item -LiteralPath $current -Force
        if (($currentItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "reparse point is not allowed: $($currentItem.FullName)"
        }
        if ([System.StringComparer]::OrdinalIgnoreCase.Equals($current, $Root)) {
            break
        }
        $parent = [System.IO.Path]::GetDirectoryName($current)
        if (-not $parent -or [System.StringComparer]::OrdinalIgnoreCase.Equals($parent, $current)) {
            throw "$Label escapes project root: $($item.FullName)"
        }
        $current = $parent
    }
    return $item.FullName
}

function Invoke-ProjectPython([string]$Executable, [string[]]$Arguments) {
    & $Executable -B -I @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "OCR compatibility driver failed with exit code $LASTEXITCODE"
    }
}

$projectItem = Get-Item -LiteralPath $ProjectRoot -Force
if (-not $projectItem.PSIsContainer) {
    throw "project root must be a directory: $($projectItem.FullName)"
}
if (($projectItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw "reparse point is not allowed: $($projectItem.FullName)"
}
$project = $projectItem.FullName
$projectPython = Assert-ExistingProjectPath $project (Join-Path $project '.toolchains\Python-3.11.15\PCbuild\amd64\python.exe') 'project Python' $false
$driver = Assert-ExistingProjectPath $project (Join-Path $project 'packaging\scripts\ocr_compatibility.py') 'OCR compatibility driver' $false
$cleanup = Assert-ExistingProjectPath $project (Join-Path $project 'packaging\scripts\Clean-OcrImport.ps1') 'OCR cleanup script' $false
$workingRoot = [System.IO.Path]::GetFullPath((Join-Path $project '.runtime-build\ocr-import'))
$environment = Join-Path $workingRoot 'environment'
$downloads = Join-Path $workingRoot 'downloads'
$conversionCache = Join-Path $workingRoot 'conversion-cache'
$evidence = Join-Path $workingRoot 'evidence'
$converterEnvironment = Join-Path $conversionCache 'converter-environment'
$evidenceFile = Join-Path $evidence 'compatibility-v2.json'

$models = @(
    [pscustomobject][ordered]@{
        Name = 'PP-OCRv5_server_det'
        Url = 'https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-OCRv5_server_det_infer.tar'
        Size = [Int64]88340480
        Sha256 = '22a33e0ba6a21425ea4192da03bf4395c9a0c67902bd924b7328fc859073045d'
    },
    [pscustomobject][ordered]@{
        Name = 'PP-OCRv5_server_rec'
        Url = 'https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-OCRv5_server_rec_infer.tar'
        Size = [Int64]84869120
        Sha256 = 'd99be2ffd348943ab52876179168be4fb5b14f5f0812f2ae4c76d89ec2ea750a'
    },
    [pscustomobject][ordered]@{
        Name = 'PP-LCNet_x1_0_textline_ori'
        Url = 'https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-LCNet_x1_0_textline_ori_infer.tar'
        Size = [Int64]6871040
        Sha256 = '6171f69605215a85624d650e9079fa45f7c3eaf944296181bcc5395bf3ddc7f6'
    }
)

$samples = @(
    [pscustomobject][ordered]@{
        Purpose = 'text_detection'
        Url = 'https://paddle-model-ecology.bj.bcebos.com/paddlex/imgs/demo_image/general_ocr_001.png'
        Size = [Int64]398527
        Sha256 = '3ac37804e4e292f68c8960d553485147516cdc2e4154afeec6ca742a70e71dca'
    },
    [pscustomobject][ordered]@{
        Purpose = 'text_recognition'
        Url = 'https://paddle-model-ecology.bj.bcebos.com/paddlex/imgs/demo_image/general_ocr_rec_001.png'
        Size = [Int64]73730
        Sha256 = '5362ba97741413494c507237b5096ef09ed575a501c4d9e68bfeffe17528a6ad'
    },
    [pscustomobject][ordered]@{
        Purpose = 'textline_orientation'
        Url = 'https://paddle-model-ecology.bj.bcebos.com/paddlex/imgs/demo_image/textline_rot180_demo.jpg'
        Size = [Int64]3996
        Sha256 = '872200f57a1408e7aab2856d5f2c687b3a937805e0c4ff74bd7de21df1f742b9'
    }
)

$plan = [pscustomobject][ordered]@{
    Action = 'TestOcrCompatibility'
    Mode = if ($Run) { 'Run' } else { 'Preview' }
    ProjectPython = $projectPython
    WorkingRoot = $workingRoot
    Environment = $environment
    ConverterEnvironment = $converterEnvironment
    Downloads = $downloads
    ConversionCache = $conversionCache
    Evidence = $evidence
    EvidenceFile = $evidenceFile
    CandidatePackages = [ordered]@{
        paddleocr = '3.7.0'
        'paddlex[ocr-core]' = '3.7.2'
        'onnxruntime-gpu' = '1.26.0'
    }
    Converter = [pscustomobject][ordered]@{
        Package = 'paddle2onnx'
        Version = '2.1.0'
        WheelSha256 = '478993e17ed0212b79a4d6e2d8d0582ebb19c7230b7f365d51222833e98581b3'
        SourceCommit = 'c8b5048c3a0903986bd3ec1cce2af9915b391c49'
    }
    CompatibilityPaths = @(
        [pscustomobject][ordered]@{
            Name = 'official-build-nightly'
            PaddleWheel = [pscustomobject][ordered]@{
                Requirement = 'paddlepaddle==3.0.0.dev20250426'
                Url = 'https://paddle-whl.bj.bcebos.com/nightly/cpu/paddlepaddle/paddlepaddle-3.0.0.dev20250426-cp311-cp311-win_amd64.whl'
                Size = [Int64]98376372
                Sha256 = 'f62aaab2bd8d3ad4f4f7781bdeed43403546057b7afcdc10a4b33847b2617f1f'
            }
        },
        [pscustomobject][ordered]@{
            Name = 'local-v2.1.0-source-build'
            Repository = 'https://github.com/PaddlePaddle/Paddle2ONNX.git'
            Tag = 'v2.1.0'
            Commit = 'c8b5048c3a0903986bd3ec1cce2af9915b391c49'
            BuildGuide = 'https://github.com/PaddlePaddle/Paddle2ONNX/blob/v2.1.0/docs/zh/compile_local.md'
        }
    )
    Models = $models
    Samples = $samples
    Reset = [bool]$Reset
    Resume = [bool]$Resume
}
Write-Output $plan
Write-Host ("OCR compatibility plan: mode {0}, reset {1}, working root {2}." -f $plan.Mode, [bool]$Reset, $workingRoot)

if (-not $Run) {
    return
}

if ($Reset) {
    & $cleanup -ProjectRoot $project -Apply | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "OCR cleanup failed with exit code $LASTEXITCODE"
    }
}

$arguments = @(
    $driver,
    '--project-root', $project,
    '--toolchain-python', $projectPython,
    '--working-root', $workingRoot
)
if ($Resume) {
    $arguments += '--resume'
}
Invoke-ProjectPython $projectPython $arguments
