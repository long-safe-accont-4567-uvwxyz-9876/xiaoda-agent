"""懒加载依赖 - 运行时按需安装和导入重依赖"""
import importlib
import subprocess
import sys
import re
from typing import Any
from loguru import logger

# 懒加载白名单（仅允许这些包被按需安装）
# probe_attrs：契约探针——导入成功 ≠ 契约满足（同 rust_core 陈旧 .so 案例形态），
# 半损坏安装可过 importlib 检查；符号缺失即判不可用。支持点路径（PIL.Image）。
LAZY_DEPS = {
    "paddleocr": {
        "packages": ["paddleocr", "paddlepaddle"],
        "description": "OCR 文字识别",
        "optional": True,
        "probe_attrs": ("PaddleOCR",),
    },
    "httpx": {
        "packages": ["httpx"],
        "description": "HTTP 客户端（视频生成）",
        "optional": True,
        "probe_attrs": ("AsyncClient", "Timeout"),
    },
    "pillow": {
        "packages": ["Pillow"],
        "description": "图像处理",
        "optional": True,
        "probe_attrs": ("Image.open",),
    },
}

# pip 包名 → 导入名差异映射（未列出的按小写+下划线推导；
# 推导对 Pillow→pillow / paddlepaddle→paddlepaddle 均为错，必须显式映射）
_IMPORT_NAME_OVERRIDES: dict[str, str] = {
    "Pillow": "PIL",
    "paddlepaddle": "paddle",
}


def _import_name(pkg: str) -> str:
    return _IMPORT_NAME_OVERRIDES.get(pkg, pkg.lower().replace("-", "_"))


def _probe_ok(mod: Any, attr_path: str) -> bool:
    """校验模块上存在指定符号路径；中间层级不在属性上时尝试按子模块导入
    （PIL.Image 场景：Image 是子模块，不 import 就不在 PIL 包的属性上）。"""
    obj: Any = mod
    for part in attr_path.split("."):
        if hasattr(obj, part):
            obj = getattr(obj, part)
            continue
        parent_name = getattr(obj, "__name__", None)
        if not parent_name or not isinstance(parent_name, str):
            return False
        try:
            obj = importlib.import_module(f"{parent_name}.{part}")
        except ImportError:
            return False
    return True


def _check_feature(feature_name: str) -> tuple[list[str], list[str]]:
    """返回 (缺失包列表, 符号缺失列表)。符号探针只针对首个成功导入的主包。"""
    feature = LAZY_DEPS[feature_name]
    missing: list[str] = []
    primary_mod: Any = None
    for pkg in feature["packages"]:
        try:
            mod = importlib.import_module(_import_name(pkg))
        except ImportError:
            missing.append(pkg)
            continue
        if primary_mod is None:
            primary_mod = mod
    if missing or primary_mod is None:
        return missing, []
    broken = [attr for attr in feature.get("probe_attrs", ())
              if not _probe_ok(primary_mod, attr)]
    return [], broken

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

    # 检查是否已安装 + 符号契约是否满足
    missing, broken = _check_feature(feature_name)
    if not missing:
        if broken:
            # 半损坏安装：pip install 不会修复（already satisfied），如实报不可用
            logger.error("lazy_deps.symbol_missing", feature=feature_name,
                         attrs=broken)
            return False
        return True

    # 按需安装
    for pkg in missing:
        if not _spec_is_safe(pkg):
            logger.error("lazy_deps.unsafe_spec", pkg=pkg)
            return False

    try:
        logger.info("lazy_deps.installing", feature=feature_name, packages=missing)
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", *missing],
            capture_output=True, text=True, timeout=120, check=False,
        )
        if result.returncode != 0:
            logger.error("lazy_deps.install_failed", packages=missing,
                        error=result.stderr[:200])
            return False
        logger.info("lazy_deps.installed", feature=feature_name, packages=missing)
        # 安装后复检符号契约，防陈旧/半损坏版本蒙混过关
        _, broken = _check_feature(feature_name)
        if broken:
            logger.error("lazy_deps.post_install_probe_failed",
                         feature=feature_name, attrs=broken)
            return False
        return True
    except (subprocess.CalledProcessError, OSError, RuntimeError) as e:
        logger.error("lazy_deps.install_error", feature=feature_name, error=str(e))
        return False

def is_available(feature_name: str) -> bool:
    """检查依赖是否已安装且满足符号契约（不触发安装）"""
    if feature_name not in LAZY_DEPS:
        return False
    missing, broken = _check_feature(feature_name)
    return not missing and not broken