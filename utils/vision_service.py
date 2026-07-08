import os
import sys
import time
import logging
import subprocess
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import Any
from loguru import logger as _loguru_logger


logger = logging.getLogger("vision_service")


def _parse_env_gpu_index() -> int | None:
    """解析环境变量 XIAODA_GPU_INDEX，返回 None 表示未指定或无效。"""
    env_idx = os.environ.get("XIAODA_GPU_INDEX", "").strip()
    if not env_idx:
        return None
    try:
        idx = int(env_idx)
        logger.info(f"vulkan gpu index overridden by env XIAODA_GPU_INDEX={idx}")
        return idx
    except ValueError:
        logger.warning(f"invalid XIAODA_GPU_INDEX={env_idx!r}, ignoring")
        return None


def _get_gpu_names_from_ncnn(ncnn_module, gpu_count: int) -> list[str]:
    """通过 ncnn API 获取每个 GPU 设备的名称。失败时返回空列表。"""
    gpu_names: list[str] = []
    try:
        for i in range(gpu_count):
            info = ncnn_module.get_gpu_info(i)
            # ncnn 的 GpuInfo 有 name() 方法返回设备名
            name = ""
            if hasattr(info, "name"):
                name = info.name() or ""
            elif hasattr(info, "device_name"):
                name = info.device_name() or ""
            gpu_names.append(name)
    except Exception as e:
        logger.info(f"ncnn.get_gpu_info failed: {e}, falling back to system commands")
    return gpu_names


def _get_gpu_names_windows() -> list[str]:
    """通过 PowerShell 获取 Windows 显卡名称列表。"""
    try:
        result = subprocess.run(
            ["powershell", "-Command",
             "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return [n.strip() for n in result.stdout.strip().split("\n") if n.strip()]
    except Exception:
        return []


def _get_gpu_names_linux() -> list[str]:
    """通过 lspci 获取 Linux 显卡名称列表。"""
    gpu_names: list[str] = []
    try:
        result = subprocess.run(
            ["lspci"], capture_output=True, text=True, timeout=5, check=False,
        )
        for line in result.stdout.split("\n"):
            if "VGA compatible controller" in line or "3D controller" in line or "Display controller" in line:
                name = line.split(":")[-1].strip() if ":" in line else ""
                gpu_names.append(name)
    except Exception:
        _loguru_logger.debug("vision.gpu_lspci_failed", exc_info=True)
    return gpu_names


def _get_gpu_names_from_system() -> list[str]:
    """通过系统命令兜底获取 GPU 名称（顺序可能与 ncnn 不一致）。"""
    if sys.platform == "win32":
        return _get_gpu_names_windows()
    return _get_gpu_names_linux()


# 独显关键词：NVIDIA / AMD / Radeon / GeForce / Arc（Intel Arc 是独显）
# 核显关键词：Intel UHD / Iris / HD Graphics / AMD Radeon Graphics（集成的）
_DISCRETE_GPU_KEYWORDS = ("nvidia", "geforce", "radeon rx", "radeon r9", "radeon r7",
                          "arc a", "arc a370", "arc a380", "arc a580", "arc a750", "arc a770")
_INTEGRATED_GPU_KEYWORDS = ("intel(r) uhd", "intel(r) iris", "intel hd graphics",
                            "intel(r) hd graphics", "amd radeon graphics")


def _select_discrete_gpu_index(gpu_names: list[str], gpu_count: int) -> int:
    """根据 GPU 名称列表选择独显索引。优先独显，次选非核显，否则返回 0。"""
    logger.info(f"vulkan gpu list (count={gpu_count}): {gpu_names}")

    # 优先选择独显
    for i, name in enumerate(gpu_names):
        if i >= gpu_count:
            break
        name_lower = name.lower()
        if any(kw in name_lower for kw in _DISCRETE_GPU_KEYWORDS):
            if not any(kw in name_lower for kw in _INTEGRATED_GPU_KEYWORDS):
                logger.info(f"vulkan discrete gpu selected: device={i} name={name}")
                return i

    # 次优选择：非核显的设备
    for i, name in enumerate(gpu_names):
        if i >= gpu_count:
            break
        name_lower = name.lower()
        if not any(kw in name_lower for kw in _INTEGRATED_GPU_KEYWORDS):
            logger.info(f"vulkan non-integrated gpu selected: device={i} name={name}")
            return i

    logger.info("vulkan no discrete gpu found, using device 0")
    return 0


def _parse_env_gpu_index() -> int | None:
    """解析环境变量 XIAODA_GPU_INDEX，返回 None 表示未指定或无效。"""
    env_idx = os.environ.get("XIAODA_GPU_INDEX", "").strip()
    if not env_idx:
        return None
    try:
        idx = int(env_idx)
        logger.info(f"vulkan gpu index overridden by env XIAODA_GPU_INDEX={idx}")
        return idx
    except ValueError:
        logger.warning(f"invalid XIAODA_GPU_INDEX={env_idx!r}, ignoring")
        return None


def _get_gpu_names_from_ncnn(ncnn_module, gpu_count: int) -> list[str]:
    """通过 ncnn API 获取每个 GPU 设备的名称。失败时返回空列表。"""
    gpu_names: list[str] = []
    try:
        for i in range(gpu_count):
            info = ncnn_module.get_gpu_info(i)
            # ncnn 的 GpuInfo 有 name() 方法返回设备名
            name = ""
            if hasattr(info, "name"):
                name = info.name() or ""
            elif hasattr(info, "device_name"):
                name = info.device_name() or ""
            gpu_names.append(name)
    except Exception as e:
        logger.info(f"ncnn.get_gpu_info failed: {e}, falling back to system commands")
    return gpu_names


def _get_gpu_names_windows() -> list[str]:
    """通过 PowerShell 获取 Windows 显卡名称列表。"""
    try:
        result = subprocess.run(
            ["powershell", "-Command",
             "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return [n.strip() for n in result.stdout.strip().split("\n") if n.strip()]
    except Exception:
        return []


def _get_gpu_names_linux() -> list[str]:
    """通过 lspci 获取 Linux 显卡名称列表。"""
    gpu_names: list[str] = []
    try:
        result = subprocess.run(
            ["lspci"], capture_output=True, text=True, timeout=5, check=False,
        )
        for line in result.stdout.split("\n"):
            if "VGA compatible controller" in line or "3D controller" in line or "Display controller" in line:
                name = line.split(":")[-1].strip() if ":" in line else ""
                gpu_names.append(name)
    except Exception:
        pass
    return gpu_names


def _get_gpu_names_from_system() -> list[str]:
    """通过系统命令兜底获取 GPU 名称（顺序可能与 ncnn 不一致）。"""
    if sys.platform == "win32":
        return _get_gpu_names_windows()
    return _get_gpu_names_linux()


# 独显关键词：NVIDIA / AMD / Radeon / GeForce / Arc（Intel Arc 是独显）
# 核显关键词：Intel UHD / Iris / HD Graphics / AMD Radeon Graphics（集成的）
_DISCRETE_GPU_KEYWORDS = ("nvidia", "geforce", "radeon rx", "radeon r9", "radeon r7",
                          "arc a", "arc a370", "arc a380", "arc a580", "arc a750", "arc a770")
_INTEGRATED_GPU_KEYWORDS = ("intel(r) uhd", "intel(r) iris", "intel hd graphics",
                            "intel(r) hd graphics", "amd radeon graphics")


def _select_discrete_gpu_index(gpu_names: list[str], gpu_count: int) -> int:
    """根据 GPU 名称列表选择独显索引。优先独显，次选非核显，否则返回 0。"""
    logger.info(f"vulkan gpu list (count={gpu_count}): {gpu_names}")

    # 优先选择独显
    for i, name in enumerate(gpu_names):
        if i >= gpu_count:
            break
        name_lower = name.lower()
        if any(kw in name_lower for kw in _DISCRETE_GPU_KEYWORDS):
            if not any(kw in name_lower for kw in _INTEGRATED_GPU_KEYWORDS):
                logger.info(f"vulkan discrete gpu selected: device={i} name={name}")
                return i

    # 次优选择：非核显的设备
    for i, name in enumerate(gpu_names):
        if i >= gpu_count:
            break
        name_lower = name.lower()
        if not any(kw in name_lower for kw in _INTEGRATED_GPU_KEYWORDS):
            logger.info(f"vulkan non-integrated gpu selected: device={i} name={name}")
            return i

    logger.info("vulkan no discrete gpu found, using device 0")
    return 0


def _detect_discrete_gpu_index() -> int:
    """检测并返回独显的 Vulkan 设备索引。

    在双显卡系统（核显 + 独显）上，Windows 默认使用核显，
    导致 GPU 加速性能低下。本函数优先选择独显，没有独显时回退到设备 0。

    优先级：
    1. 环境变量 XIAODA_GPU_INDEX（用户手动指定，最高优先级）
    2. ncnn.get_gpu_info(i).name() 返回的设备名称匹配独显关键词
    3. 系统命令兜底（PowerShell/lspci，注意顺序可能与 ncnn 不一致）
    4. 默认返回 0

    Returns:
        独显的 Vulkan 设备索引，无独显时返回 0。
    """
    # 1. 环境变量手动指定（最高优先级）
    env_idx = _parse_env_gpu_index()
    if env_idx is not None:
        return env_idx

    try:
        import ncnn
        gpu_count = ncnn.get_gpu_count()
    except Exception:
        return 0

    if gpu_count <= 1:
        return 0

    # 2. 用 ncnn 自己的 API 获取每个设备的名称（顺序与设备索引一致）
    gpu_names = _get_gpu_names_from_ncnn(ncnn, gpu_count)

    # 3. 如果 ncnn API 失败，用系统命令兜底（注意：顺序可能与 ncnn 不一致）
    if not gpu_names or all(not n for n in gpu_names):
        gpu_names = _get_gpu_names_from_system()

    if not gpu_names:
        logger.info(f"vulkan multi-gpu detected (count={gpu_count}), but unable to identify devices, using device 0")
        return 0

    return _select_discrete_gpu_index(gpu_names, gpu_count)

# 模型目录（只读资源，可从 _MEIPASS 加载）
MODELS_DIR = Path(__file__).parent / "models"
# 捕获目录（可写，使用用户数据目录避免 _MEIPASS 只读）
try:
    from config import MEDIA_DIR
    CAPTURES_DIR = MEDIA_DIR / "captures"
    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
except ImportError:
    CAPTURES_DIR = Path(__file__).parent / "captures"

COCO_LABELS = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat",
    "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack",
    "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball",
    "kite", "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket",
    "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake",
    "chair", "couch", "potted plant", "bed", "dining table", "toilet", "tv", "laptop",
    "mouse", "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush"
]

CONFIDENCE_THRESHOLD = 0.25
NMS_THRESHOLD = 0.45
INPUT_SIZE = 640

HSV_COLOR_MAP = [
    ((0, 10), "红"), ((10, 25), "橙"), ((25, 35), "黄"),
    ((35, 78), "绿"), ((78, 100), "青"), ((100, 130), "蓝"),
    ((130, 170), "紫"), ((170, 180), "红"),
]

HSV_COLOR_HEX = {
    "红": "#FF0000", "橙": "#FF8C00", "黄": "#FFD700",
    "绿": "#008000", "青": "#00CED1", "蓝": "#0000FF",
    "紫": "#800080", "白": "#FFFFFF", "灰": "#808080", "黑": "#000000",
}


@dataclass
class Detection:
    """目标检测结果，包含标签、置信度和边界框坐标。"""
    label: str
    confidence: float
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass
class ColorInfo:
    """颜色分析结果，包含颜色名称、十六进制值和占比。"""
    color: str
    hex_value: str
    percentage: float


def _hsv_to_color_name(h: int, s: int, v: int) -> str:
    """根据 HSV 分量返回对应的中文颜色名称。"""
    if v < 46:
        return "黑"
    if s < 43:
        if v < 180:
            return "灰"
        return "白"
    for (lo, hi), name in HSV_COLOR_MAP:
        if lo <= h < hi:
            return name
    return "灰"


class VisionService:
    """视觉服务，封装摄像头捕获、目标检测和颜色分析功能。"""

    def __init__(self) -> None:
        """初始化视觉服务。"""
        self.model = None
        self._npu = None
        self.model_loaded = False
        self.backend = "none"
        CAPTURES_DIR.mkdir(parents=True, exist_ok=True)

    def _check_memory(self) -> bool:
        """检查系统可用内存是否充足（>500MB）。"""
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        available_kb = int(line.split()[1])
                        available_mb = available_kb / 1024
                        return available_mb > 500
            return False
        except Exception:
            return False

    def _load_model(self) -> None:
        """加载检测模型，按优先级尝试 NPU、NCNN 或回退到 API。"""
        if self.model_loaded:
            return
        if os.getenv("ENABLE_NPU", "").lower() in ("1", "true", "yes"):
            try:
                from .npu_inference import NPUInference
                if NPUInference.is_available():
                    model_path = str(MODELS_DIR / "yolov5.nb")
                    if os.path.exists(model_path):
                        self._npu = NPUInference(model_path=model_path)
                        self.model_loaded = True
                        self.backend = "npu"
                        logger.info(f"vision.npu_loaded model={model_path}")
                        return
                    logger.warning(f"vision.npu_model_not_found path={model_path}")
                else:
                    logger.warning("vision.npu_not_available")
            except Exception as e:
                logger.warning(f"vision.npu_init_failed error={e}")
        try:
            import ncnn
        except ImportError:
            logger.warning("ncnn not available, falling back to API")
            self.backend = "api_fallback"
            self.model_loaded = True
            return

        if not self._check_memory():
            logger.warning("insufficient memory (<500MB), refusing to load model")
            self.backend = "api_fallback"
            self.model_loaded = True
            return

        param_file = None
        bin_file = None

        yolov10_param = MODELS_DIR / "yolov10n.param"
        yolov10_bin = MODELS_DIR / "yolov10n.bin"
        if yolov10_param.exists() and yolov10_bin.exists():
            param_file = str(yolov10_param)
            bin_file = str(yolov10_bin)
        else:
            params = sorted(MODELS_DIR.glob("*.param"))
            bins = sorted(MODELS_DIR.glob("*.bin"))
            if params and bins:
                param_file = str(params[0])
                bin_file = str(bins[0])

        if not param_file or not bin_file:
            logger.warning("no model files found in %s, falling back to API", MODELS_DIR)
            self.backend = "api_fallback"
            self.model_loaded = True
            return

        try:
            net = ncnn.Net()
            try:
                net.opt.use_vulkan_compute = True
                # 双显卡系统优先选择独显（Windows 默认用核显导致卡顿）
                # 始终调用 set_vulkan_device，即使 index=0（独显可能就是设备 0）
                gpu_index = _detect_discrete_gpu_index()
                net.set_vulkan_device(gpu_index)
                logger.info(f"vulkan compute enabled, using gpu device={gpu_index}")
            except Exception as e:
                logger.warning(f"vulkan init failed, falling back to cpu: {e}")
            net.load_param(param_file)
            net.load_model(bin_file)
            self.model = net
            self.backend = "ncnn"
            self.model_loaded = True
            logger.info("ncnn model loaded from %s", param_file)
        except Exception as e:
            logger.warning("failed to load ncnn model: %s, falling back to API", e)
            self.backend = "api_fallback"
            self.model_loaded = True

    def _ensure_model(self) -> None:
        """确保检测模型已加载，未加载时触发加载流程。"""
        if not self.model_loaded:
            self._load_model()

    def capture_frame(self, device: Any=0, width: Any=640, height: Any=480) -> tuple:
        """从摄像头捕获一帧图像。"""
        try:
            import cv2
            cap = cv2.VideoCapture(f"/dev/video{device}")
            if not cap.isOpened():
                return (False, f"cannot open /dev/video{device}")
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            ret, frame = cap.read()
            cap.release()
            if not ret or frame is None:
                return (False, "failed to read frame from camera")
            return (True, frame)
        except ImportError:
            return (False, "opencv not available")
        except Exception as e:
            return (False, str(e))

    def save_frame(self, frame: Any, filename: Any=None) -> str:
        """将图像帧保存为 JPEG 文件，返回保存路径。"""
        try:
            import cv2
            CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
            if filename is None:
                filename = f"capture_{int(time.time())}.jpg"
            filepath = str(CAPTURES_DIR / filename)
            cv2.imwrite(filepath, frame)
            return filepath
        except Exception as e:
            logger.warning("failed to save frame: %s", e)
            return ""

    def _nms(self, detections: list) -> list:
        """对检测结果执行非极大值抑制。"""
        if not detections:
            return []
        detections.sort(key=lambda d: d.confidence, reverse=True)
        keep = []
        while detections:
            best = detections.pop(0)
            keep.append(best)
            detections = [d for d in detections if self._iou(best, d) < NMS_THRESHOLD]
        return keep

    @staticmethod
    def _iou(a: Detection, b: Detection) -> float:
        """计算两个检测框的交并比。"""
        ix1 = max(a.x1, b.x1)
        iy1 = max(a.y1, b.y1)
        ix2 = min(a.x2, b.x2)
        iy2 = min(a.y2, b.y2)
        iw = max(0, ix2 - ix1)
        ih = max(0, iy2 - iy1)
        inter = iw * ih
        area_a = max(0, a.x2 - a.x1) * max(0, a.y2 - a.y1)
        area_b = max(0, b.x2 - b.x1) * max(0, b.y2 - b.y1)
        union = area_a + area_b - inter
        if union <= 0:
            return 0.0
        return inter / union

    def detect_objects(self, frame: Any) -> list:
        """检测图像中的目标物体，返回 Detection 列表。"""
        self._ensure_model()
        if self.backend == "npu" and self._npu:
            try:
                results = self._npu.detect(frame)
                valid_results = []
                for r in results:
                    if isinstance(r, dict):
                        if r.get("x2", 0) > r.get("x1", 0) and r.get("y2", 0) > r.get("y1", 0):
                            valid_results.append(Detection(
                                label=r["label"],
                                confidence=r["confidence"],
                                x1=r["x1"],
                                y1=r["y1"],
                                x2=r["x2"],
                                y2=r["y2"],
                            ))
                    else:
                        valid_results.append(r)
                return valid_results
            except Exception as e:
                logger.warning(f"vision.npu_detect_failed error={e}")
                return []
        if self.backend == "ncnn":
            return self._detect_ncnn(frame)
        return []

    def _detect_ncnn(self, frame: Any) -> list:
        """使用 NCNN 模型执行目标检测。"""
        try:
            import ncnn
            import cv2

            orig_h, orig_w = frame.shape[:2]
            img = cv2.resize(frame, (INPUT_SIZE, INPUT_SIZE))
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            img = img.astype(np.float32) / 255.0
            img = img.transpose(2, 0, 1)

            mat_in = ncnn.Mat.from_numpy(img)

            ex = self.model.create_extractor()
            ex.input("images", mat_in)
            ret, mat_out = ex.extract("output0")
            if not ret or mat_out.empty():
                logger.warning("ncnn inference returned empty result")
                return []

            out = np.array(mat_out)
            if out.ndim == 3:
                out = out.transpose(1, 0)
            elif out.ndim == 2:
                pass
            else:
                logger.warning("unexpected ncnn output shape: %s", out.shape)
                return []

            detections = []
            num_classes = len(COCO_LABELS)

            for i in range(out.shape[0]):
                row = out[i]
                if len(row) < 4 + num_classes:
                    break
                class_scores = row[4:4 + num_classes]
                class_id = int(np.argmax(class_scores))
                confidence = float(class_scores[class_id])
                if confidence < CONFIDENCE_THRESHOLD:
                    continue
                cx = float(row[0])
                cy = float(row[1])
                w = float(row[2])
                h = float(row[3])
                x1 = (cx - w / 2) / INPUT_SIZE * orig_w
                y1 = (cy - h / 2) / INPUT_SIZE * orig_h
                x2 = (cx + w / 2) / INPUT_SIZE * orig_w
                y2 = (cy + h / 2) / INPUT_SIZE * orig_h
                label = COCO_LABELS[class_id] if class_id < num_classes else f"class_{class_id}"
                detections.append(Detection(label=label, confidence=confidence, x1=x1, y1=y1, x2=x2, y2=y2))

            return self._nms(detections)
        except Exception as e:
            logger.warning("ncnn detection failed: %s", e)
            return []

    def describe_scene(self, frame: Any) -> str:
        """生成图像中检测到的目标的文字描述。"""
        detections = self.detect_objects(frame)
        if not detections:
            return "画面中未检测到明确的目标物体"
        high_conf = [d for d in detections if d.confidence >= 0.5]
        if not high_conf:
            high_conf = detections[:5]
        counts = {}
        for d in high_conf:
            counts[d.label] = counts.get(d.label, 0) + 1
        parts = []
        for label, count in counts.items():
            parts.append(f"{count}个{label}" if count > 1 else f"1个{label}")
        return "画面中检测到" + "、".join(parts)

    def analyze_colors(self, frame: Any) -> list:
        """分析图像中的主要颜色及其占比。"""
        try:
            import cv2
            small = cv2.resize(frame, (50, 50))
            hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
            total_pixels = 50 * 50
            color_counts = {}
            for y in range(50):
                for x in range(50):
                    h, s, v = int(hsv[y, x, 0]), int(hsv[y, x, 1]), int(hsv[y, x, 2])
                    name = _hsv_to_color_name(h, s, v)
                    color_counts[name] = color_counts.get(name, 0) + 1
            results = []
            for name, count in sorted(color_counts.items(), key=lambda x: -x[1]):
                pct = round(count / total_pixels * 100, 1)
                if pct < 1.0:
                    continue
                results.append(ColorInfo(
                    color=name,
                    hex_value=HSV_COLOR_HEX.get(name, "#808080"),
                    percentage=pct,
                ))
            return results[:5]
        except Exception as e:
            logger.warning("color analysis failed: %s", e)
            return []

    def unload_model(self) -> None:
        """卸载已加载的检测模型，释放资源。"""
        if self._npu:
            self._npu = None
        self.model = None
        self.model_loaded = False
        self.backend = "none"
