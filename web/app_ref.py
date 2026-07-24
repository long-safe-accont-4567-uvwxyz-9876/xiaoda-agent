"""Web app 与 _start_services 单例引用 —— 解耦 web.routers.setup 与 web.server 的循环.

原 web.routers.setup 函数内 `from web.server import app, _start_services`,
而 web.server.create_app() 内又 `from web.routers.setup import router`, 形成循环:
    web.routers.setup <-> web.server

将 app 与 _start_services 的引用存到本模块, 由 web.server 在 create_app() 后 set,
web.routers.setup 通过 get_app() / get_start_services() 获取, 不再直接导入 web.server.
"""
from collections.abc import Callable
from typing import Any

_app: Any = None
_start_services_fn: Callable | None = None


def set_app(app: Any) -> None:
    """由 web.server.create_app() 末尾调用, 注册全局 app 引用."""
    global _app
    _app = app


def get_app() -> Any:
    """获取 FastAPI app 单例 (create_app() 执行后可用)."""
    return _app


def set_start_services(fn: Callable) -> None:
    """由 web.server 模块加载时调用, 注册 _start_services 引用."""
    global _start_services_fn
    _start_services_fn = fn


def get_start_services() -> Callable | None:
    """获取 _start_services 函数 (web.server 加载后可用)."""
    return _start_services_fn
