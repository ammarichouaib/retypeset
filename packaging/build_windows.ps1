<#
    Build the Windows application. Run from the repository root, in PowerShell:

        powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1

    Produces:
        dist\retypeset\retypeset.exe        the application (one folder)
        dist\retypeset-0.8.0-win64.zip      portable, no admin rights needed
        dist\retypeset-setup-0.8.0.exe      installer, if Inno Setup is present

    Options:
        -WithSklearn     include scikit-learn (+~120 MB) so local training works
                         inside the .exe. Without it the app runs identically
                         and the Training panel says the library is missing.
        -NoPandoc        do not bundle pandoc (-180 MB, but the app then needs
                         pandoc installed on the target machine)
#>
param(
    [switch]$WithSklearn,
    [switch]$NoPandoc
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$version = (Select-String -Path retypeset\__init__.py -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
Write-Host "Building retypeset $version" -ForegroundColor Cyan

# 1. Build environment, kept separate from the one you develop in.
if (-not (Test-Path .venv-build)) { python -m venv .venv-build }
& .\.venv-build\Scripts\python.exe -m pip install --upgrade pip wheel --quiet
& .\.venv-build\Scripts\python.exe -m pip install -r requirements.txt --quiet
& .\.venv-build\Scripts\python.exe -m pip install pyinstaller --quiet
if (-not $WithSklearn) {
    # Present in requirements.txt for the source install; excluded here on size.
    & .\.venv-build\Scripts\python.exe -m pip uninstall -y scikit-learn scipy --quiet
}

# 2. Pandoc. Downloaded rather than redistributed from this repository, so the
#    licence stays where it belongs and the version is visible in the log.
if (-not $NoPandoc) {
    $pandocVersion = "3.10.1"
    $pandocDir = "build\pandoc"
    if (-not (Test-Path "$pandocDir\pandoc.exe")) {
        New-Item -ItemType Directory -Force -Path build | Out-Null
        $url = "https://github.com/jgm/pandoc/releases/download/$pandocVersion/pandoc-$pandocVersion-windows-x86_64.zip"
        Write-Host "Downloading pandoc $pandocVersion"
        Invoke-WebRequest -Uri $url -OutFile build\pandoc.zip
        Expand-Archive build\pandoc.zip -DestinationPath build\pandoc-tmp -Force
        New-Item -ItemType Directory -Force -Path $pandocDir | Out-Null
        Copy-Item (Get-ChildItem build\pandoc-tmp -Recurse -Filter pandoc.exe)[0].FullName $pandocDir
        Remove-Item build\pandoc.zip, build\pandoc-tmp -Recurse -Force
    }
    Write-Host "pandoc: $pandocDir\pandoc.exe"
}

# 3. Freeze.
if ($WithSklearn) { $env:RETYPESET_WITH_SKLEARN = "1" } else { $env:RETYPESET_WITH_SKLEARN = "0" }
& .\.venv-build\Scripts\pyinstaller.exe packaging\retypeset.spec --noconfirm --clean

$size = (Get-ChildItem dist\retypeset -Recurse | Measure-Object -Property Length -Sum).Sum / 1MB
Write-Host ("dist\retypeset : {0:N0} MB" -f $size) -ForegroundColor Green

# 4. Portable zip -- the version to hand to a lab machine where nobody has
#    administrator rights.
$zip = "dist\retypeset-$version-win64.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path dist\retypeset\* -DestinationPath $zip
Write-Host ("{0} : {1:N0} MB" -f $zip, ((Get-Item $zip).Length / 1MB)) -ForegroundColor Green

# 5. Installer, if Inno Setup is installed.
$iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (Test-Path $iscc) {
    & $iscc /DAppVersion=$version packaging\installer.iss
    Write-Host "installer written to dist\" -ForegroundColor Green
} else {
    Write-Host "Inno Setup 6 not found -- skipping the installer. Install it from https://jrsoftware.org/isdl.php if you want one." -ForegroundColor Yellow
}

Write-Host "`nSmoke test:  dist\retypeset\retypeset.exe" -ForegroundColor Cyan
