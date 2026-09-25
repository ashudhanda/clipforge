# Fetch static LGPL FFmpeg + ffprobe for the Windows installer (BtbN/FFmpeg-Builds).
# Never commit the binaries — they are downloaded at build time only.
$ErrorActionPreference = "Stop"

$OutDir = Join-Path $PSScriptRoot "bin"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
Set-Location $OutDir

Write-Host "→ finding latest BtbN FFmpeg-Builds release…"
$rel = Invoke-RestMethod "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"
$asset = $rel.assets | Where-Object { $_.name -match "win64-lgpl.*\.zip$" } | Select-Object -First 1
if (-not $asset) { throw "no matching win64-lgpl asset found" }

Write-Host "→ downloading $($asset.name)…"
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile "package.zip"

Write-Host "→ extracting ffmpeg.exe + ffprobe.exe…"
$stage = Join-Path $OutDir "stage"
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
Expand-Archive "package.zip" -DestinationPath $stage
$ff = Get-ChildItem -Recurse -Path $stage -Filter "ffmpeg.exe" | Select-Object -First 1
$fp = Get-ChildItem -Recurse -Path $stage -Filter "ffprobe.exe" | Select-Object -First 1
Copy-Item $ff.FullName (Join-Path $OutDir "ffmpeg.exe") -Force
Copy-Item $fp.FullName (Join-Path $OutDir "ffprobe.exe") -Force
Remove-Item -Recurse -Force $stage, "package.zip"
Write-Host "✓ ffmpeg.exe + ffprobe.exe ready in $OutDir"
& (Join-Path $OutDir "ffmpeg.exe") -hide_banner -version | Select-Object -First 1
