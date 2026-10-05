# Windows entry point. Docker Desktop must have WSL integration enabled.
$ErrorActionPreference = 'Stop'
if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) {
    throw 'Install WSL and Docker Desktop with WSL integration, then rerun .\setup.ps1.'
}
$bridgeRoot = & wsl wslpath -a $PSScriptRoot
if ($LASTEXITCODE -ne 0) { throw 'Start a configured WSL distribution, then rerun setup.' }
$bridgeArgs = @()
for ($i = 0; $i -lt $args.Count; $i++) {
    $bridgeArgs += $args[$i]
    if ($args[$i] -in @('--repo', '--project', '--import-sqlite')) {
        $i++
        if ($i -ge $args.Count) { throw 'Missing path argument.' }
        $bridgePath = (Resolve-Path $args[$i]).Path
        $bridgeConverted = & wsl wslpath -a $bridgePath
        if ($LASTEXITCODE -ne 0) { throw 'Could not translate the path for WSL.' }
        $bridgeArgs += $bridgeConverted
    }
}
& wsl bash "$bridgeRoot/setup" @bridgeArgs
exit $LASTEXITCODE
