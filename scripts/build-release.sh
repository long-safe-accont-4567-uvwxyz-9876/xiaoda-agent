#!/bin/bash
# =============================================================================
#  Nahida Agent — Build Release Script
#  Builds a distributable package for the current platform.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---- Helpers -----------------------------------------------------------------
bold()  { printf '\033[1m%s\033[0m' "$*"; }
green() { printf '\033[32m%s\033[0m' "$*"; }
red()   { printf '\033[31m%s\033[0m' "$*"; }

die() {
    red "[ERROR] $*"
    echo "" >&2
    exit 1
}

info() {
    green "[INFO] $*"
}

# ---- Read version from pyproject.toml ----------------------------------------
read_version() {
    local version
    version=$(grep -E '^version\s*=' "$PROJECT_ROOT/pyproject.toml" | head -1 | sed -E 's/^version\s*=\s*["'\''"']([^"'\''"']+)["'\''"']/\1/')
    if [ -z "$version" ]; then
        die "Could not read version from pyproject.toml"
    fi
    echo "$version"
}

# ---- Detect platform and architecture -----------------------------------------
detect_platform() {
    local os arch
    case "$(uname -s)" in
        Linux*)  os="linux" ;;
        MINGW*|MSYS*|CYGWIN*|Windows_NT) os="windows" ;;
        Darwin*) os="macos" ;;
        *)       die "Unsupported OS: $(uname -s)" ;;
    esac

    case "$(uname -m)" in
        x86_64|amd64)  arch="x86_64" ;;
        aarch64|arm64) arch="aarch64" ;;
        armv7l)         arch="armv7l" ;;
        *)              die "Unsupported architecture: $(uname -m)" ;;
    esac

    echo "${os}-${arch}"
}

# ---- Create Windows ZIP (fallback when NSIS unavailable) ----------------------
_create_windows_zip() {
    local dist_dir="$1" output_dir="$2" zip_name="$3"
    if command -v zip &>/dev/null; then
        cd "$PROJECT_ROOT/dist"
        zip -r "$output_dir/$zip_name" nahida-agent/
    elif command -v 7z &>/dev/null; then
        cd "$PROJECT_ROOT/dist"
        7z a "$output_dir/$zip_name" nahida-agent/
    else
        die "Neither 'zip' nor '7z' found. Please install one to create Windows packages."
    fi
    green "  ZIP package created: $output_dir/$zip_name"
}

# ---- Build with PyInstaller ---------------------------------------------------
do_build() {
    local version platform os arch
    version=$(read_version)
    platform=$(detect_platform)
    os="${platform%%-*}"
    arch="${platform##*-}"

    echo ""
    echo "  $(bold "Nahida Agent — Build Release")"
    echo ""
    info "Version:   $version"
    info "Platform:  $platform"
    info "Project:   $PROJECT_ROOT"
    echo ""

    # --- Check spec file exists ------------------------------------------------
    local spec_file="$PROJECT_ROOT/nahida-agent.spec"
    if [ ! -f "$spec_file" ]; then
        die "Spec file not found: $spec_file"
    fi

    # --- Run PyInstaller -------------------------------------------------------
    info "Running PyInstaller..."
    cd "$PROJECT_ROOT"
    pyinstaller nahida-agent.spec --clean --noconfirm
    if [ $? -ne 0 ]; then
        die "PyInstaller build failed."
    fi
    green "  PyInstaller build completed."

    # --- Verify the output directory -------------------------------------------
    local dist_dir="$PROJECT_ROOT/dist/nahida-agent"
    if [ ! -d "$dist_dir" ]; then
        die "Expected output directory not found: $dist_dir"
    fi

    # --- Create the distribution package ---------------------------------------
    local output_dir="$PROJECT_ROOT/dist/release"
    mkdir -p "$output_dir"

    local pkg_name="nahida-agent-${os}-${arch}-v${version}"

    cd "$PROJECT_ROOT/dist"

    if [ "$os" = "linux" ]; then
        # --- Linux: create .run self-extracting installer ----------------------
        local tar_name="${pkg_name}.tar.gz"
        local run_name="${pkg_name}.run"

        info "Creating tar.gz archive..."
        tar czf "$tar_name" nahida-agent

        info "Creating self-extracting installer..."
        cat "$SCRIPT_DIR/install-linux.sh" "$tar_name" > "$output_dir/$run_name"
        chmod +x "$output_dir/$run_name"

        # Clean up intermediate tar.gz
        rm -f "$tar_name"

        green "  Package created: $output_dir/$run_name"
        echo ""
        info "To install, run:"
        echo "    chmod +x $output_dir/$run_name && $output_dir/$run_name"

    elif [ "$os" = "windows" ]; then
        # --- Windows: create .exe installer with NSIS (fallback to ZIP) -------
        local exe_name="${pkg_name}-setup.exe"
        local zip_name="${pkg_name}.zip"

        info "Copying Windows launcher bat into dist directory..."
        cp "$SCRIPT_DIR/start-windows.bat" "$dist_dir/start-windows.bat"
        cp "$SCRIPT_DIR/auto-update.bat" "$dist_dir/auto-update.bat"

        # Try NSIS first for .exe installer
        if command -v makensis &>/dev/null; then
            info "Creating NSIS installer (.exe)..."

            local nsis_file="$PROJECT_ROOT/installer.nsi"
            cat > "$nsis_file" <<NSIS_EOF
!define PRODUCT_NAME "Nahida Agent"
!define PRODUCT_VERSION "$version"
!define PRODUCT_PUBLISHER "Nahida Agent Team"

Name "\${PRODUCT_NAME} \${PRODUCT_VERSION}"
OutFile "$output_dir/$exe_name"
InstallDir "\$PROGRAMFILES64\\\${PRODUCT_NAME}"
InstallDirRegKey HKLM "Software\\\${PRODUCT_NAME}" "InstallDir"
RequestExecutionLevel admin
SetCompressor /SOLID lzma

!include "MUI2.nsh"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_WELCOME
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH

!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

Section "MainSection" SEC01
  SetOutPath "\$INSTDIR"
  SetOverwrite on
  File /r "$dist_dir\\*.*"

  CreateShortCut "\$DESKTOP\\纳西妲.lnk" "\$INSTDIR\\start-windows.bat" "" "\$INSTDIR\\nahida-agent.exe" 0
  CreateDirectory "\$SMPROGRAMS\\\${PRODUCT_NAME}"
  CreateShortCut "\$SMPROGRAMS\\\${PRODUCT_NAME}\\纳西妲.lnk" "\$INSTDIR\\start-windows.bat" "" "\$INSTDIR\\nahida-agent.exe" 0
  CreateShortCut "\$SMPROGRAMS\\\${PRODUCT_NAME}\\卸载.lnk" "\$INSTDIR\\uninstall.exe"
SectionEnd

Section -Post
  WriteUninstaller "\$INSTDIR\\uninstall.exe"
  WriteRegStr HKLM "Software\\\${PRODUCT_NAME}" "InstallDir" "\$INSTDIR"
  WriteRegStr HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\\${PRODUCT_NAME}" "DisplayName" "\${PRODUCT_NAME}"
  WriteRegStr HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\\${PRODUCT_NAME}" "UninstallString" "\$INSTDIR\\uninstall.exe"
  WriteRegStr HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\\${PRODUCT_NAME}" "DisplayVersion" "\${PRODUCT_VERSION}"
  WriteRegStr HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\\${PRODUCT_NAME}" "Publisher" "\${PRODUCT_PUBLISHER}"
SectionEnd

Section Uninstall
  RMDir /r "\$INSTDIR"
  Delete "\$DESKTOP\\纳西妲.lnk"
  RMDir /r "\$SMPROGRAMS\\\${PRODUCT_NAME}"
  DeleteRegKey HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\\${PRODUCT_NAME}"
  DeleteRegKey HKLM "Software\\\${PRODUCT_NAME}"
SectionEnd
NSIS_EOF

            if makensis "$nsis_file"; then
                green "  NSIS installer created: $output_dir/$exe_name"
                rm -f "$nsis_file"
            else
                red "  NSIS build failed, falling back to ZIP..."
                rm -f "$nsis_file"
                _create_windows_zip "$dist_dir" "$output_dir" "$zip_name"
            fi
        else
            info "NSIS not found, creating ZIP archive instead..."
            info "(Install NSIS from https://nsis.sourceforge.io to build .exe installers)"
            _create_windows_zip "$dist_dir" "$output_dir" "$zip_name"
        fi

    else
        # --- macOS or other: create .tar.gz ------------------------------------
        local tar_name="${pkg_name}.tar.gz"

        info "Creating tar.gz archive..."
        tar czf "$output_dir/$tar_name" nahida-agent

        green "  Package created: $output_dir/$tar_name"
    fi

    echo ""
    echo "  $(bold "Build complete!")"
    echo ""
}

# ---- Main ---------------------------------------------------------------------
do_build