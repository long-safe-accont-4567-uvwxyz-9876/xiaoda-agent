"""tests/test_local_ai_modelscope.py — ModelScope Repository Adapter tests.

Covers:
- Revision validation (reject main/master/latest/non-hex)
- File listing with pagination (TotalPages + partial-page fallback)
- Auth header (Bearer) when token provided
- Missing auth token for private repo → clear error
- SSRF protection (block private/loopback/link-local IP literals)
- ORT GenAI chat layout recognition (genai_config.json)
- Embedding layout recognition (config.json architectures / sentence-transformers markers)
- Reranker layout recognition (config.json ForSequenceClassification)
- Unknown layout → requires_configuration
- Purpose NOT guessed from repository name
"""
from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import httpx
import pytest

from local_ai.catalog.modelscope import (
    CatalogInspection,
    InvalidRevisionError,
    ModelScopeRepository,
    RemoteFile,
)
from local_ai.contracts import ModelPurpose

# ── helpers ──

def _files_response(files: list[dict], total_pages: int | None = None) -> httpx.Response:
    data: dict[str, object] = {"Files": files}
    if total_pages is not None:
        data["TotalPages"] = total_pages
    return httpx.Response(200, json={"Code": 200, "Data": data})


def _file_entry(path: str, size: int = 100, sha256: str | None = None) -> dict:
    return {"Path": path, "Size": size, "Sha256": sha256}


def _config_content_response(config: dict) -> httpx.Response:
    """Raw file content endpoint returns the file bytes directly."""
    return httpx.Response(200, content=json.dumps(config).encode("utf-8"))


def _make_repo(
    handler,
    base_url: str = "https://www.modelscope.cn/api/v1/",
) -> ModelScopeRepository:
    transport = httpx.MockTransport(handler)
    return ModelScopeRepository(base_url=base_url, transport=transport)


def _listing_handler(files: list[dict], config: dict | None = None):
    """Build a handler that serves a file listing and optional config.json content."""

    def handler(request: httpx.Request) -> httpx.Response:
        file_path = request.url.params.get("FilePath")
        if file_path is not None and config is not None:
            return _config_content_response(config)
        return _files_response(files)

    return handler


# ── fixtures ──

@pytest.fixture
def mock_transport() -> httpx.MockTransport:
    """Default MockTransport: returns a single onnx file (unknown layout)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _files_response([_file_entry("model.onnx", size=100, sha256="a" * 64)])

    return httpx.MockTransport(handler)


@pytest.fixture
def repo(mock_transport: httpx.MockTransport) -> ModelScopeRepository:
    return ModelScopeRepository(transport=mock_transport)


# ── RemoteFile dataclass ──

def test_remote_file_is_frozen_and_validates_fields():
    f = RemoteFile(path="model.onnx", size=100, sha256="a" * 64)
    assert f.path == "model.onnx"
    assert f.size == 100
    assert f.sha256 == "a" * 64
    with pytest.raises(FrozenInstanceError):
        f.size = 0  # type: ignore[misc]


def test_remote_file_accepts_optional_sha256():
    f = RemoteFile(path="model.onnx", size=100, sha256=None)
    assert f.sha256 is None


def test_remote_file_rejects_empty_path():
    with pytest.raises(ValueError):
        RemoteFile(path="", size=100, sha256=None)


def test_remote_file_rejects_negative_size():
    with pytest.raises(ValueError):
        RemoteFile(path="model.onnx", size=-1, sha256=None)


def test_remote_file_rejects_short_sha256():
    with pytest.raises(ValueError):
        RemoteFile(path="model.onnx", size=100, sha256="abc")


# ── CatalogInspection dataclass ──

def test_catalog_inspection_is_frozen():
    inspection = CatalogInspection(
        repository="owner/model",
        revision="abc1234",
        files=(RemoteFile(path="model.onnx", size=100, sha256=None),),
        purpose=None,
        runnable=False,
        state="requires_configuration",
        evidence={"layout": "unknown"},
        missing=("config.json",),
    )
    with pytest.raises(FrozenInstanceError):
        inspection.runnable = True  # type: ignore[misc]


def test_catalog_inspection_rejects_invalid_state():
    with pytest.raises(ValueError):
        CatalogInspection(
            repository="owner/model",
            revision="abc1234",
            files=(),
            purpose=None,
            runnable=False,
            state="bogus",
            evidence={},
            missing=(),
        )


# ── InvalidRevisionError ──

def test_invalid_revision_error_is_value_error():
    assert issubclass(InvalidRevisionError, ValueError)


# ── Revision validation ──

@pytest.mark.parametrize(
    "revision",
    ["main", "master", "latest", "HEAD", "v1.0", "abc123g", "tag-name", ""],
)
@pytest.mark.asyncio
async def test_list_files_rejects_mutable_or_non_hex_revisions(revision: str):
    repo = _make_repo(lambda req: _files_response([]))
    with pytest.raises(InvalidRevisionError):
        await repo.list_files("owner/model", revision, None)


@pytest.mark.parametrize("revision", ["main", "master", "latest"])
@pytest.mark.asyncio
async def test_inspect_rejects_mutable_revisions(revision: str, repo: ModelScopeRepository):
    with pytest.raises(InvalidRevisionError):
        await repo.inspect("owner/model", revision, None)


@pytest.mark.parametrize("revision", ["abcdef0", "a" * 64, "ABCDEF0", "deadbeef"])
@pytest.mark.asyncio
async def test_list_files_accepts_immutable_hex_revisions(revision: str):
    repo = _make_repo(lambda req: _files_response([]))
    files = await repo.list_files("owner/model", revision, None)
    assert files == []


# ── File listing ──

@pytest.mark.asyncio
async def test_list_files_returns_files_with_path_size_sha256():
    handler = _listing_handler([
        _file_entry("config.json", size=1234, sha256="a" * 64),
        _file_entry("model.onnx", size=5678, sha256="b" * 64),
    ])
    repo = _make_repo(handler)
    files = await repo.list_files("owner/model", "abc1234", None)
    assert files == [
        RemoteFile(path="config.json", size=1234, sha256="a" * 64),
        RemoteFile(path="model.onnx", size=5678, sha256="b" * 64),
    ]


@pytest.mark.asyncio
async def test_list_files_tolerates_entries_without_sha256():
    handler = _listing_handler([
        {"Path": "README.md", "Size": 50},
        {"Path": "model.onnx", "Size": 100, "Sha256": "c" * 64},
    ])
    repo = _make_repo(handler)
    files = await repo.list_files("owner/model", "abc1234", None)
    assert files[0].sha256 is None
    assert files[1].sha256 == "c" * 64


@pytest.mark.asyncio
async def test_list_files_handles_pagination_via_total_pages():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("PageNumber", "1")
        calls.append(page)
        if page == "1":
            files = [_file_entry(f"p1/file_{i}.bin", sha256="a" * 64) for i in range(2)]
            return _files_response(files, total_pages=2)
        files = [_file_entry("p2/file_0.bin", sha256="b" * 64)]
        return _files_response(files, total_pages=2)

    repo = _make_repo(handler)
    files = await repo.list_files("owner/model", "abc1234", None)
    assert len(files) == 3
    assert calls == ["1", "2"]


@pytest.mark.asyncio
async def test_list_files_stops_when_partial_page_returned_without_total_pages():
    """When TotalPages is absent, stop once a page returns fewer than PageSize files."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("PageNumber", "1")
        calls.append(page)
        if page == "1":
            # Full page (200 entries) → adapter must request page 2
            files = [_file_entry(f"p1/file_{i}.bin", sha256="a" * 64) for i in range(200)]
            return _files_response(files)
        # Partial page → adapter stops
        files = [_file_entry(f"p2/file_{i}.bin", sha256="b" * 64) for i in range(3)]
        return _files_response(files)

    repo = _make_repo(handler)
    files = await repo.list_files("owner/model", "abc1234", None)
    assert len(files) == 203
    assert len(calls) == 2


# ── Auth header ──

@pytest.mark.asyncio
async def test_auth_token_sent_as_bearer_header():
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        return _files_response([])

    repo = _make_repo(handler)
    await repo.list_files("owner/model", "abc1234", "ms-token-xxx")
    assert captured["authorization"] == "Bearer ms-token-xxx"


@pytest.mark.asyncio
async def test_no_auth_header_when_token_is_none():
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        return _files_response([])

    repo = _make_repo(handler)
    await repo.list_files("owner/model", "abc1234", None)
    assert captured["authorization"] is None


# ── Private repo / auth error ──

@pytest.mark.asyncio
async def test_missing_auth_token_for_private_repo_returns_clear_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"Code": 401, "Message": "Unauthorized"})

    repo = _make_repo(handler)
    with pytest.raises(PermissionError, match="(?i)auth|token|private|401"):
        await repo.list_files("owner/private", "abc1234", None)


@pytest.mark.asyncio
async def test_inspect_propagates_auth_error_for_private_repo():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"Code": 401, "Message": "Unauthorized"})

    repo = _make_repo(handler)
    with pytest.raises(PermissionError):
        await repo.inspect("owner/private", "abc1234", None)


# ── SSRF protection ──

@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1/api/v1/",
        "http://10.0.0.1/api/v1/",
        "http://172.16.0.1/api/v1/",
        "http://192.168.1.1/api/v1/",
        "http://169.254.169.254/api/v1/",
        "http://[::1]/api/v1/",
    ],
)
@pytest.mark.asyncio
async def test_ssrf_blocks_private_ip_base_urls(base_url: str):
    repo = _make_repo(lambda req: _files_response([]), base_url=base_url)
    with pytest.raises(ValueError, match="(?i)ssrf|private|blocked|loopback|link.local"):
        await repo.list_files("owner/model", "abc1234", None)


# ── Layout recognition: ORT GenAI chat ──

@pytest.mark.asyncio
async def test_ort_genai_chat_layout_recognized():
    handler = _listing_handler([
        _file_entry("genai_config.json", size=200, sha256="a" * 64),
        _file_entry("model.onnx", size=1000, sha256="b" * 64),
        _file_entry("model.onnx_data", size=2000, sha256="c" * 64),
    ])
    repo = _make_repo(handler)
    inspection = await repo.inspect("owner/chat-model", "abc1234", None)
    assert inspection.purpose is ModelPurpose.CHAT
    assert inspection.runnable is True
    assert inspection.state == "ready"
    assert "genai_config.json" in inspection.evidence.get("configs", [])


@pytest.mark.asyncio
async def test_genai_config_without_onnx_is_not_runnable():
    handler = _listing_handler([
        _file_entry("genai_config.json", size=200, sha256="a" * 64),
        _file_entry("README.md", size=50, sha256="d" * 64),
    ])
    repo = _make_repo(handler)
    inspection = await repo.inspect("owner/chat-model", "abc1234", None)
    assert inspection.purpose is None
    assert inspection.runnable is False
    assert inspection.state == "requires_configuration"


# ── Layout recognition: embedding ──

@pytest.mark.asyncio
async def test_embedding_layout_recognized_from_bert_architecture():
    config = {"architectures": ["BertModel"], "model_type": "bert"}
    handler = _listing_handler(
        [_file_entry("config.json", size=100, sha256="a" * 64),
         _file_entry("model.onnx", size=1000, sha256="b" * 64)],
        config=config,
    )
    repo = _make_repo(handler)
    inspection = await repo.inspect("owner/embed-model", "abc1234", None)
    assert inspection.purpose is ModelPurpose.EMBEDDING
    assert inspection.runnable is True
    assert inspection.state == "ready"


@pytest.mark.asyncio
async def test_embedding_layout_recognized_from_xlm_roberta_architecture():
    config = {"architectures": ["XLMRobertaModel"], "model_type": "xlm-roberta"}
    handler = _listing_handler(
        [_file_entry("config.json", size=100, sha256="a" * 64),
         _file_entry("model.onnx", size=1000, sha256="b" * 64)],
        config=config,
    )
    repo = _make_repo(handler)
    inspection = await repo.inspect("owner/embed-model", "abc1234", None)
    assert inspection.purpose is ModelPurpose.EMBEDDING


@pytest.mark.asyncio
async def test_embedding_layout_recognized_from_sentence_bert_markers():
    handler = _listing_handler([
        _file_entry("sentence_bert_config.json", size=100, sha256="a" * 64),
        _file_entry("modules.json", size=50, sha256="e" * 64),
        _file_entry("model.onnx", size=1000, sha256="b" * 64),
    ])
    repo = _make_repo(handler)
    inspection = await repo.inspect("owner/embed-model", "abc1234", None)
    assert inspection.purpose is ModelPurpose.EMBEDDING
    assert inspection.runnable is True


# ── Layout recognition: reranker ──

@pytest.mark.asyncio
async def test_reranker_layout_recognized_from_sequence_classification():
    config = {
        "architectures": ["BertForSequenceClassification"],
        "model_type": "bert",
    }
    handler = _listing_handler(
        [_file_entry("config.json", size=100, sha256="a" * 64),
         _file_entry("model.onnx", size=1000, sha256="b" * 64)],
        config=config,
    )
    repo = _make_repo(handler)
    inspection = await repo.inspect("owner/reranker-model", "abc1234", None)
    assert inspection.purpose is ModelPurpose.RERANKER
    assert inspection.runnable is True
    assert inspection.state == "ready"


@pytest.mark.asyncio
async def test_reranker_layout_recognized_from_xlm_ranker():
    config = {
        "architectures": ["XLMRobertaForSequenceClassification"],
        "model_type": "xlm-roberta",
    }
    handler = _listing_handler(
        [_file_entry("config.json", size=100, sha256="a" * 64),
         _file_entry("model.onnx", size=1000, sha256="b" * 64)],
        config=config,
    )
    repo = _make_repo(handler)
    inspection = await repo.inspect("owner/reranker-model", "abc1234", None)
    assert inspection.purpose is ModelPurpose.RERANKER


# ── Unknown layout ──

@pytest.mark.asyncio
async def test_unknown_onnx_layout_is_saved_as_requires_configuration(
    repo: ModelScopeRepository, mock_transport: httpx.MockTransport,
):
    inspection = await repo.inspect("owner/custom", "abc1234", None)
    assert inspection.runnable is False
    assert inspection.state == "requires_configuration"
    assert inspection.purpose is None


@pytest.mark.asyncio
async def test_unknown_layout_with_config_but_unrecognized_architectures():
    config = {"architectures": ["GPT2LMHeadModel"], "model_type": "gpt2"}
    handler = _listing_handler(
        [_file_entry("config.json", size=100, sha256="a" * 64),
         _file_entry("model.onnx", size=1000, sha256="b" * 64)],
        config=config,
    )
    repo = _make_repo(handler)
    inspection = await repo.inspect("owner/unknown", "abc1234", None)
    assert inspection.purpose is None
    assert inspection.runnable is False
    assert inspection.state == "requires_configuration"


# ── Purpose NOT guessed from repository name ──

@pytest.mark.asyncio
async def test_purpose_not_guessed_from_repository_name_chat_named_embedding():
    """A repo named 'chat-model' but with embedding config must be embedding."""
    config = {"architectures": ["BertModel"], "model_type": "bert"}
    handler = _listing_handler(
        [_file_entry("config.json", size=100, sha256="a" * 64),
         _file_entry("model.onnx", size=1000, sha256="b" * 64)],
        config=config,
    )
    repo = _make_repo(handler)
    inspection = await repo.inspect("owner/chat-model", "abc1234", None)
    assert inspection.purpose is ModelPurpose.EMBEDDING  # NOT chat despite name


@pytest.mark.asyncio
async def test_purpose_none_when_no_config_files_at_all():
    handler = _listing_handler([
        _file_entry("README.md", size=100, sha256="a" * 64),
        _file_entry("data.bin", size=1000, sha256="b" * 64),
    ])
    repo = _make_repo(handler)
    inspection = await repo.inspect("owner/embedding", "abc1234", None)
    # Despite name "embedding", no config → no purpose guessed
    assert inspection.purpose is None
    assert inspection.runnable is False
    assert inspection.state == "requires_configuration"


# ── Inspection evidence and missing fields ──

@pytest.mark.asyncio
async def test_inspection_evidence_includes_found_configs():
    handler = _listing_handler([
        _file_entry("genai_config.json", size=200, sha256="a" * 64),
        _file_entry("model.onnx", size=1000, sha256="b" * 64),
    ])
    repo = _make_repo(handler)
    inspection = await repo.inspect("owner/model", "abc1234", None)
    assert inspection.evidence
    assert inspection.missing == ()


@pytest.mark.asyncio
async def test_inspection_missing_listed_for_unknown_layout():
    handler = _listing_handler([_file_entry("model.onnx", size=100, sha256="a" * 64)])
    repo = _make_repo(handler)
    inspection = await repo.inspect("owner/model", "abc1234", None)
    assert inspection.missing
    assert inspection.state == "requires_configuration"


# ── Inspection echoes repository and revision ──

@pytest.mark.asyncio
async def test_inspection_echoes_repository_and_revision():
    handler = _listing_handler([_file_entry("model.onnx", size=100, sha256="a" * 64)])
    repo = _make_repo(handler)
    inspection = await repo.inspect("owner/my-model", "abc1234", None)
    assert inspection.repository == "owner/my-model"
    assert inspection.revision == "abc1234"


# ── Inspection files tuple ──

@pytest.mark.asyncio
async def test_inspection_files_tuple_matches_list_files():
    entries = [
        _file_entry("config.json", size=100, sha256="a" * 64),
        _file_entry("model.onnx", size=200, sha256="b" * 64),
    ]
    handler = _listing_handler(entries)
    repo = _make_repo(handler)
    inspection = await repo.inspect("owner/model", "abc1234", None)
    assert isinstance(inspection.files, tuple)
    assert len(inspection.files) == 2
    assert inspection.files[0].path == "config.json"


# ── Error handling ──

@pytest.mark.asyncio
async def test_inspection_returns_error_state_on_http_500():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"Code": 500, "Message": "Internal Server Error"})

    repo = _make_repo(handler)
    inspection = await repo.inspect("owner/model", "abc1234", None)
    assert inspection.state == "error"
    assert inspection.runnable is False
    assert inspection.purpose is None
    assert inspection.files == ()
