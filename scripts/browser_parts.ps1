function Get-QMoneyBrowserParts {
    param([string]$ManifestPath, [string]$BrowserSource)

    $Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    foreach ($Name in @('chromium', 'chromium-headless-shell', 'ffmpeg', 'winldd')) {
        $Entries = @($Manifest.browsers | Where-Object name -EQ $Name)
        if ($Entries.Count -ne 1) { throw "Manifesto Playwright inválido: $Name" }
        $Entry = $Entries[0]
        $Revision = $Entry.revision
        if ($Entry.revisionOverrides -and $Entry.revisionOverrides.win64) {
            $Revision = $Entry.revisionOverrides.win64
        }
        if ("$Revision" -notmatch '^\d+$') { throw "Revisão Playwright inválida: $Name" }
        $Directory = Join-Path $BrowserSource ($Name.Replace('-', '_') + '-' + $Revision)
        if (-not (Test-Path -LiteralPath $Directory -PathType Container)) {
            throw "Componente Playwright esperado não encontrado: $Directory"
        }
        Get-Item -LiteralPath $Directory
    }
}
