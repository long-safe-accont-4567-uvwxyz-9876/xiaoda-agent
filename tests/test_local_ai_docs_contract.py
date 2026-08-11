from pathlib import Path

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


def test_docs_document_modelscope_source_policy():
    docs = read_project_file("docs/local-ai-platform.md")
    assert "ModelScope" in docs
    assert "revision" in docs


def test_docs_document_storage_behavior():
    docs = read_project_file("docs/local-ai-platform.md")
    assert "存储" in docs
    assert "下载" in docs


def test_docs_document_recovery():
    docs = read_project_file("docs/local-ai-platform.md")
    assert "断点" in docs or "恢复" in docs


def test_docs_document_npu_evidence():
    docs = read_project_file("docs/local-ai-platform.md")
    assert "NPU" in docs
    assert "证据" in docs