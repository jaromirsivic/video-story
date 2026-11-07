#requires -Version 5.1
<#
.SYNOPSIS
Register the Video Story skill with Claude Code and/or Codex on Windows (junctions).
.EXAMPLE
& .\scripts\install-hosts.ps1 -Claude
& .\scripts\install-hosts.ps1 -Claude -Codex
& .\scripts\install-hosts.ps1 -Claude -Remove
#>
[CmdletBinding()]
param(
    [switch]$Claude,
    [switch]$Codex,
    [switch]$Remove
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$skill = Join-Path $root 'skills\video-story'
if (-not (Test-Path -LiteralPath (Join-Path $skill 'SKILL.md'))) { throw "Skill directory not found: $skill" }
if (-not ($Claude -or $Codex)) { throw 'Pass -Claude and/or -Codex.' }

function Set-SkillLink([string]$HostName, [string]$SkillsDir) {
    $link = Join-Path $SkillsDir 'video-story'
    if ($Remove) {
        if (Test-Path -LiteralPath $link) {
            # A junction is removed with rmdir; its target stays untouched.
            cmd /c rmdir "$link" | Out-Null
            Write-Host "$HostName`: removed $link"
        } else {
            Write-Host "$HostName`: nothing to remove at $link"
        }
        return
    }
    [IO.Directory]::CreateDirectory($SkillsDir) | Out-Null
    if (Test-Path -LiteralPath $link) {
        $item = Get-Item -LiteralPath $link -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            cmd /c rmdir "$link" | Out-Null
        } else {
            throw "$link exists and is not a link; remove it first."
        }
    }
    cmd /c mklink /J "$link" "$skill" | Out-Null
    if (-not (Test-Path -LiteralPath (Join-Path $link 'SKILL.md'))) { throw "Junction failed: $link" }
    Write-Host "$HostName`: $link -> $skill"
}

if ($Claude) { Set-SkillLink 'Claude Code' (Join-Path $env:USERPROFILE '.claude\skills') }
if ($Codex) { Set-SkillLink 'Codex' (Join-Path $env:USERPROFILE '.codex\skills') }

if (-not $Remove) {
    Write-Host ''
    Write-Host 'Claude Desktop (optional) - add to claude_desktop_config.json:'
    $cfg = @{ mcpServers = @{ 'video-story' = @{ command = 'uv'; args = @('run', '--project', $root, 'video-story', 'mcp') } } }
    Write-Host ($cfg | ConvertTo-Json -Depth 5)
    Write-Host ''
    Write-Host 'Start a new chat in the host and say: Start Video Story'
}
