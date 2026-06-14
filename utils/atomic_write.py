"""原子文件写入模块

借鉴 Hermes Agent 的原子写入机制，确保状态文件和配置文件写入时
不会因崩溃导致数据损坏。

核心策略：tempfile + fsync + os.replace
"""

import os
import json
import tempfile
from pathlib import Path
from loguru import logger


def _resolve_symlink(path: Path) -> Path:
    """解析符号链接，返回真实路径

    os.replace 在替换符号链接时，会将符号链接替换为常规文件。
    此函数解析符号链接后返回真实路径，确保替换的是目标文件而非链接本身。
    """
    if path.is_symlink():
        resolved = path.resolve()
        logger.debug(f"符号链接解析: {path} -> {resolved}")
        return resolved
    return path


def _get_file_mode(path: Path) -> int | None:
    """获取文件权限模式，文件不存在返回 None"""
    try:
        return path.stat().st_mode & 0o7777
    except FileNotFoundError:
        return None


def _preserve_file_mode(original_mode: int | None, new_path: Path) -> None:
    """恢复文件权限"""
    if original_mode is not None:
        try:
            os.chmod(new_path, original_mode)
        except OSError as e:
            logger.warning(f"恢复文件权限失败 {new_path}: {e}")


def atomic_write(target_path: str | Path, content: str | bytes,
                 mode: int | None = None, encoding: str = "utf-8") -> None:
    """原子写入文件

    使用 tempfile + fsync + os.replace 模式：
    1. 写入临时文件
    2. fsync 确保数据落盘
    3. os.replace 原子替换目标文件

    特殊处理：
    - 如果目标路径是符号链接，解析后再替换，防止符号链接被静默替换为常规文件
    - 保留原始文件权限

    Args:
        target_path: 目标文件路径
        content: 写入内容（str 或 bytes）
        mode: 文件权限模式（如 0o644），None 则保留原文件权限
        encoding: 文本编码，仅 content 为 str 时使用
    """
    target = Path(target_path)
    resolved = _resolve_symlink(target)

    # 获取原文件权限
    original_mode = _get_file_mode(resolved)
    effective_mode = mode if mode is not None else original_mode

    # 确保目标目录存在
    resolved.parent.mkdir(parents=True, exist_ok=True)

    # 写入临时文件
    tmp_fd = None
    tmp_path = None
    try:
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=resolved.parent,
            prefix=".atomic_",
        )
        tmp_path = Path(tmp_name)

        if isinstance(content, str):
            content_bytes = content.encode(encoding)
        else:
            content_bytes = content

        os.write(tmp_fd, content_bytes)
        os.fsync(tmp_fd)
        os.close(tmp_fd)
        tmp_fd = None

        # 设置权限
        if effective_mode is not None:
            os.chmod(tmp_path, effective_mode)

        # 原子替换
        os.replace(tmp_path, resolved)
        tmp_path = None

        logger.debug(f"原子写入完成: {resolved}")

    except Exception:
        # 清理临时文件
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def atomic_json_write(target_path: str | Path, data: dict | list,
                       mode: int | None = None, encoding: str = "utf-8",
                       indent: int = 2, ensure_ascii: bool = False) -> None:
    """原子写入 JSON 文件

    Args:
        target_path: 目标文件路径
        data: 要序列化的数据
        mode: 文件权限模式
        encoding: 文本编码
        indent: JSON 缩进
        ensure_ascii: 是否确保 ASCII 输出
    """
    content = json.dumps(data, indent=indent, ensure_ascii=ensure_ascii)
    atomic_write(target_path, content, mode=mode, encoding=encoding)


def atomic_yaml_write(target_path: str | Path, data: dict,
                       mode: int | None = None, encoding: str = "utf-8") -> None:
    """原子写入 YAML 文件（如果 PyYAML 可用）

    Args:
        target_path: 目标文件路径
        data: 要序列化的数据
        mode: 文件权限模式
        encoding: 文本编码
    """
    try:
        import yaml
    except ImportError:
        logger.error("PyYAML 未安装，无法写入 YAML 文件")
        raise

    content = yaml.dump(data, allow_unicode=True, default_flow_style=False)
    atomic_write(target_path, content, mode=mode, encoding=encoding)
