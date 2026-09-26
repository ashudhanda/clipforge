# Fetch static LGPL FFmpeg + ffprobe for the Windows installer (BtbN/FFmpeg-Builds).
# Never commit the binaries -- they are downloaded at build time only.
$ErrorActionPreference = "Stop"

$OutDir = Join-Path $PSScriptRoot "bin"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
Set-Location $OutDir

# Authenticated API calls get a much higher rate limit on shared CI runners.
$headers = @{ "User-Agent" = "clipforge-installer-build" }
if ($env:GITHUB_TOKEN) { $headers["Authorization"] = "Bearer $($env:GITHUB_TOKEN)" }

# Pinned BtbN build tag — NOT "latest". The rolling master changes daily
# (fresh unsigned binaries = maximum antivirus false-positive risk and
# zero reproducibility). Bump deliberately after testing a new build.
# Override: $env:FFMPEG_BUILD_TAG
$BuildTag = $env:FFMPEG_BUILD_TAG
if (-not $BuildTag) { $BuildTag = "autobuild-2026-09-25-15-37" }

Write-Host "-> finding BtbN FFmpeg-Builds release for tag $BuildTag ..."
# Retry: shared CI runners hit api.github.com rate limits and transient
# network blips. Authenticated (GITHUB_TOKEN) calls get a far higher quota;
# retries cover the rest.
$rel = $null
for ($attempt = 1; $attempt -le 3 -and -not $rel; $attempt++) {
    try {
        $rel = Invoke-RestMethod -Headers $headers "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/tags/$BuildTag"
    } catch {
        Write-Warning "release lookup attempt $attempt/3 failed: $($_.Exception.Message)"
        if ($attempt -eq 3) { throw }
        Start-Sleep -Seconds (10 * $attempt)
    }
}
$asset = $rel.assets | Where-Object { $_.name -match "win64-lgpl\.zip$" } | Select-Object -First 1
if (-not $asset) { throw "no matching win64-lgpl static asset found for tag $BuildTag" }

Write-Host "-> downloading $($asset.name)..."
for ($attempt = 1; ; $attempt++) {
    try {
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile "package.zip"
        break
    } catch {
        Write-Warning "download attempt $attempt/3 failed: $($_.Exception.Message)"
        if ($attempt -eq 3) { throw }
        Start-Sleep -Seconds (15 * $attempt)
    }
}

Write-Host "-> extracting ffmpeg.exe + ffprobe.exe..."
$stage = Join-Path $OutDir "stage"
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
Expand-Archive "package.zip" -DestinationPath $stage
$ff = Get-ChildItem -Recurse -Path $stage -Filter "ffmpeg.exe" | Select-Object -First 1
$fp = Get-ChildItem -Recurse -Path $stage -Filter "ffprobe.exe" | Select-Object -First 1
Copy-Item $ff.FullName (Join-Path $OutDir "ffmpeg.exe") -Force
Copy-Item $fp.FullName (Join-Path $OutDir "ffprobe.exe") -Force
Remove-Item -Recurse -Force $stage, "package.zip"
Write-Host "OK: ffmpeg.exe + ffprobe.exe ready in $OutDir"
$ffmpegExe = Join-Path $OutDir "ffmpeg.exe"
& $ffmpegExe -hide_banner -version | Select-Object -First 1
