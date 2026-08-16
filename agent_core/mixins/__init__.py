"""agent_core.mixins 包 —— 从 message_processor.py 拆分出的叶子 Mixin。

Phase 1：问候（greeting.py / GreetingMixin）与语音判定（voice.py / VoiceMixin）。
Phase 2：Harness 验收循环（verification.py / VerificationMixin）。
Phase 3：主处理路径（main_path.py / MainPathMixin）。

叶子模块依赖约定：包内模块只依赖 agent_core._shared 及 config/core 叶子模块，
不得 import agent_core.message_processor（避免循环导入）。
"""
