param(
    [string]$Version = "1.0.14",
    [string]$QtRoot = "$PSScriptRoot\..\.qt\6.8.3\mingw_64"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path
$OutputDir = Join-Path $ProjectRoot "dist"
$WorkDir = Join-Path $OutputDir "work"
$BuildDir = Join-Path $WorkDir "cmake"
$QtRoot = (Resolve-Path $QtRoot).Path
$MingwBin = (Resolve-Path "$ProjectRoot\.qt\Tools\mingw1310_64\bin").Path
$env:PATH = "$MingwBin;$QtRoot\bin;$env:PATH"
$env:QMONEY_VERSION = $Version

# Todos os artefatos, inclusive os temporários, ficam dentro de dist/.
# Remover o pacote anterior evita misturar DLLs/runtime de versões diferentes.
$Package = Join-Path $OutputDir "QMoney"
$OutputPrefix = $OutputDir.TrimEnd('\') + '\'
foreach ($GeneratedDir in @($WorkDir, $Package)) {
    $FullGeneratedDir = [System.IO.Path]::GetFullPath($GeneratedDir)
    if (-not $FullGeneratedDir.StartsWith(
            $OutputPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Diretório de build fora de dist: $FullGeneratedDir"
    }
    if (Test-Path -LiteralPath $FullGeneratedDir) {
        Remove-Item -LiteralPath $FullGeneratedDir -Recurse -Force
    }
}
New-Item -ItemType Directory -Force $OutputDir | Out-Null
New-Item -ItemType Directory -Force "$WorkDir\spec" | Out-Null

& "$ProjectRoot\.venv\Scripts\pyinstaller.exe" --noconfirm --clean --onefile --windowed `
    --name QMoneyService `
    --paths $ProjectRoot --icon "$ProjectRoot\desktop\resources\qmoney.ico" `
    --distpath "$WorkDir\pyinstaller" --workpath "$WorkDir\pyinstaller-build" `
    --specpath "$WorkDir\spec" `
    --collect-data moneymin --collect-all curl_cffi `
    --collect-submodules playwright --collect-submodules boto3 --collect-submodules ego4d `
    --add-data "$ProjectRoot\moneymin\iphone_uw_calibration.json;moneymin" `
    --add-data "$ProjectRoot\moneymin\native_metadata_reference.json;moneymin" `
    --add-data "$ProjectRoot\moneymin\resources;moneymin/resources" `
    --add-data "$ProjectRoot\reference;reference" `
    "$ProjectRoot\packaging\qmoney_service.py"
$env:QMONEY_EMBEDDED_SERVICE = (Resolve-Path "$WorkDir\pyinstaller\QMoneyService.exe").Path
cmake -S "$ProjectRoot\desktop" -B $BuildDir -G Ninja `
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH=$QtRoot
cmake --build $BuildDir --config Release --parallel

New-Item -ItemType Directory -Force $Package | Out-Null
New-Item -ItemType Directory -Force "$Package\runtime" | Out-Null
Copy-Item "$BuildDir\QMoney.exe" $Package -Force
Copy-Item "$BuildDir\QMoneyUpdater.exe" $Package -Force
& "$QtRoot\bin\windeployqt.exe" --release --no-translations --no-system-d3d-compiler `
    --no-opengl-sw "$Package\QMoney.exe"
Copy-Item "$WorkDir\pyinstaller\QMoneyService.exe" "$Package\runtime" -Force

$Ffmpeg = Get-ChildItem "$ProjectRoot\tools\ffmpeg" -Recurse -Filter ffmpeg.exe | Select-Object -First 1
$Ffprobe = Get-ChildItem "$ProjectRoot\tools\ffmpeg" -Recurse -Filter ffprobe.exe | Select-Object -First 1
if (-not $Ffmpeg -or -not $Ffprobe) { throw "FFmpeg/FFprobe não encontrados." }
$MediaBin = "$Package\runtime\tools\ffmpeg\bin"
New-Item -ItemType Directory -Force $MediaBin | Out-Null
Copy-Item $Ffmpeg.FullName $MediaBin -Force
Copy-Item $Ffprobe.FullName $MediaBin -Force

# FFmpeg/FFprobe usam o Universal C Runtime. Em instalações limpas ou Windows
# sem o UCRT atualizado, o loader encerrava o processo com 0xc0000142 antes
# de o motor conseguir registrar o erro. O app-local deployment é suportado
# pela Microsoft e mantém o QMoney portátil, sem instalador adicional.
$WindowsKitsRedist = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\Redist"
$UcrtBase = Get-ChildItem $WindowsKitsRedist -Recurse -File -Filter ucrtbase.dll `
    -ErrorAction SilentlyContinue |
    Where-Object FullName -Match "\\ucrt\\DLLs\\x64\\ucrtbase\.dll$" |
    Sort-Object FullName -Descending | Select-Object -First 1
if (-not $UcrtBase) {
    throw "UCRT x64 de redistribuição não encontrado no Windows SDK."
}
Copy-Item (Join-Path $UcrtBase.Directory.FullName "*.dll") $MediaBin -Force

# O navegador é privado ao runtime do QMoney; o usuário não precisa instalar
# Playwright ou Chrome. Copiamos apenas a revisão atual esperada pelo pacote.
& "$ProjectRoot\.venv\Scripts\playwright.exe" install chromium
$BrowserSource = Join-Path $env:LOCALAPPDATA "ms-playwright"
$BrowserTarget = "$Package\runtime\ms-playwright"
New-Item -ItemType Directory -Force $BrowserTarget | Out-Null
$Chromium = Get-ChildItem $BrowserSource -Directory -Filter "chromium-*" |
    Where-Object Name -NotLike "chromium_headless_shell-*" |
    Sort-Object Name -Descending | Select-Object -First 1
$Headless = Get-ChildItem $BrowserSource -Directory -Filter "chromium_headless_shell-*" |
    Sort-Object Name -Descending | Select-Object -First 1
foreach ($BrowserPart in @($Chromium, $Headless)) {
    if ($BrowserPart) { Copy-Item $BrowserPart.FullName $BrowserTarget -Recurse -Force }
}
Get-ChildItem $BrowserSource -Directory | Where-Object Name -Match "^(ffmpeg|winldd)-" |
    Sort-Object Name -Descending | Group-Object { $_.Name.Split('-')[0] } |
    ForEach-Object { Copy-Item $_.Group[0].FullName $BrowserTarget -Recurse -Force }

$Zip = Join-Path $OutputDir "QMoney-windows-x64.zip"
Remove-Item $Zip -Force -ErrorAction SilentlyContinue
Compress-Archive -Path "$Package\*" -DestinationPath $Zip -CompressionLevel Optimal
$Hash = (Get-FileHash $Zip -Algorithm SHA256).Hash.ToLowerInvariant()
"$Hash  QMoney-windows-x64.zip" | Set-Content "$Zip.sha256" -Encoding ascii
$SigningKey = if ($env:QMONEY_SIGNING_KEY) {
    $env:QMONEY_SIGNING_KEY
} else {
    "$ProjectRoot\secrets\qmoney_update_private.pem"
}
if (-not (Test-Path -LiteralPath $SigningKey)) {
    throw "Chave RSA de atualização ausente. Configure QMONEY_SIGNING_KEY."
}
$Rsa = [System.Security.Cryptography.RSA]::Create()
try {
    $Rsa.ImportFromPem([System.IO.File]::ReadAllText($SigningKey))
    $Signature = $Rsa.SignHash(
        [Convert]::FromHexString($Hash),
        [System.Security.Cryptography.HashAlgorithmName]::SHA256,
        [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)
    [System.IO.File]::WriteAllText(
        "$Zip.sig", [Convert]::ToBase64String($Signature),
        [System.Text.Encoding]::ASCII)
} finally {
    $Rsa.Dispose()
}
if (Test-Path -LiteralPath $WorkDir) {
    Remove-Item -LiteralPath $WorkDir -Recurse -Force
}
Write-Host "Pacote pronto: $Zip"
