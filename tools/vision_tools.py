import subprocess
import time
import os
from pathlib import Path
from tool_registry import register_tool, ToolPermission, ToolResult

def _capture_usb_camera(timeout_sec=10, width=1280, height=960) -> ToolResult:
    try:
        import cv2
    except ImportError:
        return ToolResult.fail("OpenCV 未安装，请运行: pip install opencv-python-headless")
    save_dir = os.path.expanduser("~/nahida-agent/cam")
    os.makedirs(save_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    save_path = os.path.join(save_dir, f"capture_{ts}.jpg")
    cap = cv2.VideoCapture(0, cv2.CAP_V4L2)
    if not cap.isOpened():
        return ToolResult.fail("无法打开摄像头 /dev/video0。检查: ls -la /dev/video*")
    try:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        warmup_start = time.time()
        while time.time() - warmup_start < 2.0:
            cap.grab()
            time.sleep(0.05)
        best_frame = None
        best_brightness = -1
        attempts = 8
        for i in range(attempts):
            ret, frame = cap.read()
            if not ret or frame is None:
                time.sleep(0.2)
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            brightness = gray.mean()
            sharpness = cv2.Laplacian(gray, cv2.CV_64F).var()
            if brightness > 50 and sharpness > best_brightness:
                best_brightness = sharpness
                best_frame = frame.copy()
            time.sleep(0.15)
        if best_frame is None:
            cap.release()
            cap = cv2.VideoCapture(0, cv2.CAP_V4L2)
            if not cap.isOpened():
                return ToolResult.fail("重新打开摄像头失败")
            time.sleep(1.5)
            for _ in range(10):
                cap.grab()
            ret, best_frame = cap.read()
            if not ret or best_frame is None:
                return ToolResult.fail("多次重试仍无法捕获画面")
        encode_params = [cv2.IMWRITE_JPEG_QUALITY, 95]
        success = cv2.imwrite(save_path, best_frame, encode_params)
        if not success:
            return ToolResult.fail("图片保存失败")
        gray = cv2.cvtColor(best_frame, cv2.COLOR_BGR2GRAY)
        sharpness = cv2.Laplacian(gray, cv2.CV_64F).var()
        brightness = gray.mean()
        import base64
        _, buffer = cv2.imencode(".jpg", best_frame, encode_params)
        img_base64 = base64.b64encode(buffer).decode('utf-8')
        result = ToolResult.ok(f"📸 USB摄像头拍照成功！已保存到: {save_path} ({best_frame.shape[1]}x{best_frame.shape[0]}, 清晰度={sharpness:.0f}, 亮度={brightness:.0f})")
        result.data = {"image_base64": img_base64, "image_path": save_path}
        return result
    except Exception as e:
        return ToolResult.fail(f"拍照失败: {str(e)}")
    finally:
        cap.release()

def _capture_mipi_camera(timeout_sec=10) -> ToolResult:
    save_dir = os.path.expanduser("~/nahida-agent/cam")
    os.makedirs(save_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    save_path = os.path.join(save_dir, f"capture_{ts}.jpg")
    try:
        cmd = ["libcamera-still", "-o", save_path, "--width", "1280", "--height", "960", "--quality", "95", "--timeout", str(int(timeout_sec * 1000)), "--nopreview", "--immediate"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec + 5)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "no cameras available" in stderr or "No such device" in stderr:
                return ToolResult.fail("未检测到 MIPI 摄像头。检查排线连接: vcgencmd get_camera")
            return ToolResult.fail(f"MIPI 拍照失败: {stderr}")
        if not os.path.exists(save_path) or os.path.getsize(save_path) < 1000:
            return ToolResult.fail(f"拍照文件异常: {save_path}")
        import base64
        with open(save_path, "rb") as f:
            img_base64 = base64.b64encode(f.read()).decode('utf-8')
        result = ToolResult.ok(f"📸 MIPI摄像头拍照成功！已保存到: {save_path}")
        result.data = {"image_base64": img_base64, "image_path": save_path}
        return result
    except FileNotFoundError:
        return ToolResult.fail("libcamera-still 未安装。安装: sudo apt install libcamera-apps-lite")
    except subprocess.TimeoutExpired:
        return ToolResult.fail(f"拍照超时（{timeout_sec}秒）")

@register_tool(
    name="camera_capture",
    description="使用摄像头拍照。优先尝试USB摄像头，失败时回退到MIPI摄像头。",
    schema={
        "type": "object",
        "properties": {
            "timeout": {"type": "integer", "description": "拍照超时秒数", "default": 10}
        },
        "required": [],
    },
    permission=ToolPermission.READ_ONLY,
    category="hardware",
    max_frequency=10,
)
def camera_capture(timeout: int = 10) -> ToolResult:
    result = _capture_usb_camera(timeout_sec=timeout)
    if result.success:
        return result
    if "未安装" in result.error:
        return result
    mipi_result = _capture_mipi_camera(timeout_sec=timeout)
    if mipi_result.success:
        return mipi_result
    return ToolResult.fail(f"USB摄像头: {result.error}\nMIPI摄像头: {mipi_result.error}")

@register_tool(
    name="vision_analyze",
    description="视觉分析工具。分析图片内容，返回AI描述。支持本地文件路径和URL。",
    schema={
        "type": "object",
        "properties": {
            "image": {"type": "string", "description": "图片路径或URL"},
            "question": {"type": "string", "description": "可选的分析问题", "default": "描述图片内容"}
        },
        "required": ["image"],
    },
    permission=ToolPermission.READ_ONLY,
    category="vision",
    max_frequency=10,
)
def vision_analyze(image: str, question: str = "描述图片内容") -> ToolResult:
    try:
        from vision_service import VisionService
        service = VisionService()
        result = service.analyze_image(image, question)
        return ToolResult.ok(result)
    except Exception as e:
        return ToolResult.fail(f"视觉分析失败: {str(e)}")