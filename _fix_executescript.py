#!/usr/bin/env python3
"""批量修复 database.py 中的 executescript 调用。

将所有 executescript(...) 拆分为独立的 execute(...) 调用。
"""
import re
from pathlib import Path

db_file = Path("/home/orangepi/ai-agent/db/database.py")
content = db_file.read_text()

# 匹配 executescript("""...""")
pattern = r'await self\._conn\.executescript\("""(.*?)"""\)'

def split_ddl(match):
    """将 DDL 字符串拆分为独立的 execute 调用。"""
    ddl_block = match.group(1)
    # 按分号分割，过滤空行
    statements = [s.strip() for s in ddl_block.split(';') if s.strip()]
    # 生成独立的 execute 调用
    calls = []
    for stmt in statements:
        # 移除多余空白
        stmt = ' '.join(stmt.split())
        if stmt:
            calls.append(f'await self._conn.execute("""{stmt}""")')
    return '\n        '.join(calls)

# 替换所有 executescript
new_content = re.sub(pattern, split_ddl, content, flags=re.DOTALL)

# 写回文件
db_file.write_text(new_content)
print(f"✅ 已修复 {content.count('executescript')} 处 executescript 调用")