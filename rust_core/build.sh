#!/usr/bin/env bash
# 构建 rust_core Python 扩展（本机 aarch64/x86_64 通用）
# 用法: bash rust_core/build.sh   （需 cargo，安装见 https://rustup.rs）
set -euo pipefail
cd "$(dirname "$0")"
cargo build --release
# PyO3 cdylib 产物名 librust_core.so，Python 导入需要 rust_core.so
cp -f target/release/librust_core.so target/release/rust_core.so
SITE_DIR=$(python3 -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
cp -f target/release/rust_core.so "$SITE_DIR/rust_core.so"
echo "构建完成并已安装: $SITE_DIR/rust_core.so"
echo "启用方式: .env 追加 RUST_HYBRID_ENABLED=1 后重启服务"
echo "注意：不能只用 PYTHONPATH 指向 target/release——项目根的 rust_core/ Cargo 目录会遮蔽模块名"
