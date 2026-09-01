param(
    [switch]$SkipFfmpeg,
    [switch]$SkipMediaKit
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host "Creating project-local Python environment..."
    python -m venv (Join-Path $ProjectRoot ".venv")
}

Write-Host "Installing Python packages into .venv..."
& $VenvPython -m pip install -r (Join-Path $ProjectRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Project-local Python dependency installation failed."
}

$DownloadRoot = Join-Path $ProjectRoot "tools\downloads"
New-Item -ItemType Directory -Force -Path $DownloadRoot | Out-Null

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
    $Actual = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Expected -ne $Actual) {
        throw "SHA-256 verification failed for $Destination"
    }
}

if (-not $SkipFfmpeg) {
    $FfmpegArchive = Join-Path $DownloadRoot "ffmpeg-release-essentials.zip"
    Get-And-VerifyFile `
        -Url "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" `
        -Destination $FfmpegArchive `
        -Sha256Url "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.sha256"

    $FfmpegBin = Join-Path $ProjectRoot "tools\ffmpeg\bin\ffmpeg.exe"
    $FfprobeBin = Join-Path $ProjectRoot "tools\ffmpeg\bin\ffprobe.exe"
    if (-not (Test-Path -LiteralPath $FfmpegBin) -or -not (Test-Path -LiteralPath $FfprobeBin)) {
        New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "tools\ffmpeg") | Out-Null
        tar.exe -xf $FfmpegArchive -C (Join-Path $ProjectRoot "tools\ffmpeg") --strip-components=1
        if ($LASTEXITCODE -ne 0) {
            throw "FFmpeg archive extraction failed."
        }
    }
}

if (-not $SkipMediaKit) {
    $MediaKitArchive = Join-Path $DownloadRoot "mediakit-cli_0.2.1_windows_amd64.zip"
    Get-And-VerifyFile `
        -Url "https://github.com/volcengine/mediakit-cli/releases/download/v0.2.1/mediakit-cli_0.2.1_windows_amd64.zip" `
        -Destination $MediaKitArchive `
        -Sha256Url "https://github.com/volcengine/mediakit-cli/releases/download/v0.2.1/checksums.txt"

    $MediaKitBin = Join-Path $ProjectRoot "tools\mediakit\mediakit-cli.exe"
    if (-not (Test-Path -LiteralPath $MediaKitBin)) {
        New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "tools\mediakit") | Out-Null
        tar.exe -xf $MediaKitArchive -C (Join-Path $ProjectRoot "tools\mediakit")
        if ($LASTEXITCODE -ne 0) {
            throw "MediaKit CLI archive extraction failed."
        }
    }

    Write-Host "Registering the MediaKit npm package locally without a global install..."
    npm install --save-exact --ignore-scripts @volcengine/mediakit-cli@0.2.1
    if ($LASTEXITCODE -ne 0) {
        throw "Local MediaKit npm package installation failed."
    }
}

Write-Host "Project-local dependencies are ready."
Write-Host "Run with: .\.venv\Scripts\python.exe app.py"
