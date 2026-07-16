#!/bin/bash
cd /home/orangepi/ai-agent

# 配置 git 用户（如果未配置）
git config user.email "ai-agent@local"
git config user.name "AI Agent"

# 添加所有修改的文件（不包括 .git 和数据库文件）
git add \
  agent_context.py \
  agent_core/message_processor.py \
  agent_dispatcher.py \
  config.py \
  core/bootstrap.py \
  db/database.py \
  instinct_manager.py \
  memory/notebook_manager.py \
  memory/query_transform.py \
  model_router.py \
  tests/test_a4_rag_root_fix.py \
  tool_engine/error_rule_pipeline.py \
  utils/result_wrapper.py \
  xiaoli_agent.py

# 提交
git commit -m "$(cat <<'EOF'
fix: 修复答非所问、推理泄漏、幻觉严重、空回复、回复不完整的多项根因

核心修复：
1. 禁止用 reasoning_content 代替 content（推理泄漏根因）
   - model_router.py:969 空 content 抛异常触发 fallback
   - xiaoli_agent.py:374 / agent_dispatcher.py:797 同步修复
2. 空 content 一律触发 fallback（空回复根因）
   - model_router.py:966 移除 finish_reason != "stop" 条件
   - message_processor.py:298 fast_path 空 reply 返回 None
3. 快速路径碎片/空回复检测走主路径
   - message_processor.py:298 <30字且不以句末标点结尾 → 返回 None
4. verification loop 完整性检测（回复不完整时重试补全）
   - message_processor.py:103-123 <60字且不以句末标点结尾 → 追加"请继续"重试
5. 全局换掉 Z1 思考模型 → GLM-4-9B-0414（共同根因）
   - query_transform.py:44 默认模型改为 GLM-4-9B-0414
   - instinct_manager.py:62 / notebook_manager.py:63 / error_rule_pipeline.py:80 / result_wrapper.py:35 同步修复
   - bootstrap.py:422 统一注入免费模型配置
6. 禁用 instinct/notebook 注入（幻觉根因）
   - agent_context.py:431 注释掉 instinct_prompt 注入
   - notebook_manager.py:199 拒绝心理学关键词 + 限制长度 40 字
7. database.py executescript 拆分为独立 execute（避免 vfat database locked）
   - _run_migrations 拆分创建表 DDL（L161-180）
   - _apply_migration 防御性创建 migration_state（L274-290）

测试：32 passed in 4.87s
EOF
)"

# 推送到远程（SSH）
git push origin main

# 输出结果
echo "=== Git Push Complete ==="
git log --oneline -3
