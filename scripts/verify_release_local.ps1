param([string]$PythonPath)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$RunId = [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N')
$ReportDir = Join-Path ([IO.Path]::GetTempPath()) ('DevMoneyReleaseChecks\' + $RunId)
New-Item -ItemType Directory -Path $ReportDir -Force | Out-Null
$LogPath = Join-Path $ReportDir 'tests.log'
$ReportPath = Join-Path $ReportDir 'report.json'
$ExitCode = 2
$Report = [ordered]@{
    scope = 'Local offline checks only'
    started_at_utc = [DateTime]::UtcNow.ToString('o')
    project = $ProjectRoot
    version = $null
    git_commit = $null
    python = $null
    backend_tests = 'not_started'
    test_exit_code = $null
    test_summary = $null
    local_build_prerequisites = @()
    native_updater_tests = 'not_executed'
    qt_tests = 'not_executed'
    application_build = 'not_executed'
    package_signature = 'not_attempted'
    release_publication = 'not_attempted'
    remote_acceptance = 'not_verified'
    private_signing_key_read = $false
    verdict = 'UNVERIFIED'
    error = $null
}

Push-Location $ProjectRoot
try {
    foreach ($RequiredFile in @('desktop\CMakeLists.txt', 'scripts\run_tests.py', 'requirements-test.txt')) {
        if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot $RequiredFile) -PathType Leaf)) {
            throw "Arquivo obrigatorio ausente: $RequiredFile"
        }
    }
    $CMakeSource = Get-Content -LiteralPath (Join-Path $ProjectRoot 'desktop\CMakeLists.txt') -Raw
    $VersionMatch = [regex]::Match($CMakeSource, 'set\(QMONEY_VERSION "([0-9]+\.[0-9]+\.[0-9]+)"\)')
    if (-not $VersionMatch.Success) { throw 'Versao nao encontrada no CMakeLists.txt.' }
    $Report.version = $VersionMatch.Groups[1].Value
    $NotesPath = Join-Path $ProjectRoot ('RELEASE_NOTES_' + $Report.version + '.md')
    if (-not (Test-Path -LiteralPath $NotesPath -PathType Leaf)) { throw 'Notas da versao ausentes.' }
    Write-Host ('Versao do codigo: ' + $Report.version)

    if (Get-Command git.exe -ErrorAction SilentlyContinue) {
        $GitCommit = & git.exe -C $ProjectRoot rev-parse HEAD 2>$null
        if ($LASTEXITCODE -eq 0) { $Report.git_commit = $GitCommit }
    }

    if (-not $PythonPath) {
        $PythonCandidates = @(
            (Join-Path $ProjectRoot '.venv\Scripts\python.exe'),
            (Join-Path (Split-Path $ProjectRoot -Parent) 'QMoney\.venv\Scripts\python.exe')
        )
        $PythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($PythonCommand) { $PythonCandidates += $PythonCommand.Source }
        $PythonPath = $PythonCandidates | Where-Object {
            Test-Path -LiteralPath $_ -PathType Leaf
        } | Select-Object -First 1
    }
    if (-not $PythonPath -or -not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        throw 'Python nao encontrado. Configure um ambiente Python 3.12 com requirements-test.txt.'
    }
    $PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
    $Report.python = $PythonPath
    Write-Host ('Python: ' + $PythonPath)
    & $PythonPath -B -c "import importlib.util,sys; names=('pytest','flask','playwright','boto3','ego4d','curl_cffi'); missing=[n for n in names if importlib.util.find_spec(n) is None]; print('Python: '+sys.version.split()[0]); print('Dependencias ausentes: '+(', '.join(missing) or 'nenhuma')); sys.exit(0 if not missing and sys.version_info[:2] == (3,12) else 2)"
    if ($LASTEXITCODE -ne 0) { throw 'O ambiente de testes precisa de Python 3.12 e das dependencias do projeto.' }

    $Prerequisites = foreach ($RelativePath in @(
        '.venv\Scripts\pyinstaller.exe',
        '.qt\6.8.3\mingw_64\bin\windeployqt.exe',
        '.qt\Tools\mingw1310_64\bin\g++.exe',
        'tools\ffmpeg'
    )) {
        [ordered]@{ path = $RelativePath; present = (Test-Path -LiteralPath (Join-Path $ProjectRoot $RelativePath)) }
    }
    $Report.local_build_prerequisites = @($Prerequisites)
    Write-Host 'A disponibilidade das ferramentas locais de build foi registrada no relatorio.'
    Write-Host 'Executando a suite de backend pelo runner isolado e offline do projeto...'
    $Report.backend_tests = 'running'
    $PreviousErrorPreference = $ErrorActionPreference
    try {
        # PS 5.1 treats native stderr as error records. Preserve it in the log
        # and use the actual process exit code to decide whether tests passed.
        $ErrorActionPreference = 'Continue'
        & $PythonPath -B -X utf8 (Join-Path $ProjectRoot 'scripts\run_tests.py') --pytest -q test -p no:cacheprovider --tb=short 2>&1 |
            Tee-Object -FilePath $LogPath
        $TestExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $PreviousErrorPreference
    }
    $Report.test_exit_code = $TestExitCode
    $SummaryLine = Select-String -LiteralPath $LogPath -Pattern '\d+ (failed|passed|skipped).* in |no tests ran' |
        Select-Object -Last 1
    if ($SummaryLine) { $Report.test_summary = $SummaryLine.Line }
    if ($TestExitCode -ne 0) {
        $Report.backend_tests = 'failed'
        $Report.verdict = 'LOCAL_CHECKS_FAILED'
        $ExitCode = 1
        Write-Host 'As verificacoes locais falharam. Consulte tests.log e report.json.'
    } else {
        $Report.backend_tests = 'passed'
        $Report.verdict = 'LOCAL_BACKEND_TESTS_PASSED_ONLY'
        $ExitCode = 0
        Write-Host 'Os testes locais do backend passaram.'
    }
    Write-Host 'Build, Qt, atualizador, assinatura e publicacao nao foram executados por este verificador.'
    Write-Host 'O resultado local nao comprova captura fisica nem aceitacao remota.'
} catch {
    $Report.error = $_.Exception.Message
    $Report.verdict = 'LOCAL_CHECKS_INCOMPLETE'
    Write-Host ('Verificacao interrompida: ' + $Report.error)
} finally {
    $Report.finished_at_utc = [DateTime]::UtcNow.ToString('o')
    $Report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $ReportPath -Encoding UTF8
    Write-Host ('Relatorio: ' + $ReportPath)
    Write-Host ('Log dos testes: ' + $LogPath)
    Pop-Location
}
exit $ExitCode
