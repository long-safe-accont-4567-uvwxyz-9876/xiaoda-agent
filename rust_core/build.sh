#!/usr/bin/env bash
# 构建 rust_core Python 扩展并同步到运行时搜索路径
# 用法: bash rust_core/build.sh   （需 cargo，安装见 https://rustup.rs）
set -euo pipefail
cd "$(dirname "$0")"
# 非交互 shell 不加载 ~/.cargo/env，自举 PATH
if ! command -v cargo >/dev/null 2>&1 && [ -f "$HOME/.cargo/env" ]; then
  . "$HOME/.cargo/env"
fi
cargo build --release
# PyO3 cdylib 产物名 librust_core.so，Python 导入需要 rust_core.so
cp -f target/release/librust_core.so target/release/rust_core.so
# 同步到项目 venv（若存在），保证 pytest 无 PYTHONPATH 时也用新产物
VENV_SP="../.venv/lib/python3.11/site-packages"
if [ -d "$VENV_SP" ]; then
  cp -f target/release/librust_core.so "$VENV_SP/rust_core.so"
  echo "已同步到 $VENV_SP/rust_core.so"
fi
echo "构建完成: target/release/rust_core.so"
echo "启用方式: RUST_HYBRID_ENABLED=true + PYTHONPATH=<项目>/rust_core/target/release"
