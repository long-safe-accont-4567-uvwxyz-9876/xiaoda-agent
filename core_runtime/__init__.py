"""core_runtime — 中立运行时配置/凭证/提示词档案层。

消除底层模块(config.py/model_router/core/llm_gateway/memory)反向
import web/ 的分层倒置：这批模块原本住在 web/ 但没有任何 Web 框架
依赖(FastAPI/starlette 零引用)，实为运行时基础设施。

web/ 下保留同名 shim 重导出（一行 from core_runtime.X import * 式桥），
历史 import 面(web 层 50+ 处与全部测试)零改动；新代码请直接 import
core_runtime。
"""
