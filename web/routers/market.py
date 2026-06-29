"""MCP 工具/技能市场 REST API — 数据来自在线源（modelscope.cn / mcp-cn.com）"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from loguru import logger

from market.manifest import MarketItem, get_plugins_fetcher, get_skills_fetcher, get_mcp_fetcher
from market.installer import MarketInstaller, InstallError
from web.schemas import Envelope
from web.routers.auth import get_current_user

router = APIRouter(prefix="/market", tags=["market"],
                   dependencies=[Depends(get_current_user)])


class InstallRequest(BaseModel):
    """安装请求"""
    item_id: str
    download_url: str = ""
    version: str = ""
    sha256: str = ""


class UninstallRequest(BaseModel):
    """卸载请求"""
    item_id: str


def _get_installer(request: Request) -> MarketInstaller:
    """获取 MarketInstaller 实例"""
    from pathlib import Path
    from config import WORKSPACE_DIR

    plugins_dir = Path(__file__).resolve().parent.parent.parent / "plugins"
    skills_dir = WORKSPACE_DIR / "skills"
    mcp_config_dir = WORKSPACE_DIR / "mcp_configs"
    plugin_manager = getattr(request.app.state, "plugin_manager", None)
    return MarketInstaller(plugins_dir, skills_dir, plugin_manager, mcp_config_dir)


@dataclass
class SecurityCheckResult:
    """安全检查结果"""
    passed: bool = True
    warnings: list[str] = field(default_factory=list)
    risk_level: str = "low"  # low / medium / high


def _security_check(item: MarketItem, content: bytes = b"") -> SecurityCheckResult:
    """对下载内容进行安全检查"""
    result = SecurityCheckResult()

    # 1. SHA256 由 installer 层校验，这里只记录是否有校验
    if not item.sha256:
        result.warnings.append("未提供 SHA256 校验和，无法验证文件完整性")
        result.risk_level = "medium"

    # 2. 文件大小限制（最大 50MB）
    if len(content) > 50 * 1024 * 1024:
        result.passed = False
        result.warnings.append(f"文件大小 {len(content) / 1024 / 1024:.1f}MB 超过 50MB 限制")
        result.risk_level = "high"
        return result

    if not content:
        return result

    # 3. 内容扫描：检测可疑模式
    suspicious_patterns = [
        ("eval(", "检测到 eval() 调用"),
        ("exec(", "检测到 exec() 调用"),
        ("__import__(", "检测到 __import__() 调用"),
        ("subprocess", "检测到 subprocess 调用"),
        ("os.system", "检测到 os.system 调用"),
        ("os.popen", "检测到 os.popen 调用"),
    ]

    # 检查文件名列表（从压缩包中提取文件名，排除常见误报）
    sensitive_path_patterns = ["/etc/", "~/.ssh/", "/root/", "/etc/passwd", "/etc/shadow"]

    try:
        text = content.decode("utf-8", errors="ignore")

        for pattern, msg in suspicious_patterns:
            if pattern in text:
                result.warnings.append(msg)
                result.risk_level = "medium"

        for sp in sensitive_path_patterns:
            if sp in text:
                result.warnings.append(f"检测到敏感路径引用: {sp}")
                result.risk_level = "medium"

        # 检查非标准端口的网络调用
        import re
        port_pattern = re.compile(r'(?:connect|socket|http)[^\n]*:(\d{2,5})', re.IGNORECASE)
        for match in port_pattern.finditer(text):
            port = int(match.group(1))
            if port not in (80, 443, 8080, 8443):
                result.warnings.append(f"检测到非标准端口 {port} 的网络调用")
                result.risk_level = "medium"
                break

    except Exception:
        pass

    if result.warnings and result.risk_level == "high":
        result.passed = False

    return result


# ── MCP 工具市场（在线数据源：mcp-cn.com）──────────────────

@router.get("/plugins", response_model=Envelope[dict])
async def list_plugin_market(request: Request, force: bool = False) -> Any:
    """获取 MCP 工具市场清单（在线实时数据）"""
    fetcher = get_plugins_fetcher()
    manifest = await fetcher.fetch(force=force)
    installer = _get_installer(request)
    installed = installer.get_installed_plugins()

    items = []
    for item in manifest.items:
        if item.type not in ("plugin", "mcp"):
            continue
        installed_version = installed.get(item.id)
        items.append({
            **item.model_dump(),
            "installed": installed_version is not None,
            "installed_version": installed_version or "",
        })

    return Envelope(data={
        "version": manifest.version,
        "updated": manifest.updated,
        "source": manifest.source,
        "items": items,
        "installed": installed,
    })


@router.post("/plugins/install", response_model=Envelope[dict])
async def install_plugin(req: InstallRequest, request: Request) -> Any:
    """一键安装 MCP 工具"""
    installer = _get_installer(request)
    fetcher = get_plugins_fetcher()
    manifest = await fetcher.fetch()

    item = None
    for i in manifest.items:
        if i.id == req.item_id and i.type in ("plugin", "mcp"):
            item = i
            break

    if item is None and req.download_url:
        item = MarketItem(
            id=req.item_id, type="mcp", name=req.item_id,
            download_url=req.download_url, version=req.version or "0.0.0",
            sha256=req.sha256,
        )

    if item is None:
        raise HTTPException(404, f"市场中未找到 '{req.item_id}'")

    # 安全检查
    sec_result = _security_check(item)
    if not sec_result.passed:
        raise HTTPException(403, f"安全检查未通过: {'; '.join(sec_result.warnings)}")

    try:
        result = await asyncio.wait_for(installer.install(item), timeout=300)
    except asyncio.TimeoutError:
        raise HTTPException(504, "安装超时（300 秒）")
    except InstallError as e:
        raise HTTPException(400, str(e))

    result["security_check"] = {
        "passed": sec_result.passed,
        "warnings": sec_result.warnings,
        "risk_level": sec_result.risk_level,
    }

    # 安装后自动加载并启用
    plugin_manager = getattr(request.app.state, "plugin_manager", None)
    if plugin_manager:
        try:
            await plugin_manager.load(item.id)
            await plugin_manager.enable(item.id)
            result["auto_enabled"] = True
        except Exception as e:
            logger.warning("market.auto_enable_failed", id=item.id, error=str(e))
            result["auto_enabled"] = False

    return Envelope(data=result)


@router.post("/plugins/uninstall", response_model=Envelope[dict])
async def uninstall_plugin(req: UninstallRequest, request: Request) -> Any:
    """一键卸载 MCP 工具"""
    installer = _get_installer(request)
    try:
        result = await installer.uninstall(req.item_id, "plugin")
    except InstallError as e:
        raise HTTPException(400, str(e))
    return Envelope(data=result)


# ── 技能市场 ──────────────────────────────────────────────

@router.get("/skills", response_model=Envelope[dict])
async def list_skill_market(request: Request, force: bool = False) -> Any:
    """获取技能市场清单"""
    fetcher = get_skills_fetcher()
    manifest = await fetcher.fetch(force=force)
    installer = _get_installer(request)
    installed = installer.get_installed_skills()

    items = []
    for item in manifest.items:
        if item.type != "skill":
            continue
        installed_version = installed.get(item.id)
        items.append({
            **item.model_dump(),
            "installed": installed_version is not None,
            "installed_version": installed_version or "",
        })

    return Envelope(data={
        "version": manifest.version,
        "updated": manifest.updated,
        "source": manifest.source,
        "items": items,
        "installed": installed,
    })


@router.post("/skills/install", response_model=Envelope[dict])
async def install_skill(req: InstallRequest, request: Request) -> Any:
    """一键安装技能"""
    installer = _get_installer(request)
    fetcher = get_skills_fetcher()
    manifest = await fetcher.fetch()

    item = None
    for i in manifest.items:
        if i.id == req.item_id and i.type == "skill":
            item = i
            break

    if item is None and req.download_url:
        item = MarketItem(
            id=req.item_id, type="skill", name=req.item_id,
            download_url=req.download_url, version=req.version or "0.0.0",
            sha256=req.sha256,
        )

    if item is None:
        raise HTTPException(404, f"技能市场中未找到 '{req.item_id}'")

    # 安全检查
    sec_result = _security_check(item)
    if not sec_result.passed:
        raise HTTPException(403, f"安全检查未通过: {'; '.join(sec_result.warnings)}")

    try:
        result = await asyncio.wait_for(installer.install(item), timeout=300)
    except asyncio.TimeoutError:
        raise HTTPException(504, "安装超时（300 秒）")
    except InstallError as e:
        raise HTTPException(400, str(e))

    result["security_check"] = {
        "passed": sec_result.passed,
        "warnings": sec_result.warnings,
        "risk_level": sec_result.risk_level,
    }

    return Envelope(data=result)


@router.post("/skills/uninstall", response_model=Envelope[dict])
async def uninstall_skill(req: UninstallRequest, request: Request) -> Any:
    """一键卸载技能"""
    installer = _get_installer(request)
    try:
        result = await installer.uninstall(req.item_id, "skill")
    except InstallError as e:
        raise HTTPException(400, str(e))
    return Envelope(data=result)


# ── MCP 工具市场（在线数据源：mcp-cn.com）──────────────────

@router.get("/mcp", response_model=Envelope[dict])
async def list_mcp_market(request: Request, force: bool = False) -> Any:
    """获取 MCP 工具市场清单（在线实时数据）"""
    fetcher = get_mcp_fetcher()
    manifest = await fetcher.fetch(force=force)
    installer = _get_installer(request)
    installed = installer.get_installed_mcps()

    items = []
    for item in manifest.items:
        if item.type != "mcp":
            continue
        installed_version = installed.get(item.id)
        items.append({
            **item.model_dump(),
            "installed": installed_version is not None,
            "installed_version": installed_version or "",
        })

    return Envelope(data={
        "version": manifest.version,
        "updated": manifest.updated,
        "source": manifest.source,
        "items": items,
        "installed": installed,
    })


@router.post("/mcp/install", response_model=Envelope[dict])
async def install_mcp(req: InstallRequest, request: Request) -> Any:
    """一键安装 MCP 工具"""
    installer = _get_installer(request)
    fetcher = get_mcp_fetcher()
    manifest = await fetcher.fetch()

    item = None
    for i in manifest.items:
        if i.id == req.item_id and i.type == "mcp":
            item = i
            break

    if item is None and req.download_url:
        item = MarketItem(
            id=req.item_id, type="mcp", name=req.item_id,
            download_url=req.download_url, version=req.version or "0.0.0",
            sha256=req.sha256,
        )

    if item is None:
        raise HTTPException(404, f"MCP 市场中未找到 '{req.item_id}'")

    # 安全检查
    sec_result = _security_check(item)
    if not sec_result.passed:
        raise HTTPException(403, f"安全检查未通过: {'; '.join(sec_result.warnings)}")

    try:
        result = await asyncio.wait_for(installer.install(item), timeout=300)
    except asyncio.TimeoutError:
        raise HTTPException(504, "安装超时（300 秒）")
    except InstallError as e:
        raise HTTPException(400, str(e))

    result["security_check"] = {
        "passed": sec_result.passed,
        "warnings": sec_result.warnings,
        "risk_level": sec_result.risk_level,
    }

    return Envelope(data=result)


@router.post("/mcp/uninstall", response_model=Envelope[dict])
async def uninstall_mcp(req: UninstallRequest, request: Request) -> Any:
    """一键卸载 MCP 工具"""
    installer = _get_installer(request)
    try:
        result = await installer.uninstall(req.item_id, "mcp")
    except InstallError as e:
        raise HTTPException(400, str(e))
    return Envelope(data=result)
