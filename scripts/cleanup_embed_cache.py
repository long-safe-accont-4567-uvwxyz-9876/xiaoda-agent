#!/usr/bin/env python3
"""embedding 缓存孤儿清理（审计 M3 遗留）。

背景：embed_cache_<model>__<local_dir>.npz 的命名在 M3 之后纳入了本地模型
目录身份，换过模型的机器上会留下旧身份的 npz 文件（不再被读取，但占空间，
且容易被误认为"缓存已就绪"）。

安全约定：
- 默认只读，仅列出「当前身份在用」与「孤儿（可删）」；
- 只有显式 --apply 才删除，且只删 embed_cache_*.npz，绝不碰 .db / 目录；
- 不判断"是否正在被别的进程使用"——服务运行期删除只影响下次冷启动的
  缓存命中，不会破坏数据；但仍建议停服或低峰期执行。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _resolve_targets(data_dir: str) -> tuple[Path, str]:
    """返回 (缓存目录, 当前身份的文件名)。"""
    import config  # 延迟导入：--help 不应拉起整个配置
    from memory.vector_store import (
        _default_embed_model,
        _default_local_model_dir,
        embed_cache_filename,
    )

    base = Path(data_dir) if data_dir else Path(getattr(config, "DATA_DIR", "data"))
    model = getattr(config, "EMBED_MODEL", "") or _default_embed_model()
    local_dir = getattr(config, "LOCAL_EMBED_MODEL_DIR", "") or _default_local_model_dir()
    return base, embed_cache_filename(model, local_dir)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="列出/清理 embedding 缓存孤儿文件")
    ap.add_argument("--data-dir", default="", help="缓存所在目录（默认 config.DATA_DIR）")
    ap.add_argument("--apply", action="store_true", help="真的删除孤儿文件（默认只打印）")
    args = ap.parse_args(argv)

    base, keep_name = _resolve_targets(args.data_dir)
    if not base.exists():
        print(f"[skip] 目录不存在: {base}")
        return 0

    files = sorted(base.glob("embed_cache_*.npz"))
    keep = [f for f in files if f.name == keep_name]
    orphans = [f for f in files if f.name != keep_name]

    print(f"目录: {base}")
    print(f"当前身份: {keep_name}")
    print(f"在用:   {keep[0] if keep else '（无，下次启动会重新建）'}")
    print(f"孤儿:   {len(orphans)} 个 / {sum(f.stat().st_size for f in orphans)} 字节")
    for f in orphans:
        print(f"  - {f.name}  {f.stat().st_size} 字节")

    if not orphans:
        return 0
    if not args.apply:
        print("\n[dry-run] 未删除任何文件；确认无误后加 --apply 执行。")
        return 0

    removed = 0
    for f in orphans:
        try:
            f.unlink()
            removed += 1
        except OSError as e:  # 权限/占用：报告并继续
            print(f"[error] 删除失败 {f.name}: {e}", file=sys.stderr)
    print(f"\n[applied] 已删除 {removed}/{len(orphans)} 个孤儿文件")
    return 0 if removed == len(orphans) else 1


if __name__ == "__main__":
    raise SystemExit(main())
