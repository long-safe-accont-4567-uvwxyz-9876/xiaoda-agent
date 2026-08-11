#!/bin/bash
# =============================================================================
#  Xiaoda Agent — Build Release Script
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
    version=$(python3 -c "
import re, sys
with open('$PROJECT_ROOT/pyproject.toml') as f:
    for line in f:
        m = re.match(r'^version\s*=\s*\"(.+?)\"', line)
        if m:
            print(m.group(1))
            sys.exit(0)
print('dev')
")
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
        zip -r "$output_dir/$zip_name" xiaoda-agent/
    elif command -v 7z &>/dev/null; then
        cd "$PROJECT_ROOT/dist"
        7z a "$output_dir/$zip_name" xiaoda-agent/
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
    echo "  $(bold "Xiaoda Agent — Build Release")"
    echo ""
    info "Version:   $version"
    info "Platform:  $platform"
    info "Project:   $PROJECT_ROOT"
    echo ""

    # --- Check spec file exists ------------------------------------------------
    local spec_file="$PROJECT_ROOT/xiaoda-agent.spec"
    if [ ! -f "$spec_file" ]; then
        die "Spec file not found: $spec_file"
    fi

    cd "$PROJECT_ROOT"
    python3 -m pip install onnx
    python3 -m pip install ".[local-ai]"

    # --- Build frontend ---------------------------------------------------------
    local frontend_dir="$PROJECT_ROOT/web/frontend"
    info "Building Web UI..."
    if [ ! -f "$frontend_dir/package.json" ]; then
        die "Frontend package.json not found: $frontend_dir/package.json"
    fi
    if ! command -v npm >/dev/null 2>&1; then
        die "npm not found. Node.js is required to build the Web UI."
    fi
    cd "$frontend_dir"
    if [ -f package-lock.json ]; then
        npm ci
    else
        npm install
    fi
    npm run build
    if [ ! -f "$PROJECT_ROOT/web/dist/index.html" ]; then
        die "Frontend build output missing: web/dist/index.html"
    fi

    # --- Run PyInstaller -------------------------------------------------------
    info "Running PyInstaller..."
    cd "$PROJECT_ROOT"
    pyinstaller xiaoda-agent.spec --clean --noconfirm
    if [ $? -ne 0 ]; then
        die "PyInstaller build failed."
    fi
    green "  PyInstaller build completed."

    # --- Verify the output directory -------------------------------------------
    local dist_dir="$PROJECT_ROOT/dist/xiaoda-agent"
    if [ ! -d "$dist_dir" ]; then
        die "Expected output directory not found: $dist_dir"
    fi

    # --- Verify CLI interactive modules bundled ---------------------------------
    # cli_palette / cli_menu 在 cli.py 中是 try/except 守卫导入，PyInstaller 静态
    # 分析可能漏掉；未打包会导致打包版 CLI 静默回退到基础输入，命令面板/滚动失效。
    # PyInstaller 6.x onedir 下 PYZ 归档嵌入主可执行文件的 CArchive（条目 PYZ.pyz），
    # dist 下没有独立 .pyz 文件；需用 pyi-archive_viewer 打开内嵌 PYZ.pyz 检查模块清单。
    info "Verifying CLI interactive modules bundled..."
    local exe_file
    exe_file=$(find "$dist_dir" -maxdepth 1 -type f \
        \( -name "xiaoda-agent" -o -name "xiaoda-agent.exe" \) \
        -print -quit 2>/dev/null || true)
    if [ -z "$exe_file" ]; then
        die "Executable not found in build output: $dist_dir"
    fi
    if ! command -v pyi-archive_viewer >/dev/null 2>&1; then
        die "pyi-archive_viewer not found (is PyInstaller installed?)"
    fi
    printf 'O PYZ.pyz\nq\n' | pyi-archive_viewer "$exe_file" > /tmp/pyz_contents.txt
    for _m in cli_palette cli_menu prompt_toolkit; do
        if grep -q "'$_m" /tmp/pyz_contents.txt; then
            green "  ✓ $_m bundled"
        else
            die "Module $_m NOT bundled! Interactive slash-command panel/menu will be disabled."
        fi
    done
    rm -f /tmp/pyz_contents.txt

    info "Verifying ORT GenAI frozen runtime..."
    printf 'O PYZ.pyz\nq\n' | pyi-archive_viewer "$exe_file" > /tmp/ort_genai_pyz_contents.txt
    if ! grep -q "'onnxruntime_genai" /tmp/ort_genai_pyz_contents.txt; then
        die "Module onnxruntime_genai NOT bundled."
    fi
    local ort_genai_library
    if [ "$os" = "windows" ]; then
        ort_genai_library=$(find "$dist_dir" -name "onnxruntime-genai*.dll" -print -quit 2>/dev/null || true)
    else
        ort_genai_library=$(find "$dist_dir" -name "libonnxruntime-genai*.so*" -print -quit 2>/dev/null || true)
    fi
    if [ -z "$ort_genai_library" ]; then
        die "ORT GenAI native library NOT bundled."
    fi
    local smoke_dir
    smoke_dir=$(mktemp -d)
    python3 "$PROJECT_ROOT/scripts/create_ort_genai_smoke_model.py" "$smoke_dir"
    "$exe_file" local-ai-smoke "$smoke_dir"
    rm -rf "$smoke_dir"
    rm -f /tmp/ort_genai_pyz_contents.txt

    # --- Write version stamp into dist directory --------------------------------
    echo -n "$version" > "$dist_dir/.version"
    # 不创建 .auto_update：自动更新必须由用户显式启用（与 CI 和 auto-update.bat 设计一致）
    info "Version stamp: $dist_dir/.version ($version)"

    # --- Create the distribution package ---------------------------------------
    local output_dir="$PROJECT_ROOT/dist/release"
    mkdir -p "$output_dir"

    local pkg_name="xiaoda-agent-${os}-${arch}-v${version}"

    cd "$PROJECT_ROOT/dist"

    if [ "$os" = "linux" ]; then
        # --- Linux: create .run self-extracting installer ----------------------
        local tar_name="${pkg_name}.tar.gz"
        local run_name="${pkg_name}.run"

        info "Copying Linux startup/CLI scripts into dist..."
        # 与 CI 保持一致：start-linux.sh / doctor.sh / auto-update.sh / xiaoda
        # 必须打进 dist，否则 install-linux.sh 的 systemd 服务与 xiaoda 命令会失效
        local _linux_scripts=("start-linux.sh" "doctor.sh" "auto-update.sh" "xiaoda")
        mkdir -p "$dist_dir/scripts"
        for _s in "${_linux_scripts[@]}"; do
            if [ ! -f "$SCRIPT_DIR/$_s" ]; then
                die "Required Linux script not found: $SCRIPT_DIR/$_s"
            fi
            cp "$SCRIPT_DIR/$_s" "$dist_dir/scripts/$_s"
            chmod +x "$dist_dir/scripts/$_s"
        done

        info "Creating tar.gz archive..."
        tar czf "$tar_name" xiaoda-agent

        info "Creating self-extracting installer..."
        cat "$SCRIPT_DIR/install-linux.sh" > "$output_dir/$run_name"
        echo '__ARCHIVE__' >> "$output_dir/$run_name"
        cat "$tar_name" >> "$output_dir/$run_name"
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
        # 与 CI 保持一致的 5 个启动脚本清单，本地构建也不能漏掉 auto-update.ps1
        local _win_scripts=("xiaoda.bat" "auto-update.bat" "auto-update.ps1" "open-browser.ps1" "doctor.bat")
        for _s in "${_win_scripts[@]}"; do
            if [ ! -f "$SCRIPT_DIR/$_s" ]; then
                die "Required Windows script not found: $SCRIPT_DIR/$_s"
            fi
            cp "$SCRIPT_DIR/$_s" "$dist_dir/$_s"
        done

        # Copy icon file for NSIS
        cp "$PROJECT_ROOT/assets/xiaoda-icon.ico" "$dist_dir/xiaoda-icon.ico"

        # Try NSIS first for .exe installer
        if command -v makensis &>/dev/null; then
            info "Creating NSIS installer (.exe) using scripts/installer.nsi..."

            # Use the maintained installer.nsi with /DVERSION= injection
            if makensis /DVERSION="$version" "$SCRIPT_DIR/installer.nsi" \
                -DOUTFILE="$output_dir/$exe_name" \
                -DDIST_DIR="$dist_dir"; then
                green "  NSIS installer created: $output_dir/$exe_name"
            else
                red "  NSIS build failed, falling back to ZIP..."
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
        tar czf "$output_dir/$tar_name" xiaoda-agent

        green "  Package created: $output_dir/$tar_name"
    fi

    echo ""
    echo "  $(bold "Build complete!")"
    echo ""
}

# ---- Main ---------------------------------------------------------------------
do_build
