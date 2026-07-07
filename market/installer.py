"""市场安装器 — 下载、验证、安装/卸载插件与技能"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
import zipfile
import tarfile
from pathlib import Path
from typing import Any

from loguru import logger

from market.manifest import MarketItem


class InstallError(Exception):
    """安装/卸载错误"""


class MarketInstaller:
    """市场安装器"""

    def __init__(
        self,
        plugins_dir: Path,
        skills_dir: Path,
        plugin_manager: Any | None = None,
        mcp_config_dir: Path | None = None,
    ) -> None:
        self._plugins_dir = plugins_dir
        self._skills_dir = skills_dir
        self._plugin_manager = plugin_manager
        self._mcp_config_dir = mcp_config_dir or plugins_dir.parent / "mcp_configs"

    def get_installed_plugins(self) -> dict[str, str]:
        """返回已安装的插件 {id: version}"""
        installed: dict[str, str] = {}
        if not self._plugins_dir.is_dir():
            return installed
        for child in self._plugins_dir.iterdir():
            yaml_path = child / "plugin.yaml"
            if child.is_dir() and yaml_path.exists():
                try:
                    import yaml
                    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
                    installed[data.get("id", child.name)] = data.get("version", "0.0.0")
                except Exception:
                    logger.debug("installer.yaml_parse_error path={}", yaml_path, exc_info=True)
                    installed[child.name] = "0.0.0"
        return installed

    def get_installed_skills(self) -> dict[str, str]:
        """返回已安装的技能 {name: version}"""
        installed: dict[str, str] = {}
        if not self._skills_dir.is_dir():
            return installed
        for fp in self._skills_dir.glob("*.md"):
            # 技能文件头部可包含 version 元数据
            content = fp.read_text(encoding="utf-8-sig")
            version = "0.0.0"
            if content.startswith("<!--"):
                end = content.find("-->")
                if end > 0:
                    meta = content[4:end].strip()
                    for line in meta.split("\n"):
                        if line.strip().startswith("version:"):
                            version = line.split(":", 1)[1].strip().strip("'\"")
            installed[fp.stem] = version
        return installed

    def get_installed_mcps(self) -> dict[str, str]:
        """返回已安装的 MCP 工具 {id: version}"""
        installed: dict[str, str] = {}
        if not self._mcp_config_dir.is_dir():
            return installed
        for fp in self._mcp_config_dir.glob("*.json"):
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                installed[fp.stem] = data.get("version", "0.0.0")
            except Exception:
                logger.debug("installer.json_parse_error path={}", fp, exc_info=True)
                installed[fp.stem] = "0.0.0"
        return installed

    async def install(self, item: MarketItem, env: dict[str, str] | None = None) -> dict[str, Any]:
        """安装一个市场条目"""
        if item.type == "plugin":
            return await self._install_plugin(item)
        elif item.type == "skill":
            return await self._install_skill(item)
        elif item.type == "mcp":
            return await self._install_mcp(item, env=env)
        raise InstallError(f"未知类型: {item.type}")

    async def uninstall(self, item_id: str, item_type: str) -> dict[str, Any]:
        """卸载一个市场条目"""
        if item_type == "plugin":
            return await self._uninstall_plugin(item_id)
        elif item_type == "skill":
            return await self._uninstall_skill(item_id)
        elif item_type == "mcp":
            return await self._uninstall_mcp(item_id)
        raise InstallError(f"未知类型: {item_type}")

    # ── 插件安装 ──────────────────────────────────────────────

    async def _install_plugin(self, item: MarketItem) -> dict[str, Any]:
        """下载并安装插件"""
        target_dir = self._plugins_dir / item.id

        # 下载到临时目录
        tmp_dir = Path(tempfile.mkdtemp(prefix="market_plugin_"))
        try:
            archive_path = await self._download(item.download_url, tmp_dir)

            # SHA256 校验
            if item.sha256:
                self._verify_sha256(archive_path, item.sha256)

            # 安全扫描下载内容
            self._security_scan(archive_path.read_bytes(), item.id)

            # 解压
            extract_dir = tmp_dir / "extracted"
            self._extract(archive_path, extract_dir)

            # 查找 plugin.yaml 确认是合法插件
            yaml_path = self._find_plugin_yaml(extract_dir)
            if not yaml_path:
                raise InstallError("下载的包中未找到 plugin.yaml，不是合法的插件包")

            # 如果已存在，先卸载旧版本
            if target_dir.exists():
                logger.info("market.plugin_upgrade", id=item.id, old_dir=str(target_dir))
                await self._unload_plugin_if_loaded(item.id)
                shutil.rmtree(target_dir)

            # 移动到目标目录
            plugin_src = yaml_path.parent
            shutil.move(str(plugin_src), str(target_dir))

            # 触发插件发现
            if self._plugin_manager:
                self._plugin_manager.discover()

            logger.info("market.plugin_installed", id=item.id, version=item.version)

            # 安装后验证
            verify = await self._verify_plugin(item.id, target_dir)

            return {"status": "ok", "id": item.id, "type": "plugin",
                    "version": item.version, "verify": verify}

        except Exception as e:
            logger.error("market.plugin_install_failed", id=item.id, error=str(e))
            raise InstallError(f"插件安装失败: {e}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    async def _uninstall_plugin(self, plugin_id: str) -> dict[str, Any]:
        """卸载插件"""
        target_dir = self._plugins_dir / plugin_id
        if not target_dir.exists():
            raise InstallError(f"插件 '{plugin_id}' 未安装")

        # 先从运行时卸载
        await self._unload_plugin_if_loaded(plugin_id)

        # 删除目录
        shutil.rmtree(target_dir)
        logger.info("market.plugin_uninstalled", id=plugin_id)
        return {"status": "ok", "id": plugin_id, "type": "plugin"}

    async def _unload_plugin_if_loaded(self, plugin_id: str) -> None:
        """如果插件已加载，先从运行时卸载"""
        if not self._plugin_manager:
            return
        try:
            record = self._plugin_manager.get_plugin(plugin_id)
            if record:
                from plugins.manager import PluginState
                if record.state in (PluginState.ENABLED, PluginState.LOADED):
                    if record.state == PluginState.ENABLED:
                        await self._plugin_manager.disable(plugin_id)
                    await self._plugin_manager.unload(plugin_id)
        except Exception as e:
            logger.warning("market.plugin_unload_warning", id=plugin_id, error=str(e))

    def _find_plugin_yaml(self, base_dir: Path) -> Path | None:
        """在解压目录中查找 plugin.yaml"""
        # 直接在根目录
        direct = base_dir / "plugin.yaml"
        if direct.exists():
            return direct
        # 在一级子目录中
        for child in base_dir.iterdir():
            if child.is_dir():
                yaml_path = child / "plugin.yaml"
                if yaml_path.exists():
                    return yaml_path
        return None

    # ── 技能安装 ──────────────────────────────────────────────

    async def _install_skill(self, item: MarketItem) -> dict[str, Any]:
        """下载并安装技能"""
        self._skills_dir.mkdir(parents=True, exist_ok=True)
        target_path = self._skills_dir / f"{item.id}.md"

        tmp_dir = Path(tempfile.mkdtemp(prefix="market_skill_"))
        try:
            file_path = await self._download(item.download_url, tmp_dir)

            # SHA256 校验
            if item.sha256:
                self._verify_sha256(file_path, item.sha256)

            # 安全扫描下载内容
            self._security_scan(file_path.read_bytes(), item.id)

            # 如果是压缩包，解压查找 .md 文件
            if self._is_archive(file_path):
                extract_dir = tmp_dir / "extracted"
                self._extract(file_path, extract_dir)
                md_files = list(extract_dir.rglob("*.md"))
                if not md_files:
                    raise InstallError("压缩包中未找到 .md 技能文件")
                content = md_files[0].read_text(encoding="utf-8-sig")
            else:
                content = file_path.read_text(encoding="utf-8-sig")

            # 添加版本元数据
            version_meta = f"<!-- version: {item.version} -->\n"
            if content.startswith("<!--"):
                # 已有元数据，替换 version
                end = content.find("-->")
                if end > 0:
                    content = version_meta[:end + 3] + content[end + 3:]
                else:
                    content = version_meta + content
            else:
                content = version_meta + content

            target_path.write_text(content, encoding="utf-8-sig")
            logger.info("market.skill_installed", id=item.id, version=item.version)

            # 安装后验证
            verify = self._verify_skill(target_path, content)

            return {"status": "ok", "id": item.id, "type": "skill",
                    "version": item.version, "verify": verify}

        except Exception as e:
            logger.error("market.skill_install_failed", id=item.id, error=str(e))
            raise InstallError(f"技能安装失败: {e}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    async def _uninstall_skill(self, skill_id: str) -> dict[str, Any]:
        """卸载技能"""
        target_path = self._skills_dir / f"{skill_id}.md"
        if not target_path.exists():
            raise InstallError(f"技能 '{skill_id}' 未安装")
        target_path.unlink()
        logger.info("market.skill_uninstalled", id=skill_id)
        return {"status": "ok", "id": skill_id, "type": "skill"}

    # ── MCP 工具安装 ─────────────────────────────────────────

    async def _install_mcp(self, item: MarketItem, env: dict[str, str] | None = None) -> dict[str, Any]:
        """下载并安装 MCP 工具（写入配置文件）"""
        self._mcp_config_dir.mkdir(parents=True, exist_ok=True)
        config_path = self._mcp_config_dir / f"{item.id}.json"

        tmp_dir = Path(tempfile.mkdtemp(prefix="market_mcp_"))
        try:
            # 如果有下载链接，下载并解析配置
            if item.download_url:
                file_path = await self._download(item.download_url, tmp_dir)

                # SHA256 校验
                if item.sha256:
                    self._verify_sha256(file_path, item.sha256)

                # 安全扫描下载内容
                content_bytes = file_path.read_bytes()
                self._security_scan(content_bytes, item.id)

                # 解析配置
                if self._is_archive(file_path):
                    extract_dir = tmp_dir / "extracted"
                    self._extract(file_path, extract_dir)
                    json_files = list(extract_dir.rglob("*.json"))
                    if not json_files:
                        raise InstallError("压缩包中未找到 MCP 配置文件（.json）")
                    config_data = json.loads(json_files[0].read_text(encoding="utf-8"))
                else:
                    config_data = json.loads(content_bytes.decode("utf-8"))
            else:
                config_data = {}

            # 解析 connections 为 MCP server 结构（{command, args, env}）
            connections = self._parse_connections(
                item.connections or config_data.get("connections", ""),
                config_data,
                env,
            )

            # 写入配置文件
            config_entry = {
                "id": item.id,
                "name": item.name,
                "version": item.version,
                "description": item.description,
                "author": item.author,
                "qualified_name": item.qualified_name,
                "connections": connections,
                "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            config_path.write_text(
                json.dumps(config_entry, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            logger.info("market.mcp_installed", id=item.id, version=item.version)
            return {"status": "ok", "id": item.id, "type": "mcp",
                    "version": item.version, "connections": connections}

        except Exception as e:
            logger.error("market.mcp_install_failed", id=item.id, error=str(e))
            raise InstallError(f"MCP 工具安装失败: {e}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @staticmethod
    def _parse_connections(
        connections: str | dict,
        config_data: dict | None = None,
        env: dict[str, str] | None = None,
    ) -> dict:
        """将 connections 字段解析为 MCP server 结构 {command, args, env}。"""
        result: dict = {}

        # 如果 connections 已经是 dict，直接使用
        if isinstance(connections, dict):
            result = dict(connections)
        elif isinstance(connections, str) and connections:
            # 尝试 JSON 解析
            try:
                parsed = json.loads(connections)
                if isinstance(parsed, dict):
                    result = parsed
            except Exception:
                logger.debug("installer.connections_parse_error", exc_info=True)
                # 尝试解析 "npx -y @xxx/server" 格式的命令行
                parts = connections.strip().split()
                if parts:
                    result = {"command": parts[0], "args": parts[1:]}

        # 从 config_data 补充（如果 connections 不完整）
        if config_data and not result.get("command"):
            for key in ("command", "args", "env"):
                if key in config_data and key not in result:
                    result[key] = config_data[key]

        # 合并用户提供的 env
        if env:
            existing_env = result.get("env", {})
            if isinstance(existing_env, dict):
                existing_env.update(env)
                result["env"] = existing_env
            else:
                result["env"] = env

        return result

    async def _uninstall_mcp(self, mcp_id: str) -> dict[str, Any]:
        """卸载 MCP 工具"""
        config_path = self._mcp_config_dir / f"{mcp_id}.json"
        if not config_path.exists():
            raise InstallError(f"MCP 工具 '{mcp_id}' 未安装")
        config_path.unlink()
        logger.info("market.mcp_uninstalled", id=mcp_id)
        return {"status": "ok", "id": mcp_id, "type": "mcp"}

    # ── 通用工具 ──────────────────────────────────────────────

    def _security_scan(self, content: bytes, item_id: str) -> None:
        """对下载内容进行基础安全扫描"""
        suspicious = []
        try:
            text = content.decode("utf-8", errors="ignore")

            patterns = [
                ("eval(", "eval()"),
                ("exec(", "exec()"),
                ("__import__(", "__import__()"),
                ("subprocess", "subprocess"),
                ("os.system", "os.system"),
                ("os.popen", "os.popen"),
            ]
            for pattern, label in patterns:
                if pattern in text:
                    suspicious.append(label)

            # 敏感路径
            for sp in ["/etc/passwd", "/etc/shadow", "~/.ssh/"]:
                if sp in text:
                    suspicious.append(f"敏感路径: {sp}")

        except Exception:
            logger.debug("installer.security_scan_error", exc_info=True)

        if suspicious:
            logger.warning("market.security_scan_warnings", id=item_id,
                           warnings=suspicious)

    async def _download(self, url: str, dest_dir: Path) -> Path:
        """下载文件到目标目录"""
        import httpx
        filename = url.split("/")[-1].split("?")[0] or "download"
        dest_path = dest_dir / filename

        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with open(dest_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=8192):
                        f.write(chunk)

        logger.debug("market.downloaded", url=url, size=dest_path.stat().st_size)
        return dest_path

    def _verify_sha256(self, file_path: Path, expected: str) -> None:
        """校验 SHA256"""
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        actual = sha256.hexdigest()
        if actual.lower() != expected.lower():
            raise InstallError(
                f"SHA256 校验失败: 期望 {expected[:16]}...，实际 {actual[:16]}..."
            )

    def _extract(self, archive_path: Path, dest_dir: Path) -> None:
        """解压压缩包"""
        dest_dir.mkdir(parents=True, exist_ok=True)
        if zipfile.is_zipfile(archive_path):
            with zipfile.ZipFile(archive_path) as zf:
                zf.extractall(dest_dir)
        elif tarfile.is_tarfile(archive_path):
            with tarfile.open(archive_path) as tf:
                # 安全解压：防止路径穿越
                for member in tf.getmembers():
                    member_path = dest_dir / member.name
                    if not str(member_path.resolve()).startswith(str(dest_dir.resolve())):
                        raise InstallError(f"不安全的压缩包路径: {member.name}")
                tf.extractall(dest_dir)
        else:
            raise InstallError(f"不支持的压缩格式: {archive_path.name}")

    @staticmethod
    def _is_archive(path: Path) -> bool:
        """判断是否为压缩包"""
        suffixes = path.suffixes
        return any(
            s in (".zip", ".tar", ".gz", ".tgz", ".tar.gz", ".bz2")
            for s in suffixes
        ) or zipfile.is_zipfile(path) or tarfile.is_tarfile(path)

    # ── 安装后验证 ──────────────────────────────────────────────

    async def _verify_plugin(self, plugin_id: str, plugin_dir: Path) -> dict[str, Any]:
        """验证已安装的插件是否可用"""
        result: dict[str, Any] = {"ok": False, "checks": []}

        # 检查 1: plugin.yaml 存在且可解析
        yaml_path = plugin_dir / "plugin.yaml"
        if not yaml_path.exists():
            result["checks"].append({"name": "plugin.yaml", "status": "fail", "detail": "文件不存在"})
            return result
        try:
            import yaml
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
            result["checks"].append({"name": "plugin.yaml", "status": "ok"})
        except Exception as e:
            result["checks"].append({"name": "plugin.yaml", "status": "fail", "detail": str(e)})
            return result

        # 检查 2: 入口模块可导入
        entry = data.get("entry", "")
        if entry:
            module_name = entry.replace("/", ".").replace("\\", ".").rstrip(".py")
            try:
                import importlib
                importlib.import_module(f"plugins.{plugin_id}.{module_name}")
                result["checks"].append({"name": "entry_module", "status": "ok"})
            except Exception as e:
                result["checks"].append({"name": "entry_module", "status": "fail", "detail": str(e)})
                return result

        # 检查 3: 已注册到运行时
        if self._plugin_manager:
            try:
                record = self._plugin_manager.get_plugin(plugin_id)
                if record:
                    result["checks"].append({"name": "registered", "status": "ok"})
                else:
                    result["checks"].append({"name": "registered", "status": "warn",
                                             "detail": "插件未被发现，可能需要扫描"})
            except Exception as e:
                result["checks"].append({"name": "registered", "status": "warn", "detail": str(e)})

        result["ok"] = all(c["status"] != "fail" for c in result["checks"])
        return result

    def _verify_skill(self, path: Path, content: str) -> dict[str, Any]:
        """验证已安装的技能是否可用"""
        result: dict[str, Any] = {"ok": False, "checks": []}

        # 检查 1: 文件存在
        if not path.exists():
            result["checks"].append({"name": "file_exists", "status": "fail"})
            return result
        result["checks"].append({"name": "file_exists", "status": "ok"})

        # 检查 2: 内容非空且有实质内容
        text = content.strip()
        if len(text) < 20:
            result["checks"].append({"name": "content", "status": "fail",
                                     "detail": f"内容过短（{len(text)} 字符）"})
            return result
        result["checks"].append({"name": "content", "status": "ok",
                                 "detail": f"{len(text)} 字符"})

        # 检查 3: 已在 skills 目录中
        result["checks"].append({"name": "deployed", "status": "ok"})

        result["ok"] = all(c["status"] != "fail" for c in result["checks"])
        return result
