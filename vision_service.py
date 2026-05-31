import os
import asyncio
import time
import base64
from pathlib import Path
from typing import Optional

import numpy as np

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False

from loguru import logger
from config import load_config


class VisionService:

    def __init__(self, config: dict = None):
        if config is None:
            config = load_config()
        self._config = config
        self._camera_index = config.get("camera_index", 0)
        self._capture_width = config.get("capture_width", 640)
        self._capture_height = config.get("capture_height", 480)
        self._snapshot_dir = Path(config.get("snapshot_dir", "snapshots"))
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)

        self._capture = None
        self._available = False
        self._last_frame = None
        self._last_capture_time = 0

    async def init(self):
        if not CV2_AVAILABLE:
            logger.warning("vision.cv2_not_available")
            return

        try:
            self._capture = cv2.VideoCapture(self._camera_index)
            if not self._capture.isOpened():
                logger.warning("vision.camera_not_found", index=self._camera_index)
                return

            self._capture.set(cv2.CAP_PROP_FRAME_WIDTH, self._capture_width)
            self._capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self._capture_height)

            ret, frame = self._capture.read()
            if ret and frame is not None:
                self._available = True
                self._last_frame = frame
                logger.info("vision.ready", index=self._camera_index, shape=frame.shape)
            else:
                logger.warning("vision.capture_test_failed")
                self._capture.release()
                self._capture = None

        except Exception as e:
            logger.error("vision.init_failed", error=str(e))
            if self._capture:
                self._capture.release()
                self._capture = None

    async def capture(self, save: bool = True) -> Optional[str]:
        if not self._available or not self._capture:
            return None

        try:
            ret, frame = self._capture.read()
            if not ret or frame is None:
                logger.warning("vision.capture_failed")
                return None

            self._last_frame = frame
            self._last_capture_time = time.time()

            if save:
                timestamp = int(time.time())
                filename = f"capture_{timestamp}.jpg"
                filepath = self._snapshot_dir / filename
                cv2.imwrite(str(filepath), frame)
                logger.info("vision.saved", path=str(filepath))
                return str(filepath)

            return None

        except Exception as e:
            logger.error("vision.capture_error", error=str(e))
            return None

    async def get_frame(self) -> Optional[np.ndarray]:
        if not self._available or not self._capture:
            return None

        try:
            ret, frame = self._capture.read()
            if ret and frame is not None:
                self._last_frame = frame
                return frame
            return self._last_frame
        except Exception:
            return self._last_frame

    async def detect_objects(self, image: np.ndarray = None) -> list[dict]:
        if image is None:
            image = await self.get_frame()
        if image is None:
            return []

        try:
            from npu_inference import NPUInference
            npu = NPUInference()
            if await npu.init():
                results = await npu.detect(image)
                await npu.close()
                return results
        except ImportError:
            pass
        except Exception as e:
            logger.warning("vision.detect_failed", error=str(e))

        return []

    async def describe_image(self, image_path: str, router=None) -> str:
        if not os.path.exists(image_path):
            return "图片不存在"

        try:
            with open(image_path, "rb") as f:
                img_data = f.read()
            img_base64 = base64.b64encode(img_data).decode("utf-8")

            ext = Path(image_path).suffix.lower()
            mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif"}.get(ext, "image/jpeg")

            if router:
                messages = [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "请描述这张图片的内容："},
                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_base64}"}},
                        ],
                    }
                ]
                result = await router.route("vision", messages)
                if isinstance(result, str):
                    return result
                return (result.choices[0].message.content or "").strip()

            return f"图片已加载: {Path(image_path).name} ({len(img_data)} bytes)"

        except Exception as e:
            logger.error("vision.describe_failed", error=str(e))
            return f"图片描述失败: {e}"

    async def capture_and_describe(self, router=None) -> dict:
        path = await self.capture(save=True)
        if not path:
            return {"success": False, "error": "拍照失败"}

        description = await self.describe_image(path, router=router)
        return {
            "success": True,
            "path": path,
            "description": description,
        }

    async def get_status(self) -> dict:
        return {
            "available": self._available,
            "camera_index": self._camera_index,
            "resolution": f"{self._capture_width}x{self._capture_height}",
            "cv2_available": CV2_AVAILABLE,
            "snapshot_dir": str(self._snapshot_dir),
        }

    async def close(self):
        if self._capture:
            self._capture.release()
            self._capture = None
        self._available = False
        logger.info("vision.closed")
