# 🧠 纳西妲 AI Agent - 记忆系统

## 概述

记忆系统分为三个层次：

### 1. 短期记忆（对话上下文）
- 存储在内存中，维护最近对话轮次

### 2. 长期记忆（SQLite）
- 存储在 `data/memory.db` 中
- 记录用户偏好、重要事件、知识片段

### 3. 向量记忆（sqlite-vec）
- 使用 BGE-M3 模型生成嵌入
- 支持语义相似度检索

## 记忆操作

- `remember(content, tags)` - 保存新记忆
- `recall(query)` - 检索相关记忆
- `forget(query)` - 删除特定记忆
