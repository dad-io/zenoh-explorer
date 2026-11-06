param(
    [Parameter(Mandatory)][string]$Executable,
    [Parameter(Mandatory)][string]$Destination
)

$ErrorActionPreference = 'Stop'
$sourceExe = (Resolve-Path -LiteralPath $Executable).Path
if (Test-Path -LiteralPath $Destination) { throw "Smoke runtime already exists: $Destination" }
New-Item -ItemType Directory -Path $Destination | Out-Null
$runtimeDir = (Resolve-Path -LiteralPath $Destination).Path
$mesaArchive = Join-Path $runtimeDir 'mesa.7z'
$mesaDir = Join-Path $runtimeDir 'mesa'
New-Item -ItemType Directory -Path $mesaDir | Out-Null

# The hosted VM has no hardware OpenGL driver. Use a pinned, verified Mesa build
# only beside a copy of the executable; these DLLs are never put in release assets.
Invoke-WebRequest -Uri 'https://github.com/pal1000/mesa-dist-win/releases/download/26.2.6/mesa3d-26.2.6-release-mingw.7z' -OutFile $mesaArchive -TimeoutSec 120
$mesaHash = (Get-FileHash -LiteralPath $mesaArchive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($mesaHash -ne 'da0f71d9181b672c1918f3c342f11118c687f6059a46b16502845160733cc388') {
    throw 'Mesa archive checksum mismatch'
}
tar -xf $mesaArchive -C $mesaDir
if ($LASTEXITCODE -ne 0) { throw 'Could not unpack Mesa' }
foreach ($mesaDll in @('opengl32.dll', 'libgallium_wgl.dll', 'libdlclose-skip.dll')) {
    Copy-Item -LiteralPath (Join-Path $mesaDir "x64\$mesaDll") -Destination $runtimeDir
}
$runtimeExe = Join-Path $runtimeDir 'zenoh-explorer.exe'
Copy-Item -LiteralPath $sourceExe -Destination $runtimeExe
if ((Get-FileHash -LiteralPath $sourceExe).Hash -ne (Get-FileHash -LiteralPath $runtimeExe).Hash) {
    throw 'Smoke executable differs from the built release executable'
}
Write-Output "Windows smoke runtime ready: $runtimeExe"
