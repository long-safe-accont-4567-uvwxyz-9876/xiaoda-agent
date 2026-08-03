"""看门狗探活地址守护测试。

防止 P0 bug 回归：watchdog 用监听地址（如 0.0.0.0）去 ping，导致
Windows 上 ping 永远失败 → 误判冻结 → 无限重启。

根因：0.0.0.0 是"监听所有网卡"的通配地址，不是有效的客户端连接目标。
Windows 上 http://0.0.0.0:port 的请求会失败。watchdog 探活是本地行为，
必须用 loopback（127.0.0.1），与 server 监听地址解耦。
"""
import argparse
from utils.watchdog_runner import build_watchdog_config, DEFAULTS


def _parse(argv: list[str]) -> argparse.Namespace:
    """用 run_watchdog_cli 的同名 ArgumentParser 解析参数。"""
    p = argparse.ArgumentParser(prog="watchdog")
    p.add_argument("--port", type=int, default=8082)
    p.add_argument("--host", type=str, default="127.0.0.1")
    p.add_argument("--mode", choices=["web", "desktop"], default="web")
    p.add_argument("--check-interval", type=int, default=DEFAULTS["check_interval"])
    p.add_argument("--freeze-threshold", type=int, default=DEFAULTS["freeze_threshold"])
    p.add_argument("--max-restarts", type=int, default=DEFAULTS["max_restarts"])
    p.add_argument("--ping-retries", type=int, default=DEFAULTS["ping_retries"])
    p.add_argument("--log-file", type=str, default="")
    return p.parse_args(argv)


def test_ping_url_uses_loopback_when_host_is_wildcard():
    """host=0.0.0.0 时，ping_url 必须用 127.0.0.1（Windows 上 0.0.0.0 无法连接）。"""
    args = _parse(["--host", "0.0.0.0", "--port", "8082"])
    cfg = build_watchdog_config(args)

    assert cfg["ping_url"] == "http://127.0.0.1:8082/api/v1/ping", (
        f"ping_url 必须用 loopback，实际: {cfg['ping_url']}"
    )


def test_ping_url_uses_loopback_when_host_is_ipv6_wildcard():
    """host=:: 时，ping_url 必须用 127.0.0.1。"""
    args = _parse(["--host", "::", "--port", "8082"])
    cfg = build_watchdog_config(args)

    assert cfg["ping_url"] == "http://127.0.0.1:8082/api/v1/ping"


def test_ping_url_uses_loopback_when_host_empty():
    """host 为空时，ping_url 必须用 127.0.0.1。"""
    args = _parse(["--host", "", "--port", "8082"])
    cfg = build_watchdog_config(args)

    assert cfg["ping_url"] == "http://127.0.0.1:8082/api/v1/ping"


def test_main_cmd_keeps_listen_host():
    """主进程启动命令仍用原始 host（server 监听 0.0.0.0 是合理的，可局域网访问）。"""
    args = _parse(["--host", "0.0.0.0", "--port", "8082", "--mode", "desktop"])
    cfg = build_watchdog_config(args)

    cmd_str = " ".join(cfg["cmd"])
    assert "--host 0.0.0.0" in cmd_str, "主进程命令应保留原始监听地址"
    assert "--desktop" in cmd_str


def test_ping_url_with_explicit_loopback_host():
    """host=127.0.0.1 时，ping_url 正常用 127.0.0.1。"""
    args = _parse(["--host", "127.0.0.1", "--port", "8082"])
    cfg = build_watchdog_config(args)

    assert cfg["ping_url"] == "http://127.0.0.1:8082/api/v1/ping"
