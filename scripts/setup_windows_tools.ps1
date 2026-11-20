param(
    [Parameter(Mandatory = $true)]
    [string]$ToolsDir,

    [switch]$NeedTraceWpp,
    [switch]$NeedClangCl
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null

function Add-ToPath([string]$Dir) {
    if (-not (Test-Path $Dir)) { return }
    $env:PATH = "$Dir;$env:PATH"
    if ($env:GITHUB_PATH) {
        Add-Content -Path $env:GITHUB_PATH -Value $Dir
    }
    Write-Host "Added to PATH: $Dir"
}

# 1. Ensure win_flex and win_bison
$flexDir = Join-Path $ToolsDir "winflexbison"
if (-not (Get-Command win_flex -ErrorAction SilentlyContinue)) {
    if (-not (Test-Path (Join-Path $flexDir "win_flex.exe"))) {
        Write-Host "Downloading winflexbison..."
        $zipPath = Join-Path $ToolsDir "winflexbison.zip"
        $downloadUrl = "https://github.com/lexxmark/winflexbison/releases/download/v2.5.25/win_flex_bison-2.5.25.zip"
        Invoke-WebRequest -Uri $downloadUrl -OutFile $zipPath
        New-Item -ItemType Directory -Force -Path $flexDir | Out-Null
        Expand-Archive -Path $zipPath -DestinationPath $flexDir -Force
        Remove-Item -Force $zipPath
    }
    Add-ToPath $flexDir
}
Write-Host ("win_flex:  " + (Get-Command win_flex).Source)
Write-Host ("win_bison: " + (Get-Command win_bison).Source)

# 2. Ensure tracewpp.exe (required by gallium-mediafoundation)
if ($NeedTraceWpp) {
    $traceWpp = Get-Command tracewpp -ErrorAction SilentlyContinue
    if (-not $traceWpp) {
        # Check installed Windows Kits first
        $kitsRoot = "C:\Program Files (x86)\Windows Kits\10\bin"
        if (Test-Path $kitsRoot) {
            $found = Get-ChildItem -Path $kitsRoot -Filter "tracewpp.exe" -Recurse -ErrorAction SilentlyContinue |
                Where-Object { $_.DirectoryName -match "\\x64$" } |
                Sort-Object FullName -Descending |
                Select-Object -First 1
            if ($found) {
                Add-ToPath $found.DirectoryName
                $traceWpp = Get-Command tracewpp -ErrorAction SilentlyContinue
            }
        }
    }

    if (-not $traceWpp) {
        # Lightweight (~21 MB) fallback via Microsoft.Windows.SDK.BuildTools NuGet package
        $btDir = Join-Path $ToolsDir "sdk-buildtools"
        $found = $null
        if (Test-Path $btDir) {
            $found = Get-ChildItem -Path $btDir -Filter "tracewpp.exe" -Recurse -ErrorAction SilentlyContinue |
                Where-Object { $_.DirectoryName -match "\\x64$" } |
                Sort-Object FullName -Descending |
                Select-Object -First 1
        }
        if (-not $found) {
            Write-Host "Fetching Microsoft.Windows.SDK.BuildTools from NuGet for tracewpp.exe..."
            $index = Invoke-RestMethod -Uri "https://api.nuget.org/v3-flatcontainer/microsoft.windows.sdk.buildtools/index.json"
            $stableVers = $index.versions | Where-Object { $_ -notmatch "-" }
            $ver = ($stableVers | Select-Object -Last 1)
            if (-not $ver) { $ver = ($index.versions | Select-Object -Last 1) }
            Write-Host "Using Microsoft.Windows.SDK.BuildTools $ver"
            $nupkg = Join-Path $ToolsDir "sdk-buildtools.zip"
            $pkgUrl = "https://api.nuget.org/v3-flatcontainer/microsoft.windows.sdk.buildtools/$ver/microsoft.windows.sdk.buildtools.$ver.nupkg"
            Invoke-WebRequest -Uri $pkgUrl -OutFile $nupkg
            New-Item -ItemType Directory -Force -Path $btDir | Out-Null
            Expand-Archive -Path $nupkg -DestinationPath $btDir -Force
            Remove-Item -Force $nupkg
            $found = Get-ChildItem -Path $btDir -Filter "tracewpp.exe" -Recurse -ErrorAction SilentlyContinue |
                Where-Object { $_.DirectoryName -match "\\x64$" } |
                Sort-Object FullName -Descending |
                Select-Object -First 1
        }
        if (-not $found) {
            throw "Failed to locate tracewpp.exe inside Microsoft.Windows.SDK.BuildTools"
        }
        Add-ToPath $found.DirectoryName
    }
    Write-Host ("tracewpp:  " + (Get-Command tracewpp).Source)
}

# 3. Ensure clang-cl when building the clang-cl variant
if ($NeedClangCl) {
    if (-not (Get-Command clang-cl -ErrorAction SilentlyContinue)) {
        $candidates = @(
            "C:\Program Files\LLVM\bin",
            "C:\Program Files\Microsoft Visual Studio\2022\Enterprise\VC\Tools\Llvm\x64\bin",
            "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Tools\Llvm\x64\bin",
            "C:\Program Files\Microsoft Visual Studio\2022\BuildTools\VC\Tools\Llvm\x64\bin"
        )
        foreach ($cand in $candidates) {
            if (Test-Path (Join-Path $cand "clang-cl.exe")) {
                Add-ToPath $cand
                break
            }
        }
    }
    if (-not (Get-Command clang-cl -ErrorAction SilentlyContinue)) {
        throw "clang-cl.exe not found on runner"
    }
    Write-Host ("clang-cl:  " + (Get-Command clang-cl).Source)
    & clang-cl --version
}
