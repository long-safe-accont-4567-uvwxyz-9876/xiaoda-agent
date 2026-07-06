<#
.SYNOPSIS
    Installs or uninstalls xiaoda-agent on Windows.

.DESCRIPTION
    Creates a Start Menu shortcut, adds the install directory to the
    user-level PATH, and optionally removes them with -Uninstall.

.PARAMETER InstallPath
    Directory where xiaoda-agent is installed.
    Default: $env:LOCALAPPDATA\xiaoda-agent

.PARAMETER Uninstall
    Switch to remove shortcuts and PATH entry instead of installing.

.EXAMPLE
    .\install-windows.ps1
    Installs xiaoda-agent with default settings.

.EXAMPLE
    .\install-windows.ps1 -InstallPath "C:\Tools\xiaoda-agent"
    Installs xiaoda-agent from a custom directory.

.EXAMPLE
    .\install-windows.ps1 -Uninstall
    Removes shortcuts and PATH entry.
#>

param(
    [string]$InstallPath = "$env:LOCALAPPDATA\xiaoda-agent",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

# ============================================
#  Helper functions
# ============================================

function Add-UserPath {
    param([string]$Dir)

    $currentPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($currentPath -split ";" | Where-Object { $_ -eq $Dir }) {
        Write-Host "  [SKIP] PATH already contains: $Dir" -ForegroundColor Yellow
        return
    }

    $newPath = if ($currentPath) { "$currentPath;$Dir" } else { $Dir }
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Host "  [OK]   Added to user PATH: $Dir" -ForegroundColor Green
}

function Remove-UserPath {
    param([string]$Dir)

    $currentPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not $currentPath) { return }

    $entries = $currentPath -split ";" | Where-Object { $_ -ne $Dir -and $_ -ne "" }
    $newPath = $entries -join ";"
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Host "  [OK]   Removed from user PATH: $Dir" -ForegroundColor Green
}

function Create-Shortcut {
    param(
        [string]$TargetExe,
        [string]$ShortcutPath,
        [string]$Description
    )

    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($ShortcutPath)
    $sc.TargetPath = $TargetExe
    $sc.WorkingDirectory = Split-Path $TargetExe
    $sc.Description = $Description
    $sc.Save()
    Write-Host "  [OK]   Created shortcut: $ShortcutPath" -ForegroundColor Green
}

# ============================================
#  Uninstall
# ============================================

if ($Uninstall) {
    Write-Host ""
    Write-Host "  ================================" -ForegroundColor Cyan
    Write-Host "    Xiaoda Agent - Uninstall" -ForegroundColor Cyan
    Write-Host "  ================================" -ForegroundColor Cyan
    Write-Host ""

    # Remove Start Menu shortcut
    $startMenuDir = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs"
    $shortcutFile = "$startMenuDir\xiaoda-agent.lnk"
    if (Test-Path $shortcutFile) {
        Remove-Item $shortcutFile -Force
        Write-Host "  [OK]   Removed shortcut: $shortcutFile" -ForegroundColor Green
    } else {
        Write-Host "  [SKIP] Shortcut not found: $shortcutFile" -ForegroundColor Yellow
    }

    # Remove from PATH
    Remove-UserPath -Dir $InstallPath

    Write-Host ""
    Write-Host "  Uninstall complete." -ForegroundColor Cyan
    Write-Host ""
    return
}

# ============================================
#  Install
# ============================================

Write-Host ""
Write-Host "  ================================" -ForegroundColor Cyan
Write-Host "    Xiaoda Agent - Installer" -ForegroundColor Cyan
Write-Host "  ================================" -ForegroundColor Cyan
Write-Host ""

# Verify executable exists
$exePath = Join-Path $InstallPath "xiaoda-agent.exe"
if (-not (Test-Path $exePath)) {
    Write-Host "  [WARN] Executable not found at: $exePath" -ForegroundColor Yellow
    Write-Host "         Installation will continue, but you may need to build first." -ForegroundColor Yellow
    Write-Host ""
}

# Create Start Menu shortcut
$startMenuDir = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs"
$shortcutPath = "$startMenuDir\xiaoda-agent.lnk"

if (Test-Path $exePath) {
    Create-Shortcut -TargetExe $exePath -ShortcutPath $shortcutPath -Description "Xiaoda Agent"
} else {
    # Create shortcut pointing to install dir if exe doesn't exist yet
    Create-Shortcut -TargetExe $InstallPath -ShortcutPath $shortcutPath -Description "Xiaoda Agent"
}

# Add to PATH
Add-UserPath -Dir $InstallPath

# Create user data directory structure (for voice refs, stickers, etc.)
$userDataDirs = @(
    "$env:USERPROFILE\.ai-agent\data\voice_refs",
    "$env:USERPROFILE\.ai-agent\data\stickers",
    "$env:USERPROFILE\.ai-agent\data\xiaoli-stickers",
    "$env:USERPROFILE\.ai-agent\data\agent-stickers",
    "$env:USERPROFILE\.ai-agent\data\media",
    "$env:USERPROFILE\.ai-agent\data\files"
)
foreach ($dir in $userDataDirs) {
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        Write-Host "  [OK]   Created directory: $dir" -ForegroundColor Green
    }
}

# Print summary
Write-Host ""
Write-Host "  ================================" -ForegroundColor Cyan
Write-Host "    Installation Summary" -ForegroundColor Cyan
Write-Host "  ================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Install path : $InstallPath"
Write-Host "  Executable   : $exePath"
Write-Host "  Shortcut     : $shortcutPath"
Write-Host "  PATH updated : Yes (user-level)"
Write-Host ""
Write-Host "  To start xiaoda-agent, open a NEW terminal and run:"
Write-Host "    xiaoda-agent.exe          (CLI mode)"
Write-Host "    xiaoda-agent.exe --web    (Web UI mode)"
Write-Host ""
Write-Host "  Or use the Start Menu shortcut."
Write-Host ""
Write-Host "  To uninstall, run:"
Write-Host "    .\install-windows.ps1 -Uninstall"
Write-Host ""