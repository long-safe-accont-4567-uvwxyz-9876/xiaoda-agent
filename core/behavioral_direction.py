# core/behavioral_direction.py
"""
行为方向向量 — 对齐 RepE 的 contrast vector 和 reprobe 的 Probe.get_direction()。

在 API-only 约束下，方向向量不作用于模型激活，而作用于 Agent 的上下文空间。

参考:
- repe/rep_readers.py: PCARepReader.get_rep_directions() — PCA方向识别
- reprobe/probe.py: Probe.get_direction() — 归一化线性探针方向
- repe/rep_control_reading_vec.py: WrappedBlock.set_controller() — 方向注入
- reprobe/steerer.py: Steerer._apply_projection() — 投影干预
"""
from dataclasses import dataclass, field
import json
from pathlib import Path
from loguru import logger


@dataclass
class DirectionVector:
    """
    行为方向向量 — 对齐 RepE 的 contrast vector 和 reprobe 的 Probe.get_direction()。
    """
    name: str
    dimensions: dict[str, float]
    source: str = ""
    magnitude: float = 1.0
    meta: dict = field(default_factory=dict)

    def __mul__(self, scalar: float) -> "DirectionVector":
        """缩放方向 — 对齐 Steerer 的 alpha 参数"""
        return DirectionVector(
            name=self.name,
            dimensions={k: v * scalar for k, v in self.dimensions.items()},
            source=self.source,
            magnitude=self.magnitude * scalar,
            meta=self.meta,
        )

    def __add__(self, other: "DirectionVector") -> "DirectionVector":
        """方向叠加 — 对齐 WrappedBlock 的 linear_comb 算子"""
        merged = dict(self.dimensions)
        for k, v in other.dimensions.items():
            merged[k] = merged.get(k, 0.0) + v
        return DirectionVector(
            name=f"{self.name}+{other.name}",
            dimensions=merged,
            magnitude=1.0,
            meta={**self.meta, **other.meta},
        )

    def apply_to_context(self, context: dict) -> dict:
        """
        将方向向量应用到 Agent 上下文。

        对齐:
        - repe/rep_control_reading_vec.py: WrappedBlock.forward()
        - reprobe/steerer.py: Steerer._apply_projection()

        在 API-only 下，"hidden" 是 context dict 而非激活张量。
        """
        result = dict(context)
        for dim, weight in self.dimensions.items():
            if dim == "prompt":
                result["prompt_modifier"] = result.get("prompt_modifier", 0.0) + weight
            elif dim == "tool":
                result["tool_bias"] = result.get("tool_bias", 0.0) + weight
            elif dim == "emotion":
                current = result.get("emotion_offset", 0.0)
                result["emotion_offset"] = current + weight
            elif dim == "route":
                result["route_bias"] = result.get("route_bias", 0.0) + weight
        return result

    def save(self, path: str) -> None:
        """持久化 — 对齐 reprobe/probe.py: Probe.save()"""
        data = {
            "name": self.name,
            "dimensions": self.dimensions,
            "source": self.source,
            "magnitude": self.magnitude,
            "meta": self.meta,
        }
        Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False))

    @classmethod
    def load(cls, path: str) -> "DirectionVector":
        """加载 — 对齐 reprobe/loader.py: ProbeLoader.from_file()"""
        data = json.loads(Path(path).read_text())
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class DirectionRegistry:
    """
    方向向量注册表 — 对齐 reprobe/loader.py: ProbeLoader + SAELens 的 pretrained_saes_directory。
    """

    def __init__(self, storage_path: str = ""):
        self._directions: dict[str, DirectionVector] = {}
        self._storage_path = storage_path
        if storage_path:
            self._load_from_storage()

    def register(self, direction: DirectionVector) -> None:
        self._directions[direction.name] = direction
        if self._storage_path:
            self._save_to_storage()

    def get(self, name: str) -> DirectionVector | None:
        return self._directions.get(name)

    def list_directions(self) -> list[str]:
        return list(self._directions.keys())

    def _save_to_storage(self) -> None:
        """对齐 reprobe/store.py: ActivationStore 的持久化模式"""
        try:
            registry = {
                name: {"dimensions": d.dimensions, "source": d.source,
                       "magnitude": d.magnitude, "meta": d.meta}
                for name, d in self._directions.items()
            }
            Path(self._storage_path).parent.mkdir(parents=True, exist_ok=True)
            Path(self._storage_path).write_text(
                json.dumps(registry, indent=2, ensure_ascii=False))
        except Exception as e:
            logger.error(f"direction_registry.save_failed: {e}")

    def _load_from_storage(self) -> None:
        path = Path(self._storage_path)
        if not path.exists():
            return
        try:
            registry = json.loads(path.read_text())
            for name, data in registry.items():
                self._directions[name] = DirectionVector(
                    name=name, dimensions=data["dimensions"],
                    source=data.get("source", ""),
                    magnitude=data.get("magnitude", 1.0),
                    meta=data.get("meta", {}),
                )
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"direction_registry.load_failed_corrupted: {e}")
            # 损坏时返回空注册表，不崩溃
