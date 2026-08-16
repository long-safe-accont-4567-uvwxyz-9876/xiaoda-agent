"""agent_core.mixins 包 —— Phase 1 从 message_processor.py 拆分出的叶子 Mixin。

叶子模块依赖约定：包内模块只依赖 agent_core._shared 及 config/core 叶子模块，
不得 import agent_core.message_processor（避免循环导入）。
"""
