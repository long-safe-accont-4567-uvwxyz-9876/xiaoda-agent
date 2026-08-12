import inspect
import re
from pathlib import Path

from local_ai.catalog.modelscope import ModelScopeRepository
from local_ai.models.registry import ModelRegistry
from local_ai.models.storage import StoragePolicy

ROOT = Path(__file__).resolve().parents[1]


def read_project_file(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_docs_exist():
    assert read_project_file("docs/local-ai-platform.md")


def test_docs_state_android_is_contract_only():
    docs = read_project_file("docs/local-ai-platform.md")
    assert "Android" in docs
    assert "不包含 Android 客户端" in docs


def test_docs_never_claim_fixed_vip_tops():
    docs = read_project_file("docs/local-ai-platform.md")
    assert "VIP9000 (3 TOPS" not in docs
    assert "3 TOPS INT8" not in docs


def test_docs_document_platform_providers():
    docs = read_project_file("docs/local-ai-platform.md")
    for provider in ("openai", "anthropic", "ollama", "custom-map"):
        assert provider in docs
    assert "填写配置 → 测试连接 → 保存" in docs
    assert "测试成功后才保存 Provider" in docs


def test_docs_document_modelscope_source_policy():
    docs = read_project_file("docs/local-ai-platform.md")
    assert "当前仅支持公开 ModelScope 仓库" in docs
    assert "不可变 `revision`" in docs
    assert "7–64 位十六进制" in docs
    assert "`main`、`master`、`latest` 等可变引用会被拒绝" in docs


def test_docs_document_storage_behavior():
    docs = read_project_file("docs/local-ai-platform.md")
    assert "下载前检查目录能否创建" in docs
    assert "默认目录在每次下载前都会重新校验" in docs
    assert "不会自动保存为默认目录" in docs


def test_docs_document_recovery():
    docs = read_project_file("docs/local-ai-platform.md")
    assert "HTTP Range 断点续传" in docs
    assert "暂停保留分片" in docs
    assert "取消时可选择保留分片或丢弃分片" in docs


def test_docs_document_npu_evidence():
    docs = read_project_file("docs/local-ai-platform.md")
    assert "NPU" in docs
    assert "页面只展示后端探测结果" in docs
    assert "不写死任何设备型号或 TOPS 数值" in docs
    assert "实际探测证据" in docs


def test_docs_are_complete_chinese_operator_and_user_guide():
    docs = read_project_file("docs/local-ai-platform.md")
    for section in (
        "## 适用范围",
        "## 上线前检查",
        "## 用户操作指南",
        "## Provider 接入",
        "## 模型存储",
        "## 下载与恢复",
        "## 运行时与路由",
        "## 日常运维",
        "## 故障排查",
        "## 安全与备份",
    ):
        assert section in docs


def test_docs_explain_all_six_web_ui_tabs_and_core_actions():
    docs = read_project_file("docs/local-ai-platform.md")
    for tab in ("部署", "模型广场", "已安装", "算力设备", "功能节点", "下载任务"):
        assert f"`{tab}`" in docs
    for action in ("重新扫描", "选择目录并下载", "暂停", "恢复", "取消", "启动", "停止", "移除"):
        assert action in docs


def test_docs_tabs_match_local_deploy_view():
    docs = read_project_file("docs/local-ai-platform.md")
    view = read_project_file("web/frontend/src/views/LocalDeployView.vue")
    view_tabs = set(re.findall(r'tab="([^"]+)"', view))
    assert view_tabs == {"部署", "模型广场", "已安装", "算力设备", "功能节点", "下载任务"}
    for tab in view_tabs:
        assert f"`{tab}`" in docs
    assert "模型市场" not in docs


def test_docs_explain_storage_validation_and_explicit_persistence(tmp_path, monkeypatch):
    docs = read_project_file("docs/local-ai-platform.md")
    picker = read_project_file("web/frontend/src/components/local-ai/StoragePickerDialog.vue")
    storage_api = read_project_file("web/routers/local_ai_storage.py")
    assert "保存为默认目录" in docs
    assert "不会自动保存为默认目录" in docs
    assert 'placeholder="手动输入服务器绝对路径"' in picker
    assert "Absolute or relative path to validate." in storage_api
    monkeypatch.chdir(tmp_path)
    validation = StoragePolicy(config_service=object()).validate_destination("models", 0)
    assert validation.path == str(tmp_path / "models")
    assert validation.writable
    for requirement in ("UI 提示输入服务器绝对路径", "API 仍会接受相对路径", "规范化为绝对路径", "可写", "可用空间", "/dev", "/proc", "/sys"):
        assert requirement in docs


def test_docs_explain_download_integrity_and_restart_recovery():
    docs = read_project_file("docs/local-ai-platform.md")
    assert "local_ai_downloads.json" in docs
    assert ".part" in docs
    assert ".quarantine" in docs
    assert "SHA-256" in docs
    assert "重启前处于下载中的任务会恢复为暂停状态" in docs


def test_docs_explain_runtime_combinations_and_no_silent_cloud_fallback():
    docs = read_project_file("docs/local-ai-platform.md")
    assert "ONNX Runtime GenAI" in docs
    assert "Embedding" in docs
    assert "Reranker" in docs
    assert "不会静默回退到云端" in docs


def test_docs_include_actionable_operations_and_troubleshooting():
    docs = read_project_file("docs/local-ai-platform.md")
    agent = read_project_file("agent.py")
    assert 'parser.add_argument("--web"' in agent
    assert "python agent.py --web" in docs
    assert "python agent.py serve" not in docs
    for command in ("python agent.py --web", "pytest tests/test_local_ai_docs_contract.py -q"):
        assert command in docs
    for symptom in ("本地 AI 资源加载失败", "目录不可写", "空间不足", "校验失败", "设备不可用", "实例启动失败"):
        assert symptom in docs


def test_readme_links_local_ai_operator_guide():
    readme = read_project_file("README.md")
    assert "[本地 AI 平台运维与用户指南](docs/local-ai-platform.md)" in readme


def test_modelscope_public_repository_contract_is_consistent_across_layers():
    env = read_project_file(".env.example")
    docs = read_project_file("docs/local-ai-platform.md")
    transport = read_project_file("local_ai/downloads/transport.py")
    installed_models = read_project_file("web/frontend/src/components/local-ai/InstalledModelsTab.vue")
    assert "MODELSCOPE_ACCESS_TOKEN=" not in env
    assert "MODELSCOPE_API_KEY=" not in env
    assert "模型目录与下载任务状态由 Web UI 管理" in env
    assert ModelScopeRepository._auth_headers(None) == {}
    assert "当前仅支持公开 ModelScope 仓库" in env
    assert "当前仅支持公开 ModelScope 仓库" in docs
    assert "Authorization" not in transport
    assert "仅移除安装登记，不会删除模型目录或文件" in installed_models
    assert "确认移除模型文件" not in installed_models


def test_docs_truthfully_explain_model_removal_only_unregisters_metadata():
    docs = read_project_file("docs/local-ai-platform.md")
    registry_remove = inspect.getsource(ModelRegistry.remove)
    assert "delete_if_mutable" in registry_remove
    assert "unlink" not in registry_remove
    assert "rmtree" not in registry_remove
    assert "移除只注销已安装模型登记，不会删除模型目录或文件" in docs
    assert "确认移除模型文件" not in docs
    assert "删除用户模型是破坏性操作" not in docs


def test_modelscope_support_and_removal_wording_is_locked_across_documentation():
    env = read_project_file(".env.example")
    readme = read_project_file("README.md")
    docs = read_project_file("docs/local-ai-platform.md")
    public_only = "当前仅支持公开 ModelScope 仓库，私有或受限仓库暂不支持"
    removal_core = "只注销已安装模型登记，不会删除模型目录或文件"
    assert public_only in env
    assert public_only in readme
    assert "当前仅支持公开 ModelScope 仓库" in docs
    assert removal_core in readme
    assert removal_core in docs
    for text in (env, readme, docs):
        assert "MODELSCOPE_ACCESS_TOKEN" not in text
        assert "MODELSCOPE_API_KEY" not in text
        assert "ModelScope Token" not in text
        assert "ModelScope token" not in text
