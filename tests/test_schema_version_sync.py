"""验证 CURRENT_SCHEMA_VERSION 与迁移列表最后一个版本一致。

缺陷 D5: CURRENT_SCHEMA_VERSION 曾为 9，而实际迁移列表已扩展到 v20，
导致新安装的数据库跳过 v10~v20 的 DDL，运行时访问不存在的列而报错。
"""
from __future__ import annotations

import ast
import inspect


def test_current_schema_version_matches_last_migration():
    """CURRENT_SCHEMA_VERSION 必须等于迁移列表中的最大版本号。"""
    from db.database import CURRENT_SCHEMA_VERSION

    # 通过源码 AST 提取 migrations 列表中的最大版本号
    import db.database as db_mod
    source = inspect.getsource(db_mod)
    tree = ast.parse(source)

    max_version = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.List):
            for elt in node.elts:
                if isinstance(elt, ast.Tuple) and len(elt.elts) >= 1:
                    first = elt.elts[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, int):
                        max_version = max(max_version, first.value)

    assert CURRENT_SCHEMA_VERSION == max_version, (
        f"CURRENT_SCHEMA_VERSION ({CURRENT_SCHEMA_VERSION}) 不等于 "
        f"迁移列表最大版本 ({max_version})，会导致新库跳过部分迁移"
    )
