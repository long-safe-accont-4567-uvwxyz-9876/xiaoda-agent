"""FreeModelBackend 统一重构契约测试。

背景：set_backend 模式原复制 13 处（4 份逐字节 + 7 薄包装 + 变体）。本次将
后端切换逻辑收敛到 utils/free_model_backend.FreeModelBackend：
    - set_backend 新增 local_model 参数（原由各包装者手动调 set_local_model）
    - 新增 set_free_model_client（WebUI 节点配置热更新）
    - 新增 disabled property（backend=off 语义，原各复制者用 _disabled）
    - __init__ 尊重 SILICONFLOW_BASE_URL env（distiller 历史行为）

契约：4 个完整复制者（instinct/error_rule/kg/distiller）委托后行为不变——
    off → 禁用；local → 备份 key 走本地；api/auto → 恢复 key 走免费。
"""

from utils.free_model_backend import FreeModelBackend

# ── FreeModelBackend 新契约 ───────────────────────────────────

class TestFreeModelBackendContract:
    def test_set_backend_accepts_local_model(self, monkeypatch):
        monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-test")
        backend = FreeModelBackend()
        backend.set_backend("local", local_model="local-chat")
        assert backend.backend == "local"
        assert backend._local_model == "local-chat"

    def test_set_backend_local_model_none_keeps_previous(self, monkeypatch):
        monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-test")
        backend = FreeModelBackend()
        backend.set_local_model("m1")
        backend.set_backend("api")  # 不带 local_model → 不清空
        assert backend._local_model == "m1"

    def test_set_free_model_client(self):
        backend = FreeModelBackend()
        backend.set_free_model_client(api_key="k", base_url="https://x/v1", model="m")
        assert backend._api_key == "k"
        assert backend._base_url == "https://x/v1"
        assert backend._model == "m"

    def test_disabled_property(self, monkeypatch):
        monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-test")
        backend = FreeModelBackend()
        assert not backend.disabled
        backend.set_backend("off")
        assert backend.disabled
        backend.set_backend("api")
        assert not backend.disabled

    def test_off_backs_up_key_and_api_unavailable(self, monkeypatch):
        monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-test")
        backend = FreeModelBackend()
        backend.set_backend("off")
        assert backend._api_key == ""      # off 后免费 key 被暂存
        assert not backend.api_available
        backend.set_backend("api")          # 恢复
        assert backend._api_key == "sk-test"
        assert backend.api_available

    def test_local_backs_up_key(self, monkeypatch):
        monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-test")
        backend = FreeModelBackend()
        backend.set_backend("local")
        assert backend._api_key == ""
        assert backend._backup_key == "sk-test"

    def test_base_url_respects_env(self, monkeypatch):
        monkeypatch.setenv("SILICONFLOW_BASE_URL", "https://custom.example/v1")
        backend = FreeModelBackend()
        assert backend._base_url == "https://custom.example/v1"

    def test_invalid_backend_ignored(self):
        backend = FreeModelBackend()
        backend.set_backend("bogus")
        assert backend.backend == "api"

    def test_auto_backend_alias_maps_to_api(self):
        # 历史值 auto 已取消：一律按 api（硅基流动免费模型）处理
        backend = FreeModelBackend(backend="auto")
        assert backend.backend == "api"
        backend.set_backend("auto")
        assert backend.backend == "api"


# ── 4 个完整复制者委托后行为不变 ──────────────────────────────

class TestDelegatingClassesContract:
    def test_instinct_manager_delegates(self):
        from instinct_manager import InstinctManager
        mgr = InstinctManager(db=None, router=None)
        assert isinstance(mgr._free, FreeModelBackend)  # 组合（非继承）持有共享后端
        assert InstinctManager.set_backend is not FreeModelBackend.set_backend

    def test_error_rule_pipeline_delegates(self):
        from tool_engine.error_rule_pipeline import ErrorRulePipeline
        p = ErrorRulePipeline(db=None, router=None)
        assert isinstance(p._free, FreeModelBackend)

    def test_knowledge_graph_delegates(self):
        from memory.knowledge_graph import KnowledgeGraph
        kg = KnowledgeGraph()
        assert isinstance(kg._free, FreeModelBackend)

    def test_memory_distiller_delegates(self):
        from memory.memory_distiller import MemoryDistiller
        d = MemoryDistiller(router=None)
        assert isinstance(d._free, FreeModelBackend)

    def test_instinct_off_disables_extraction(self):
        from instinct_manager import InstinctManager
        mgr = InstinctManager(db=None, router=None)
        mgr.set_backend("off")
        assert mgr._free.disabled
        assert mgr._free.api_available is False

    def test_instinct_local_sets_model(self):
        from instinct_manager import InstinctManager
        mgr = InstinctManager(db=None, router=None)
        mgr.set_backend("local", local_model="local-chat")
        assert mgr._free.backend == "local"
        assert mgr._free._local_model == "local-chat"

    def test_kg_off_disables_extraction(self):
        from memory.knowledge_graph import KnowledgeGraph
        kg = KnowledgeGraph()
        kg.set_backend("off")
        assert kg._free.disabled

    def test_kg_set_router_syncs_free(self):
        from memory.knowledge_graph import KnowledgeGraph
        kg = KnowledgeGraph()
        router = object()
        kg.set_router(router)
        assert kg._free._router is router

    def test_distiller_off_now_disables(self):
        """distiller 旧变体 off 不真正禁用（恢复 key），统一后 off 正确禁用。"""
        from memory.memory_distiller import MemoryDistiller
        d = MemoryDistiller(router=None)
        d.set_backend("off")
        assert d._free.disabled
