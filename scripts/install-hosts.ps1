#requires -Version 5.1
<#
.SYNOPSIS
Register the Video Story skills (video-story, video-story-edit) with Claude Code, Codex and/or
Cursor on Windows (directory junctions, no administrator rights needed).
.DESCRIPTION
Where each host looks for user skills (docs/installation.md, section 3, cites the pages):
  -Claude   %USERPROFILE%\.claude\skills
  -Codex    %USERPROFILE%\.agents\skills  (a link left in the legacy .codex\skills is removed)
  -Cursor   %USERPROFILE%\.cursor\skills, but only when Cursor does not already load the skill
            from one of the folders it also reads: .agents\skills, .claude\skills, .codex\skills
Both skills are always handled together. Running it again changes nothing that is already
right. -Remove deletes the links of the named hosts (only links, never a real folder).
.EXAMPLE
& .\scripts\install-hosts.ps1 -Claude
& .\scripts\install-hosts.ps1 -Claude -Codex -Cursor
& .\scripts\install-hosts.ps1 -Cursor -Remove
#>
[CmdletBinding()]
param(
    [switch]$Claude,
    [switch]$Codex,
    [switch]$Cursor,
    [switch]$Remove
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$skillNames = @('video-story', 'video-story-edit')
foreach ($name in $skillNames) {
    if (-not (Test-Path -LiteralPath (Join-Path $root "skills\$name\SKILL.md"))) { throw "Skill directory not found: skills\$name" }
}
if (-not ($Claude -or $Codex -or $Cursor)) { throw 'Pass -Claude, -Codex and/or -Cursor (and -Remove to unregister).' }

$userHome = $env:USERPROFILE
$claudeDir = Join-Path $userHome '.claude\skills'
$agentsDir = Join-Path $userHome '.agents\skills'
$legacyCodexDir = Join-Path $userHome '.codex\skills'
$cursorDir = Join-Path $userHome '.cursor\skills'
# The other folders Cursor reads skills from (cursor.com/docs/skills, "Skill directories").
$cursorAlsoReads = @($agentsDir, $claudeDir, $legacyCodexDir)

function Get-Normalized([string]$Path) {
    return [IO.Path]::GetFullPath($Path).TrimEnd('\')
}

function Test-IsLink([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $item = Get-Item -LiteralPath $Path -Force
    return [bool]($item.Attributes -band [IO.FileAttributes]::ReparsePoint)
}

function Get-LinkTarget([string]$Path) {
    $target = @((Get-Item -LiteralPath $Path -Force).Target)[0]
    if (-not $target) { return $null }
    if (-not [IO.Path]::IsPathRooted($target)) { $target = Join-Path (Split-Path -Parent $Path) $target }
    return Get-Normalized $target
}

# True when $Path is a junction or symbolic link to this checkout's skill $Name.
function Test-OwnLink([string]$Path, [string]$Name) {
    if (-not (Test-IsLink $Path)) { return $false }
    return (Get-LinkTarget $Path) -eq (Get-Normalized (Join-Path $root "skills\$Name"))
}

function Remove-SkillLink([string]$HostName, [string]$Link, [string]$Why) {
    if (-not (Test-Path -LiteralPath $Link)) { return $false }
    if (-not (Test-IsLink $Link)) {
        Write-Host "$HostName`: left $Link alone (a real folder, not a link)"
        return $false
    }
    # rmdir removes the junction itself; its target stays untouched.
    cmd /c rmdir "$Link" | Out-Null
    if (Test-Path -LiteralPath $Link) { throw "Could not remove the link $Link" }
    Write-Host "$HostName`: removed $Link$Why"
    return $true
}

function New-SkillLink([string]$HostName, [string]$SkillsDir, [string]$Name) {
    $skill = Join-Path $root "skills\$Name"
    $link = Join-Path $SkillsDir $Name
    if (Test-OwnLink $link $Name) {
        Write-Host "$HostName`: unchanged $link -> $skill"
        return
    }
    $replaced = ''
    if (Test-Path -LiteralPath $link) {
        if (-not (Test-IsLink $link)) { throw "$link exists and is not a link; remove it first." }
        $replaced = " (replaced a link to $(Get-LinkTarget $link))"
        cmd /c rmdir "$link" | Out-Null
    }
    [IO.Directory]::CreateDirectory($SkillsDir) | Out-Null
    cmd /c mklink /J "$link" "$skill" | Out-Null
    if (-not (Test-Path -LiteralPath (Join-Path $link 'SKILL.md'))) { throw "Junction failed: $link" }
    Write-Host "$HostName`: created $link -> $skill$replaced"
}

function Get-CursorSources([string]$Name) {
    return @($cursorAlsoReads | ForEach-Object { Join-Path $_ $Name } | Where-Object { Test-OwnLink $_ $Name })
}

function Write-CursorDuplicateNote([string]$Name) {
    $own = Join-Path $cursorDir $Name
    if ($Cursor) { return }  # the Cursor step of this run sorts it out
    if ((Test-OwnLink $own $Name) -and @(Get-CursorSources $Name).Count -gt 0) {
        Write-Host "  note: Cursor now finds $Name twice (also $own); run with -Cursor to drop that link."
    }
}

function Write-CursorLostNote([string]$Name) {
    if ($Cursor -or -not (Test-Path -LiteralPath $cursorDir)) { return }
    if (@(Get-CursorSources $Name).Count -eq 0 -and -not (Test-OwnLink (Join-Path $cursorDir $Name) $Name)) {
        Write-Host "  note: Cursor no longer finds $Name either; run with -Cursor if you use it there."
    }
}

foreach ($name in $skillNames) {
    if ($Claude) {
        $link = Join-Path $claudeDir $name
        if ($Remove) {
            if (Remove-SkillLink 'Claude Code' $link '') { Write-CursorLostNote $name }
            elseif (-not (Test-Path -LiteralPath $link)) { Write-Host "Claude Code: nothing to remove at $link" }
        } else {
            New-SkillLink 'Claude Code' $claudeDir $name
            Write-CursorDuplicateNote $name
        }
    }
    if ($Codex) {
        $link = Join-Path $agentsDir $name
        $legacy = Join-Path $legacyCodexDir $name
        if ($Remove) {
            $removed = Remove-SkillLink 'Codex' $link ''
            if (-not $removed -and -not (Test-Path -LiteralPath $link)) { Write-Host "Codex: nothing to remove at $link" }
            if (Remove-SkillLink 'Codex' $legacy ' (legacy location)') { $removed = $true }
            if ($removed) { Write-CursorLostNote $name }
        } else {
            New-SkillLink 'Codex' $agentsDir $name
            # Codex lists two skills of the same name side by side; its current documentation
            # names only ~/.agents/skills, so an older link in ~/.codex/skills goes.
            Remove-SkillLink 'Codex' $legacy ' (legacy location; Codex now reads ~/.agents/skills)' | Out-Null
            Write-CursorDuplicateNote $name
        }
    }
    if ($Cursor) {
        $link = Join-Path $cursorDir $name
        $sources = @(Get-CursorSources $name)
        if ($Remove) {
            if (-not (Remove-SkillLink 'Cursor' $link '') -and -not (Test-Path -LiteralPath $link)) {
                Write-Host "Cursor: nothing to remove at $link"
            }
            foreach ($s in $sources) { Write-Host "  note: Cursor still loads $name from $s (registered for another host)" }
        } elseif ($sources.Count -gt 0) {
            $why = "; Cursor already loads the skill from $($sources[0])"
            if (-not (Remove-SkillLink 'Cursor' $link $why)) {
                Write-Host "Cursor: no link needed in $cursorDir$why"
            }
        } else {
            New-SkillLink 'Cursor' $cursorDir $name
        }
    }
}

if (-not $Remove) {
    $mcpArgs = @('/d', '/c', 'call', (Join-Path $root 'skills/video-story/scripts/video-story.bat'), 'mcp')
    Write-Host ''
    Write-Host 'MCP server (optional; only for a host that cannot open the window from a command, see docs/installation.md):'
    if ($Claude) {
        Write-Host ''
        Write-Host 'Claude Desktop - add to claude_desktop_config.json:'
        $cfg = @{ mcpServers = @{ 'video-story' = @{ command = 'cmd.exe'; args = $mcpArgs } } }
        Write-Host ($cfg | ConvertTo-Json -Depth 5)
    }
    if ($Codex) {
        Write-Host ''
        Write-Host 'Codex - add to ~/.codex/config.toml (or run: codex mcp add video-story -- cmd.exe /d /c call <launcher.bat> mcp):'
        $tomlArgs = ($mcpArgs | ForEach-Object { '"' + ($_ -replace '\\', '\\') + '"' }) -join ', '
        Write-Host '[mcp_servers.video-story]'
        Write-Host 'command = "cmd.exe"'
        Write-Host "args = [$tomlArgs]"
        Write-Host 'tool_timeout_sec = 60'
    }
    if ($Cursor) {
        Write-Host ''
        Write-Host 'Cursor - add to ~/.cursor/mcp.json (or .cursor/mcp.json in a project):'
        $cfg = @{ mcpServers = @{ 'video-story' = [ordered]@{ type = 'stdio'; command = 'cmd.exe'; args = $mcpArgs } } }
        Write-Host ($cfg | ConvertTo-Json -Depth 5)
    }
    Write-Host ''
    Write-Host 'Start a new chat in the host and say: Start Video Story (or Edit Video Story)'
}
