#Requires -Version 5.1
<#
.SYNOPSIS
    Install the c-lib-configurator skill for DSH and/or Claude Code.

.DESCRIPTION
    Copies this repository's payload into the skills directory of each agent host:

        dsh     %USERPROFILE%\.dsh\skills\c-lib-configurator\      (SKILL.md is the entry point)
        claude  %USERPROFILE%\.claude\skills\c-lib-configurator\   (same payload)

    Re-running is safe: the payload is refreshed in place, and if the repository
    already *is* the skill directory nothing is copied.

    Set DSH_SKILLS_DIR to override the DSH location.

.EXAMPLE
    .\scripts\install-skill.ps1 -Target dsh

.EXAMPLE
    .\scripts\install-skill.ps1 -Target all -DryRun
#>
[CmdletBinding()]
param(
    [ValidateSet('dsh', 'claude', 'all')]
    [string]$Target = 'dsh',
    [string]$SkillsDir,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $SkillsDir) {
    $SkillsDir = if ($env:DSH_SKILLS_DIR) { $env:DSH_SKILLS_DIR } else { Join-Path $env:USERPROFILE '.dsh\skills' }
}
$payload = @('SKILL.md', 'README.md', 'scripts', 'assets', 'references', 'examples')

function Say([string]$msg, [string]$color = 'Gray') { Write-Host $msg -ForegroundColor $color }

function Copy-Payload([string]$dest) {
    $resolved = Resolve-Path -LiteralPath $dest -ErrorAction SilentlyContinue
    if ($resolved -and $resolved.Path -eq (Resolve-Path -LiteralPath $repo).Path) {
        Say '  already in place (the repo *is* the skill directory), nothing to do' 'Green'
        return
    }
    if ($DryRun) {
        Say "  [dry-run] copy $repo -> $dest" 'Yellow'
        return
    }
    New-Item -ItemType Directory -Path $dest -Force | Out-Null
    foreach ($item in $payload) {
        $src = Join-Path $repo $item
        if (-not (Test-Path -LiteralPath $src)) { continue }
        $dstItem = Join-Path $dest $item
        # Replace, don't merge: Copy-Item -Recurse into an existing folder nests
        # it on some PowerShell versions (scripts\scripts).
        if (Test-Path -LiteralPath $dstItem) { Remove-Item -LiteralPath $dstItem -Recurse -Force }
        Copy-Item -LiteralPath $src -Destination $dstItem -Recurse -Force
    }
    # Never ship caches into a skill directory.
    Get-ChildItem -LiteralPath $dest -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Say "  ok: $dest" 'Green'
}

function Install-Dsh {
    $dest = Join-Path $SkillsDir 'c-lib-configurator'
    Say "DSH -> $dest" 'Cyan'
    Copy-Payload $dest
    if (-not $DryRun) { Say '  restart the session so the catalog picks it up' 'DarkGray' }
}

function Install-Claude {
    $dest = Join-Path $env:USERPROFILE '.claude\skills\c-lib-configurator'
    Say "Claude Code -> $dest" 'Cyan'
    Copy-Payload $dest
    if (-not $DryRun) { Say '  run /skills (or restart) for Claude Code to load it' 'DarkGray' }
}

if (-not (Test-Path -LiteralPath (Join-Path $repo 'SKILL.md'))) {
    throw "cannot find SKILL.md relative to $repo"
}

Say 'c-lib-configurator installer' 'White'
Say "  payload : $repo"
Say "  targets : $Target"
if ($DryRun) { Say '  mode    : DRY RUN' 'Yellow' }

if ($Target -in @('dsh', 'all')) { Install-Dsh }
if ($Target -in @('claude', 'all')) { Install-Claude }

Say 'done.' 'Green'
