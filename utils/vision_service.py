import os
import time
import logging
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

from tool_engine.tool_registry import ToolResult

logger = logging.getLogger("vision_service")

MODELS_DIR = Path(__file__).parent / "models"
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
    label: str
    confidence: float
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass
class ColorInfo:
    color: str
    hex_value: str
    percentage: float


def _hsv_to_color_name(h: int, s: int, v: int) -> str:
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
    def __init__(self):
        self.model = None
        self._npu = None
        self.model_loaded = False
        self.backend = "none"
        CAPTURES_DIR.mkdir(parents=True, exist_ok=True)

    def _check_memory(self) -> bool:
        try:
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        available_kb = int(line.split()[1])
                        available_mb = available_kb / 1024
                        return available_mb > 500
            return False
        except Exception:
            return False

    def _load_model(self):
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
                        logger.info("vision.npu_loaded", model=model_path)
                        return
                    else:
                        logger.warning("vision.npu_model_not_found", path=model_path)
                else:
                    logger.warning("vision.npu_not_available")
            except Exception as e:
                logger.warning("vision.npu_init_failed", error=str(e))
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
                logger.info("vulkan compute enabled for ncnn")
            except Exception:
                pass
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

    def _ensure_model(self):
        if not self.model_loaded:
            self._load_model()

    def capture_frame(self, device=0, width=640, height=480) -> tuple:
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

    def save_frame(self, frame, filename=None) -> str:
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

    def detect_objects(self, frame) -> list:
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
                logger.warning("vision.npu_detect_failed", error=str(e))
                return []
        if self.backend == "ncnn":
            return self._detect_ncnn(frame)
        return []

    def _detect_ncnn(self, frame) -> list:
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

    def describe_scene(self, frame) -> str:
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

    def analyze_colors(self, frame) -> list:
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

    def unload_model(self):
        if self._npu:
            self._npu = None
        self.model = None
        self.model_loaded = False
        self.backend = "none"
