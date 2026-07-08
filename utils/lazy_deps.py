"""懒加载依赖 - 运行时按需安装和导入重依赖"""
import importlib
import subprocess
import sys
import re
from loguru import logger

# 懒加载白名单（仅允许这些包被按需安装）
LAZY_DEPS = {
    "paddleocr": {
        "packages": ["paddleocr", "paddlepaddle"],
        "description": "OCR 文字识别",
        "optional": True,
    },
    "httpx": {
        "packages": ["httpx"],
        "description": "HTTP 客户端（视频生成）",
        "optional": True,
    },
    "pillow": {
        "packages": ["Pillow"],
        "description": "图像处理",
        "optional": True,
    },
}

def _spec_is_safe(spec: str) -> bool:
    """验证依赖规格是否安全

    拒绝：URL、git+、文件路径、shell 元字符
    """
    if not spec or not spec.strip():
        return False
    # 拒绝 URL 和 git+
    if "://" in spec or spec.startswith("git+"):
        return False
    # 拒绝文件路径
    if "/" in spec or "\\" in spec:
        return False
    # 拒绝 shell 元字符
    if re.search(r'[;&|`$]', spec):
        return False
    # 仅允许字母、数字、连字符、下划线、点、方括号、比较运算符
    return re.match(r'^[a-zA-Z0-9_\-\.\[\]>=<~!]+$', spec)

def ensure(feature_name: str) -> bool:
    """确保依赖可用，不可用则按需安装

    Args:
        feature_name: LAZY_DEPS 中的特性名称

    Returns:
        True 如果依赖可用，False 如果安装失败
    """
    if feature_name not in LAZY_DEPS:
        logger.warning("lazy_deps.unknown_feature", feature=feature_name)
        return False

    feature = LAZY_DEPS[feature_name]
    packages = feature["packages"]

    # 检查是否已安装
    missing = []
    for pkg in packages:
        # 标准化包名（Pillow -> pillow）
        import_name = pkg.lower().replace("-", "_")
        try:
            importlib.import_module(import_name)
        except ImportError:
            missing.append(pkg)

    if not missing:
        return True

    # 按需安装
    for pkg in missing:
        if not _spec_is_safe(pkg):
            logger.error("lazy_deps.unsafe_spec", pkg=pkg)
            return False

    try:
        logger.info("lazy_deps.installing", feature=feature_name, packages=missing)
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet"] + missing,
            capture_output=True, text=True, timeout=120, check=False,
        )
        if result.returncode != 0:
            logger.error("lazy_deps.install_failed", packages=missing,
                        error=result.stderr[:200])
            return False
        logger.info("lazy_deps.installed", feature=feature_name, packages=missing)
        return True
    except Exception as e:
        logger.error("lazy_deps.install_error", feature=feature_name, error=str(e))
        return False

def is_available(feature_name: str) -> bool:
    """检查依赖是否已安装（不触发安装）"""
    if feature_name not in LAZY_DEPS:
        return False
    packages = LAZY_DEPS[feature_name]["packages"]
    for pkg in packages:
        import_name = pkg.lower().replace("-", "_")
        try:
            importlib.import_module(import_name)
        except ImportError:
            return False
    return True