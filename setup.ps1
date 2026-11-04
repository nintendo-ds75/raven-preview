# Windows entry point. Docker Desktop must have WSL integration enabled.
$ErrorActionPreference = 'Stop'
if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) {
    throw 'Install WSL and Docker Desktop with WSL integration, then rerun .\setup.ps1.'
}
$ravenRoot = & wsl wslpath -a $PSScriptRoot
if ($LASTEXITCODE -ne 0) { throw 'Start a configured WSL distribution, then rerun setup.' }
$ravenArgs = @()
for ($i = 0; $i -lt $args.Count; $i++) {
    $ravenArgs += $args[$i]
    if ($args[$i] -in @('--repo', '--project', '--import-sqlite')) {
        $i++
        if ($i -ge $args.Count) { throw 'Missing path argument.' }
        $ravenPath = (Resolve-Path $args[$i]).Path
        $ravenConverted = & wsl wslpath -a $ravenPath
        if ($LASTEXITCODE -ne 0) { throw 'Could not translate the path for WSL.' }
        $ravenArgs += $ravenConverted
    }
}
& wsl bash "$ravenRoot/setup" @ravenArgs
exit $LASTEXITCODE
