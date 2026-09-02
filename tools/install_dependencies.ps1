param(
    [switch]$SkipMediaTools
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

$DownloadRoot = Join-Path $ProjectRoot "tools\downloads"
New-Item -ItemType Directory -Force -Path $DownloadRoot | Out-Null

$RuntimeDir = Join-Path $ProjectRoot "runtime\python3.13.15"
$ProjectPython = Join-Path $RuntimeDir "python.exe"
$PythonArchive = Join-Path $DownloadRoot "python-3.13.15-amd64.zip"
$PythonUrl = "https://www.python.org/ftp/python/3.13.15/python-3.13.15-amd64.zip"
# SHA-256 for the official CPython 3.13.15 AMD64 ZIP package.
$PythonSha256 = "6479223746cdfb79d25865110d6f524ac98de081324e119af1dc3ae36bddc7a5"
$ReadyMarker = Join-Path $RuntimeDir ".project-ready"

function Get-Sha256 {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    $Algorithm = [Security.Cryptography.SHA256]::Create()
    $Stream = [IO.File]::OpenRead($Path)
    try {
        $Bytes = $Algorithm.ComputeHash($Stream)
    } finally {
        $Stream.Dispose()
        $Algorithm.Dispose()
    }
    return ([BitConverter]::ToString($Bytes) -replace "-", "").ToLowerInvariant()
}

function Assert-Sha256 {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Expected
    )

    $Actual = Get-Sha256 -Path $Path
    if ($Actual -ne $Expected.ToLowerInvariant()) {
        throw "SHA-256 verification failed for $Path. Expected $Expected, got $Actual."
    }
}

function Get-And-VerifyFile {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$Sha256Url
    )

    if (-not (Test-Path -LiteralPath $Destination)) {
        Write-Host "Downloading $Url"
        Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing
    }
    $ChecksumPath = "$Destination.sha256"
    if (-not (Test-Path -LiteralPath $ChecksumPath)) {
        Invoke-WebRequest -Uri $Sha256Url -OutFile $ChecksumPath -UseBasicParsing
    }
    $ChecksumLines = @(Get-Content -LiteralPath $ChecksumPath)
    $FileName = [IO.Path]::GetFileName($Destination)
    $MatchingLine = $ChecksumLines | Where-Object { $_ -match [regex]::Escape($FileName) } | Select-Object -First 1
    if ($MatchingLine) {
        $Expected = ($MatchingLine -split "\s+")[0].ToLowerInvariant()
    } else {
        $Expected = $ChecksumLines[0].Trim().Split()[0].ToLowerInvariant()
    }
    Assert-Sha256 -Path $Destination -Expected $Expected
}

if (-not (Test-Path -LiteralPath $ProjectPython)) {
    if (-not (Test-Path -LiteralPath $PythonArchive)) {
        Write-Host "Downloading project-local Python 3.13.15..."
        Invoke-WebRequest -Uri $PythonUrl -OutFile $PythonArchive -UseBasicParsing
    }
    Assert-Sha256 -Path $PythonArchive -Expected $PythonSha256
    New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
    Write-Host "Extracting project-local Python to $RuntimeDir..."
    Expand-Archive -LiteralPath $PythonArchive -DestinationPath $RuntimeDir -Force
}

if (-not (Test-Path -LiteralPath $ProjectPython)) {
    throw "Project-local Python 3.13.15 was not installed at $ProjectPython"
}

Write-Host "Installing Python packages into the project-local runtime..."
& $ProjectPython -m pip install --disable-pip-version-check -r (Join-Path $ProjectRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Project-local Python dependency installation failed."
}

if (-not $SkipMediaTools) {
    $FfmpegArchive = Join-Path $DownloadRoot "ffmpeg-release-essentials.zip"
    Get-And-VerifyFile `
        -Url "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" `
        -Destination $FfmpegArchive `
        -Sha256Url "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.sha256"

    $FfprobeBin = Join-Path $ProjectRoot "tools\ffmpeg\bin\ffprobe.exe"
    if (-not (Test-Path -LiteralPath $FfprobeBin)) {
        New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "tools\ffmpeg") | Out-Null
        tar.exe -xf $FfmpegArchive -C (Join-Path $ProjectRoot "tools\ffmpeg") --strip-components=1
        if ($LASTEXITCODE -ne 0) {
            throw "FFmpeg archive extraction failed."
        }
    }
}

if (-not $SkipMediaTools) {
    if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot "tools\ffmpeg\bin\ffprobe.exe"))) {
        throw "ffprobe was not installed."
    }
}

if (-not $SkipMediaTools) {
    $RequirementsHash = Get-Sha256 -Path (Join-Path $ProjectRoot "requirements.txt")
    $MarkerText = "Python=3.13.15`r`nRequirementsSHA256=$RequirementsHash`r`n"
    [IO.File]::WriteAllText($ReadyMarker, $MarkerText, [Text.UTF8Encoding]::new($false))
}

Write-Host "Project-local dependencies are ready."
Write-Host "Run with: .\runtime\python3.13.15\python.exe app.py"
