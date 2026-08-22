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

# 契约探针：同步前验证新产物可导入且满足 Python 侧契约
# （CONTRACT_VERSION 相等 + NodeIndex 符号齐全），防止陈旧/不完整二进制
# 流入运行时——否则会在使用点爆 AttributeError 而非在构建期报错。
PY_BIN="$PWD/../.venv/bin/python"
[ -x "$PY_BIN" ] || PY_BIN="$(command -v python3)"
if ! PYTHONPATH="target/release" "$PY_BIN" - <<'EOF'
import sys
import rust_core

missing = [a for a in ("NodeIndex", "CONTRACT_VERSION") if not hasattr(rust_core, a)]
assert not missing, f"产物缺符号: {missing}"
sys.path.insert(0, "..")  # 读 Python 侧期望版本（单行提取，避免整包 import 的副作用）
import re
src = open("../memory/rust_hybrid.py", encoding="utf-8").read()
want = int(re.search(r"RUST_CORE_CONTRACT_VERSION\s*=\s*(\d+)", src).group(1))
got = rust_core.CONTRACT_VERSION
assert got == want, f"契约版本不一致: 二进制={got} Python={want}，请双侧同步 bump"
for m in ("direct_channel", "load_edges", "spreading_channel", "size"):
    assert hasattr(rust_core.NodeIndex, m), f"NodeIndex 缺方法: {m}"
print(f"契约探针通过: v{got}, NodeIndex 符号齐全")
EOF
then
  echo "错误：rust_core.so 未通过契约探针，已中止同步到 venv" >&2
  exit 1
fi

# 同步到项目 venv（若存在），保证 pytest 无 PYTHONPATH 时也用新产物
VENV_SP="../.venv/lib/python3.11/site-packages"
if [ -d "$VENV_SP" ]; then
  cp -f target/release/librust_core.so "$VENV_SP/rust_core.so"
  echo "已同步到 $VENV_SP/rust_core.so"
fi
echo "构建完成: target/release/rust_core.so"
echo "启用方式: RUST_HYBRID_ENABLED=true + PYTHONPATH=<项目>/rust_core/target/release"
