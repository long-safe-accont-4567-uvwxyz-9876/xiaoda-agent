from typing import Any
import os
import ctypes
from pathlib import Path

import numpy as np
from loguru import logger

_vip_initialized = False

VIP_SUCCESS = 0
VIP_BUFFER_FORMAT_FP32 = 0
VIP_BUFFER_FORMAT_FP16 = 1
VIP_BUFFER_FORMAT_UINT8 = 2
VIP_BUFFER_FORMAT_INT8 = 3
VIP_BUFFER_QUANTIZE_NONE = 0
VIP_BUFFER_QUANTIZE_DYNAMIC_FIXED_POINT = 1
VIP_BUFFER_QUANTIZE_TF_ASYMM = 2
VIP_CREATE_NETWORK_FROM_FILE = 0x01
VIP_NETWORK_PROP_INPUT_COUNT = 1
VIP_NETWORK_PROP_OUTPUT_COUNT = 2
VIP_BUFFER_PROP_QUANT_FORMAT = 0
VIP_BUFFER_PROP_NUM_OF_DIMENSION = 1
VIP_BUFFER_PROP_SIZES_OF_DIMENSION = 2
VIP_BUFFER_PROP_DATA_FORMAT = 3
VIP_BUFFER_PROP_FIXED_POINT_POS = 4
VIP_BUFFER_PROP_TF_SCALE = 5
VIP_BUFFER_PROP_TF_ZERO_POINT = 6
VIP_BUFFER_OPER_TYPE_FLUSH = 1
VIP_BUFFER_OPER_TYPE_INVALIDATE = 2
VIP_BUFFER_MEMORY_TYPE_DEFAULT = 0

INPUT_SIZE = 640

DEFAULT_MODEL_PATH = str(Path(__file__).parent / "models" / "yolov5.nb")

YOLOV5_ANCHORS = [
    [(10, 13), (16, 30), (33, 23)],
    [(30, 61), (62, 45), (59, 119)],
    [(116, 90), (156, 198), (373, 326)],
]

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
    "toothbrush",
]


class _QuantDFP(ctypes.Structure):
    """动态定点量化参数结构体。"""
    _fields_ = [("fixed_point_pos", ctypes.c_int32)]


class _QuantAffine(ctypes.Structure):
    """仿射量化参数结构体，包含缩放因子和零点。"""
    _fields_ = [("scale", ctypes.c_float), ("zeroPoint", ctypes.c_int32)]


class _QuantData(ctypes.Union):
    """量化数据联合体，支持定点和仿射两种格式。"""
    _fields_ = [("dfp", _QuantDFP), ("affine", _QuantAffine)]  # noqa: RUF012


class vip_buffer_create_params_t(ctypes.Structure):
    """VIP 缓冲区创建参数结构体。"""
    _fields_ = [
        ("num_of_dims", ctypes.c_uint32),
        ("sizes", ctypes.c_uint32 * 6),
        ("data_format", ctypes.c_int32),
        ("quant_format", ctypes.c_int32),
        ("quant_data", _QuantData),
        ("memory_type", ctypes.c_uint32),
    ]


class VIPLite:
    """VIPLite NPU 推理库的 ctypes 封装。"""

    def __init__(self) -> None:
        """初始化 VIPLite 库封装。"""
        self._lib = None
        self._load_library()

    def _load_library(self) -> None:
        """加载 libNBGlinker.so 共享库。"""
        try:
            self._lib = ctypes.CDLL("/usr/lib/libNBGlinker.so")
            self._setup_argtypes()
        except OSError as e:
            logger.warning("failed to load libNBGlinker.so: {}", e)
            self._lib = None

    def _setup_argtypes(self) -> None:
        """配置所有 VIP C 函数的参数类型和返回类型。"""
        lib = self._lib
        lib.vip_init.argtypes = []
        lib.vip_init.restype = ctypes.c_int32
        lib.vip_destroy.argtypes = []
        lib.vip_destroy.restype = ctypes.c_int32
        lib.vip_get_version.argtypes = []
        lib.vip_get_version.restype = ctypes.c_uint32
        lib.vip_create_network.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        lib.vip_create_network.restype = ctypes.c_int32
        lib.vip_destroy_network.argtypes = [ctypes.c_void_p]
        lib.vip_destroy_network.restype = ctypes.c_int32
        lib.vip_prepare_network.argtypes = [ctypes.c_void_p]
        lib.vip_prepare_network.restype = ctypes.c_int32
        lib.vip_run_network.argtypes = [ctypes.c_void_p]
        lib.vip_run_network.restype = ctypes.c_int32
        lib.vip_finish_network.argtypes = [ctypes.c_void_p]
        lib.vip_finish_network.restype = ctypes.c_int32
        lib.vip_query_network.argtypes = [
            ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p,
        ]
        lib.vip_query_network.restype = ctypes.c_int32
        lib.vip_query_input.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32, ctypes.c_void_p,
        ]
        lib.vip_query_input.restype = ctypes.c_int32
        lib.vip_query_output.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32, ctypes.c_void_p,
        ]
        lib.vip_query_output.restype = ctypes.c_int32
        lib.vip_set_input.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
        ]
        lib.vip_set_input.restype = ctypes.c_int32
        lib.vip_set_output.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
        ]
        lib.vip_set_output.restype = ctypes.c_int32
        lib.vip_create_buffer.argtypes = [
            ctypes.POINTER(vip_buffer_create_params_t),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        lib.vip_create_buffer.restype = ctypes.c_int32
        lib.vip_map_buffer.argtypes = [ctypes.c_void_p]
        lib.vip_map_buffer.restype = ctypes.c_void_p
        lib.vip_unmap_buffer.argtypes = [ctypes.c_void_p]
        lib.vip_unmap_buffer.restype = ctypes.c_int32
        lib.vip_destroy_buffer.argtypes = [ctypes.c_void_p]
        lib.vip_destroy_buffer.restype = ctypes.c_int32
        lib.vip_get_buffer_size.argtypes = [ctypes.c_void_p]
        lib.vip_get_buffer_size.restype = ctypes.c_uint32
        lib.vip_flush_buffer.argtypes = [ctypes.c_void_p, ctypes.c_int32]
        lib.vip_flush_buffer.restype = ctypes.c_int32
        lib.vip_query_hardware.argtypes = [
            ctypes.c_int32, ctypes.c_uint32, ctypes.c_void_p,
        ]
        lib.vip_query_hardware.restype = ctypes.c_int32

    @property
    def available(self) -> Any:
        """检查 VIP Lite 库是否可用。"""
        return self._lib is not None

    def init(self) -> bool:
        """初始化 VIP 运行时环境，仅执行一次。"""
        global _vip_initialized
        if _vip_initialized:
            return True
        if not self.available:
            return False
        status = self._lib.vip_init()
        if status != VIP_SUCCESS:
            logger.warning("vip_init failed: {}", status)
            return False
        _vip_initialized = True
        logger.info("vip_init success")
        return True

    def destroy(self) -> None:
        """销毁 VIP 运行时环境。"""
        global _vip_initialized
        if not _vip_initialized or not self.available:
            return
        self._lib.vip_destroy()
        _vip_initialized = False

    def create_network(self, model_path: Any) -> Any:
        """从模型文件创建推理网络。"""
        if not self.available:
            return None
        network = ctypes.c_void_p()
        path_bytes = model_path.encode("utf-8") if isinstance(model_path, str) else model_path
        status = self._lib.vip_create_network(
            path_bytes, 0, VIP_CREATE_NETWORK_FROM_FILE, ctypes.byref(network),
        )
        if status != VIP_SUCCESS:
            logger.warning("vip_create_network failed: {}", status)
            return None
        return network

    def prepare_network(self, network: Any) -> bool:
        """准备推理网络，完成编译和资源分配。"""
        if not self.available or not network:
            return False
        status = self._lib.vip_prepare_network(network)
        if status != VIP_SUCCESS:
            logger.warning("vip_prepare_network failed: {}", status)
            return False
        return True

    def run_network(self, network: Any) -> bool:
        """执行推理网络的前向计算。"""
        if not self.available or not network:
            return False
        status = self._lib.vip_run_network(network)
        if status != VIP_SUCCESS:
            logger.warning("vip_run_network failed: {}", status)
            return False
        return True

    def finish_network(self, network: Any) -> None:
        """完成推理网络运行，释放临时资源。"""
        if not self.available or not network:
            return
        self._lib.vip_finish_network(network)

    def destroy_network(self, network: Any) -> None:
        """销毁推理网络并释放所有相关资源。"""
        if not self.available or not network:
            return
        self._lib.vip_destroy_network(network)

    def query_network_u32(self, network: Any, prop: Any) -> Any:
        """查询网络的 uint32 类型属性值。"""
        val = ctypes.c_uint32()
        self._lib.vip_query_network(network, prop, ctypes.byref(val))
        return val.value

    def query_input_u32(self, network: Any, index: Any, prop: Any) -> Any:
        """查询输入张量的 uint32 类型属性值。"""
        val = ctypes.c_uint32()
        self._lib.vip_query_input(network, index, prop, ctypes.byref(val))
        return val.value

    def query_input_float(self, network: Any, index: Any, prop: Any) -> Any:
        """查询输入张量的 float 类型属性值。"""
        val = ctypes.c_float()
        self._lib.vip_query_input(network, index, prop, ctypes.byref(val))
        return val.value

    def query_input_sizes(self, network: Any, index: Any) -> Any:
        """查询输入张量各维度的尺寸。"""
        sizes = (ctypes.c_uint32 * 6)()
        self._lib.vip_query_input(network, index, VIP_BUFFER_PROP_SIZES_OF_DIMENSION, sizes)
        return list(sizes)

    def query_output_u32(self, network: Any, index: Any, prop: Any) -> Any:
        """查询输出张量的 uint32 类型属性值。"""
        val = ctypes.c_uint32()
        self._lib.vip_query_output(network, index, prop, ctypes.byref(val))
        return val.value

    def query_output_float(self, network: Any, index: Any, prop: Any) -> Any:
        """查询输出张量的 float 类型属性值。"""
        val = ctypes.c_float()
        self._lib.vip_query_output(network, index, prop, ctypes.byref(val))
        return val.value

    def query_output_sizes(self, network: Any, index: Any) -> Any:
        """查询输出张量各维度的尺寸。"""
        sizes = (ctypes.c_uint32 * 6)()
        self._lib.vip_query_output(network, index, VIP_BUFFER_PROP_SIZES_OF_DIMENSION, sizes)
        return list(sizes)

    def create_buffer(self, params: Any) -> Any:
        """根据参数创建 VIP 缓冲区。"""
        if not self.available:
            return None
        buf = ctypes.c_void_p()
        status = self._lib.vip_create_buffer(
            ctypes.byref(params), ctypes.sizeof(params), ctypes.byref(buf),
        )
        if status != VIP_SUCCESS:
            logger.warning("vip_create_buffer failed: {}", status)
            return None
        return buf

    def map_buffer(self, buf: Any) -> Any:
        """映射缓冲区到用户空间并返回内存地址。"""
        if not self.available or not buf:
            return None
        return self._lib.vip_map_buffer(buf)

    def unmap_buffer(self, buf: Any) -> None:
        """取消缓冲区的用户空间映射。"""
        if not self.available or not buf:
            return
        self._lib.vip_unmap_buffer(buf)

    def destroy_buffer(self, buf: Any) -> None:
        """销毁 VIP 缓冲区并释放资源。"""
        if not self.available or not buf:
            return
        self._lib.vip_destroy_buffer(buf)

    def get_buffer_size(self, buf: Any) -> Any:
        """获取缓冲区的字节大小。"""
        if not self.available or not buf:
            return 0
        return self._lib.vip_get_buffer_size(buf)

    def flush_buffer(self, buf: Any, op_type: Any=VIP_BUFFER_OPER_TYPE_FLUSH) -> None:
        """刷新缓冲区，同步缓存与内存。"""
        if not self.available or not buf:
            return
        self._lib.vip_flush_buffer(buf, op_type)

    def set_input(self, network: Any, index: Any, buf: Any) -> bool:
        """将缓冲区绑定到网络的指定输入槽位。"""
        if not self.available or not network or not buf:
            return False
        status = self._lib.vip_set_input(network, index, buf)
        if status != VIP_SUCCESS:
            logger.warning("vip_set_input failed: {}", status)
            return False
        return True

    def set_output(self, network: Any, index: Any, buf: Any) -> bool:
        """将缓冲区绑定到网络的指定输出槽位。"""
        if not self.available or not network or not buf:
            return False
        status = self._lib.vip_set_output(network, index, buf)
        if status != VIP_SUCCESS:
            logger.warning("vip_set_output failed: {}", status)
            return False
        return True


class BufferInfo:
    """存储张量缓冲区的元数据信息。"""
    __slots__ = ("data_format", "fixed_point_pos", "num_dims", "quant_format", "scale", "sizes", "zero_point")

    def __init__(self) -> None:
        """初始化缓冲区信息，设置默认值。"""
        self.num_dims = 0
        self.sizes = []
        self.data_format = 0
        self.quant_format = 0
        self.scale = 1.0
        self.zero_point = 0
        self.fixed_point_pos = 0


class NPUModel:
    """NPU 模型加载与推理执行管理。"""

    def __init__(self, model_path: Any) -> None:
        """初始化 NPU 模型，加载并准备推理网络。"""
        self._vip = VIPLite()
        self._network = None
        self._input_buffers = []
        self._output_buffers = []
        self._input_infos = []
        self._output_infos = []
        self._loaded = False

        if not self._vip.available:
            logger.warning("VIP Lite library not available")
            return

        if not self._vip.init():
            logger.warning("VIP init failed")
            return

        self._network = self._vip.create_network(model_path)
        if not self._network:
            logger.warning("failed to create network from {}", model_path)
            return

        if not self._vip.prepare_network(self._network):
            logger.warning("failed to prepare network")
            return

        self._query_buffer_info()
        self._create_buffers()
        self._attach_buffers()
        self._loaded = True
        logger.info("NPU model loaded: {}", model_path)

    def _query_buffer_info(self) -> None:
        """查询网络所有输入输出张量的维度和量化参数。"""
        num_inputs = self._vip.query_network_u32(self._network, VIP_NETWORK_PROP_INPUT_COUNT)
        num_outputs = self._vip.query_network_u32(self._network, VIP_NETWORK_PROP_OUTPUT_COUNT)

        for i in range(num_inputs):
            info = BufferInfo()
            info.num_dims = self._vip.query_input_u32(self._network, i, VIP_BUFFER_PROP_NUM_OF_DIMENSION)
            info.sizes = self._vip.query_input_sizes(self._network, i)[:info.num_dims]
            info.data_format = self._vip.query_input_u32(self._network, i, VIP_BUFFER_PROP_DATA_FORMAT)
            info.quant_format = self._vip.query_input_u32(self._network, i, VIP_BUFFER_PROP_QUANT_FORMAT)
            if info.quant_format == VIP_BUFFER_QUANTIZE_TF_ASYMM:
                info.scale = self._vip.query_input_float(self._network, i, VIP_BUFFER_PROP_TF_SCALE)
                info.zero_point = self._vip.query_input_u32(self._network, i, VIP_BUFFER_PROP_TF_ZERO_POINT)
            elif info.quant_format == VIP_BUFFER_QUANTIZE_DYNAMIC_FIXED_POINT:
                info.fixed_point_pos = self._vip.query_input_u32(self._network, i, VIP_BUFFER_PROP_FIXED_POINT_POS)
            self._input_infos.append(info)

        for i in range(num_outputs):
            info = BufferInfo()
            info.num_dims = self._vip.query_output_u32(self._network, i, VIP_BUFFER_PROP_NUM_OF_DIMENSION)
            info.sizes = self._vip.query_output_sizes(self._network, i)[:info.num_dims]
            info.data_format = self._vip.query_output_u32(self._network, i, VIP_BUFFER_PROP_DATA_FORMAT)
            info.quant_format = self._vip.query_output_u32(self._network, i, VIP_BUFFER_PROP_QUANT_FORMAT)
            if info.quant_format == VIP_BUFFER_QUANTIZE_TF_ASYMM:
                info.scale = self._vip.query_output_float(self._network, i, VIP_BUFFER_PROP_TF_SCALE)
                info.zero_point = self._vip.query_output_u32(self._network, i, VIP_BUFFER_PROP_TF_ZERO_POINT)
            elif info.quant_format == VIP_BUFFER_QUANTIZE_DYNAMIC_FIXED_POINT:
                info.fixed_point_pos = self._vip.query_output_u32(self._network, i, VIP_BUFFER_PROP_FIXED_POINT_POS)
            self._output_infos.append(info)

    def _create_buffers(self) -> None:
        """根据查询到的张量信息创建输入输出缓冲区。"""
        for info in self._input_infos:
            params = vip_buffer_create_params_t()
            params.num_of_dims = info.num_dims
            for j, s in enumerate(info.sizes):
                params.sizes[j] = s
            params.data_format = info.data_format
            params.quant_format = info.quant_format
            if info.quant_format == VIP_BUFFER_QUANTIZE_TF_ASYMM:
                params.quant_data.affine.scale = info.scale
                params.quant_data.affine.zeroPoint = info.zero_point
            elif info.quant_format == VIP_BUFFER_QUANTIZE_DYNAMIC_FIXED_POINT:
                params.quant_data.dfp.fixed_point_pos = info.fixed_point_pos
            params.memory_type = VIP_BUFFER_MEMORY_TYPE_DEFAULT
            buf = self._vip.create_buffer(params)
            if buf:
                self._input_buffers.append(buf)
            else:
                logger.warning("failed to create input buffer {}", len(self._input_buffers))

        for info in self._output_infos:
            params = vip_buffer_create_params_t()
            params.num_of_dims = info.num_dims
            for j, s in enumerate(info.sizes):
                params.sizes[j] = s
            params.data_format = info.data_format
            params.quant_format = info.quant_format
            if info.quant_format == VIP_BUFFER_QUANTIZE_TF_ASYMM:
                params.quant_data.affine.scale = info.scale
                params.quant_data.affine.zeroPoint = info.zero_point
            elif info.quant_format == VIP_BUFFER_QUANTIZE_DYNAMIC_FIXED_POINT:
                params.quant_data.dfp.fixed_point_pos = info.fixed_point_pos
            params.memory_type = VIP_BUFFER_MEMORY_TYPE_DEFAULT
            buf = self._vip.create_buffer(params)
            if buf:
                self._output_buffers.append(buf)
            else:
                logger.warning("failed to create output buffer {}", len(self._output_buffers))

    def _attach_buffers(self) -> None:
        """将创建的缓冲区绑定到网络的输入输出槽位。"""
        for i, buf in enumerate(self._input_buffers):
            self._vip.set_input(self._network, i, buf)
        for i, buf in enumerate(self._output_buffers):
            self._vip.set_output(self._network, i, buf)

    @property
    def loaded(self) -> Any:
        """模型是否已成功加载。"""
        return self._loaded

    @property
    def input_infos(self) -> Any:
        """输入张量的元数据列表。"""
        return self._input_infos

    @property
    def output_infos(self) -> Any:
        """输出张量的元数据列表。"""
        return self._output_infos

    def run(self, input_data: bytes) -> list:
        """执行一次推理，写入输入数据并返回各输出缓冲区的字节内容。"""
        if not self._loaded:
            logger.warning("model not loaded")
            return []

        if not self._input_buffers:
            logger.warning("no input buffers")
            return []

        input_buf = self._input_buffers[0]
        mapped = self._vip.map_buffer(input_buf)
        if not mapped:
            logger.warning("failed to map input buffer")
            return []

        buf_size = self._vip.get_buffer_size(input_buf)
        write_size = min(len(input_data), buf_size)
        ctypes.memmove(mapped, input_data, write_size)
        self._vip.flush_buffer(input_buf, VIP_BUFFER_OPER_TYPE_FLUSH)

        if not self._vip.run_network(self._network):
            logger.warning("network run failed")
            return []

        results = []
        for buf in self._output_buffers:
            self._vip.flush_buffer(buf, VIP_BUFFER_OPER_TYPE_INVALIDATE)
            mapped_out = self._vip.map_buffer(buf)
            if not mapped_out:
                results.append(b"")
                continue
            out_size = self._vip.get_buffer_size(buf)
            out_data = (ctypes.c_uint8 * out_size)()
            ctypes.memmove(out_data, mapped_out, out_size)
            results.append(bytes(out_data))

        return results

    def close(self) -> None:
        """显式释放所有缓冲区和网络资源。

        推荐调用 close() 而非依赖 __del__，因为 __del__ 的调用时机不可控。
        """
        if not self._loaded:
            return
        self._loaded = False
        for buf in self._output_buffers:
            try:
                self._vip.destroy_buffer(buf)
            except Exception:
                logger.debug("npu.destroy_output_buffer_failed", exc_info=True)
        self._output_buffers.clear()
        for buf in self._input_buffers:
            try:
                self._vip.destroy_buffer(buf)
            except Exception:
                logger.debug("npu.destroy_input_buffer_failed", exc_info=True)
        self._input_buffers.clear()
        if self._network:
            try:
                self._vip.finish_network(self._network)
                self._vip.destroy_network(self._network)
            except Exception:
                logger.debug("npu.destroy_network_failed", exc_info=True)
            self._network = None

    def __del__(self) -> None:
        """析构时释放资源 (推荐使用 close() 显式释放)。"""
        self.close()


def _sigmoid(x: Any) -> Any:
    """计算 sigmoid 激活函数值。"""
    return 1.0 / (1.0 + np.exp(-np.clip(x, -500, 500)))


class YOLOv5PostProcessor:
    """YOLOv5 检测结果后处理器，解码锚框并执行 NMS 过滤。"""

    def __init__(self, conf_threshold: Any=0.45, nms_threshold: Any=0.45, max_detections: Any=100) -> None:
        """初始化后处理器参数。

        参数:
            conf_threshold: 置信度阈值，低于该值的检测框被丢弃。
            nms_threshold: NMS 的 IoU 阈值，用于合并重叠框。
            max_detections: 最终保留的最大检测框数量。
        """
        self.conf_threshold = conf_threshold
        self.nms_threshold = nms_threshold
        self.max_detections = max_detections
        self.labels = COCO_LABELS
        self.anchors = YOLOV5_ANCHORS

    def _iou(self, a: Any, b: Any) -> Any:
        """计算两个检测框的交并比。"""
        ix1 = max(a["x1"], b["x1"])
        iy1 = max(a["y1"], b["y1"])
        ix2 = min(a["x2"], b["x2"])
        iy2 = min(a["y2"], b["y2"])
        iw = max(0, ix2 - ix1)
        ih = max(0, iy2 - iy1)
        inter = iw * ih
        area_a = max(0, a["x2"] - a["x1"]) * max(0, a["y2"] - a["y1"])
        area_b = max(0, b["x2"] - b["x1"]) * max(0, b["y2"] - b["y1"])
        union = area_a + area_b - inter
        if union <= 0:
            return 0.0
        return inter / union

    def _nms(self, detections: Any) -> Any:
        """对检测结果执行非极大值抑制，去除重叠框。"""
        if not detections:
            return []
        detections.sort(key=lambda d: d["confidence"], reverse=True)
        detections = detections[:self.max_detections * 3]
        keep = []
        while detections:
            best = detections.pop(0)
            keep.append(best)
            if len(keep) >= self.max_detections:
                break
            detections = [d for d in detections if self._iou(best, d) < self.nms_threshold]
        return keep

    def process(self, outputs: list, input_shape: tuple = (INPUT_SIZE, INPUT_SIZE)) -> list:
        """处理多尺度输出，解码锚框并执行 NMS，返回最终检测结果。"""
        all_detections = []
        strides = [8, 16, 32]

        for scale_idx, output_data in enumerate(outputs):
            if not output_data or scale_idx >= len(strides):
                continue

            data = np.frombuffer(output_data, dtype=np.float32)
            stride = strides[scale_idx]
            grid_h = INPUT_SIZE // stride
            grid_w = INPUT_SIZE // stride

            expected_size = 3 * grid_h * grid_w * 85
            if data.size < expected_size:
                continue

            output = data.reshape(3, grid_h, grid_w, 85)
            anchors = self.anchors[scale_idx]

            gx, gy = np.meshgrid(np.arange(grid_w), np.arange(grid_h))
            gx = gx[:, :, np.newaxis].astype(np.float32)
            gy = gy[:, :, np.newaxis].astype(np.float32)

            for a_i in range(3):
                aw, ah = anchors[a_i]
                tx = output[a_i, :, :, 1]
                ty = output[a_i, :, :, 2]
                tw = output[a_i, :, :, 3]
                th = output[a_i, :, :, 4]
                obj_conf = _sigmoid(output[a_i, :, :, 0])
                class_scores = _sigmoid(output[a_i, :, :, 5:85])

                cx = ((_sigmoid(tx) * 2 - 0.5 + gx) * stride).squeeze(-1)
                cy = ((_sigmoid(ty) * 2 - 0.5 + gy) * stride).squeeze(-1)
                w = (_sigmoid(tw) * 2) ** 2 * aw
                h = (_sigmoid(th) * 2) ** 2 * ah

                max_class_score = np.max(class_scores, axis=-1)
                confidence = obj_conf * max_class_score

                mask = confidence > self.conf_threshold
                indices = np.argwhere(mask)

                for idx in indices:
                    gy_i, gx_i = int(idx[0]), int(idx[1])
                    det_cx = float(cx[gy_i, gx_i])
                    det_cy = float(cy[gy_i, gx_i])
                    det_w = float(w[gy_i, gx_i])
                    det_h = float(h[gy_i, gx_i])
                    if det_w < 1 or det_h < 1:
                        continue
                    det_conf = float(confidence[gy_i, gx_i])
                    cid = int(np.argmax(class_scores[gy_i, gx_i]))

                    x1 = det_cx - det_w / 2
                    y1 = det_cy - det_h / 2
                    x2 = det_cx + det_w / 2
                    y2 = det_cy + det_h / 2

                    label = self.labels[cid] if cid < len(self.labels) else f"class_{cid}"

                    all_detections.append({
                        "label": label,
                        "confidence": det_conf,
                        "x1": x1,
                        "y1": y1,
                        "x2": x2,
                        "y2": y2,
                    })

        return self._nms(all_detections)


class NPUInference:
    """NPU 推理接口，封装模型加载和目标检测流程。"""

    def __init__(self, model_path: Any=None) -> None:
        """初始化 NPU 推理引擎，加载模型并准备后处理器。"""
        self.available = False
        self._model = None
        self._postprocessor = YOLOv5PostProcessor(conf_threshold=0.15, nms_threshold=0.45, max_detections=100)

        if not self.is_available():
            logger.warning("NPU device not available (/dev/vipcore not found)")
            return

        if model_path is None:
            model_path = DEFAULT_MODEL_PATH

        if not os.path.isfile(model_path):
            logger.warning("model file not found: {}", model_path)
            return

        try:
            self._model = NPUModel(model_path)
            if not self._model.loaded:
                logger.warning("failed to load NPU model")
                return
            self.available = True
            logger.info("NPU inference ready")
        except Exception as e:
            logger.warning("NPU init failed: {}", e)
            self.available = False

    @staticmethod
    def is_available() -> bool:
        """检查 NPU 设备是否可用。"""
        return os.path.exists("/dev/vipcore")

    def detect(self, frame: Any) -> list:
        """对输入图像执行目标检测，返回归一化到原图尺寸的检测结果。"""
        if not self.available or not self._model:
            return []

        try:
            import cv2
        except ImportError:
            logger.warning("opencv not available for preprocessing")
            return []

        try:
            orig_h, orig_w = frame.shape[:2]
            img = cv2.resize(frame, (INPUT_SIZE, INPUT_SIZE))
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

            input_info = self._model.input_infos[0] if self._model.input_infos else None
            if input_info and input_info.data_format == VIP_BUFFER_FORMAT_UINT8:
                input_bytes = img.astype(np.uint8).tobytes()
            else:
                img_float = img.astype(np.float32) / 255.0
                img_float = img_float.transpose(2, 0, 1)
                input_bytes = img_float.tobytes()

            outputs = self._model.run(input_bytes)
            if not outputs:
                return []

            detections = self._postprocessor.process(outputs, (INPUT_SIZE, INPUT_SIZE))

            scale_x = orig_w / INPUT_SIZE
            scale_y = orig_h / INPUT_SIZE
            for det in detections:
                det["x1"] = max(0, det["x1"] * scale_x)
                det["y1"] = max(0, det["y1"] * scale_y)
                det["x2"] = min(orig_w, det["x2"] * scale_x)
                det["y2"] = min(orig_h, det["y2"] * scale_y)

            return detections
        except Exception as e:
            logger.warning("NPU detection failed: {}", e)
            return []
