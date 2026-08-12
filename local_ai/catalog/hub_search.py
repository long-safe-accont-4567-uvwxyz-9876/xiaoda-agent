"""在线模型仓库搜索：HuggingFace 国内镜像（hf-mirror.com）与 ModelScope。

只允许固定的模型仓库主机（SSRF 白名单），用户输入仅作为搜索关键字，
不参与 URL 拼接（无 SSRF 风险）。搜索结果用于「模型广场 → 在线搜索」，
由用户自行选择仓库并检视后下载，不做预设目录。
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from local_ai.catalog.modelscope import _check_ssrf

# SSRF 白名单：仅允许这些固定镜像主机（用户输入不参与 host 决定）
_ALLOWED_HOSTS = frozenset({"hf-mirror.com", "www.modelscope.cn", "modelscope.cn"})
_HTTP_TIMEOUT = 15.0
_SHA40_RE = re.compile(r"[0-9a-f]{40}")

# ── 模型分类（功能性小模型为主，本机 2GB 内存跑不动的大 LLM 不展示） ──
# HF pipeline_tag 与 ModelScope task 的统一归类；"chat" 大模型默认过滤，
# 用户可在分类中主动选择（多为大模型，仅作搜索入口）。
_CATEGORY_PIPELINES: dict[str, set[str]] = {
    "embedding": {"text-embedding", "feature-extraction", "sentence-similarity"},
    "rerank": {"rerank", "semantic-rerank"},
    "chat": {"text-generation", "text2text-generation"},
    "other": {
        "text-classification",
        "token-classification",
        "question-answering",
        "fill-mask",
        "translation",
        "text-to-speech",
        "automatic-speech-recognition",
        "auto-speech-recognition",
        "image-classification",
        "image-to-text",
        "object-detection",
    },
}
# 功能性小模型 = 全部类别除去 chat（对话大模型）
_FUNCTIONAL_PIPELINES = (
    _CATEGORY_PIPELINES["embedding"]
    | _CATEGORY_PIPELINES["rerank"]
    | _CATEGORY_PIPELINES["other"]
)

# 各分类的"主 pipeline"用于镜像端单值 filter 拉取（避免 OR 语义问题）
_CATEGORY_FILTERS: dict[str, list[str]] = {
    "embedding": ["text-embedding", "feature-extraction", "sentence-similarity"],
    "rerank": ["rerank"],
    "chat": ["text-generation", "text2text-generation"],
    "other": [
        "text-classification",
        "token-classification",
        "question-answering",
        "fill-mask",
        "translation",
        "text-to-speech",
        "automatic-speech-recognition",
        "image-classification",
        "image-to-text",
        "object-detection",
    ],
}


def _categorize(pipeline_tag: str | None) -> str:
    """把 pipeline_tag / task 归入分类（未知归入 other）。"""
    if not pipeline_tag:
        return "other"
    tag = pipeline_tag.strip().lower()
    for category, pipelines in _CATEGORY_PIPELINES.items():
        if tag in pipelines:
            return category
    return "other"


class HubSearchError(RuntimeError):
    pass


def _verify_host(base_url: str) -> None:
    parsed_host = base_url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    if parsed_host not in _ALLOWED_HOSTS:
        raise HubSearchError(f"unsupported hub host: {parsed_host}")
    _check_ssrf(base_url, skip_dns=False)


def _request(base_url: str) -> str:
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
            response = client.get(base_url, headers={"User-Agent": "nahida-web/1.0"})
            response.raise_for_status()
            return response.text
    except httpx.HTTPError as error:
        raise HubSearchError(f"hub search failed: {error}") from error


def search_hf_mirror(
    query: str,
    *,
    limit: int = 20,
    category: str = "all",
) -> list[dict[str, Any]]:
    """在 hf-mirror.com 搜索模型仓库（内置 huggingface_hub CLI 库，走镜像）。

    返回 [{id, downloads, likes, sha, modified_at, pipeline_tag, tags, category}]，
    sha 为默认分支不可变 commit hash，可直接作为检视/下载的 revision。
    category 指定时按 pipeline 过滤；默认只返回功能性小模型（不含 chat 大模型）。
    """
    try:
        from huggingface_hub import HfApi
    except ImportError as error:  # pragma: no cover
        raise HubSearchError(f"huggingface_hub unavailable: {error}") from error

    api = HfApi(endpoint="https://hf-mirror.com")
    # 确定要拉取的 pipeline 集合：分类明确时按分类；"all" 默认只拉功能节点
    pipelines = list(_CATEGORY_FILTERS[category]) if category in _CATEGORY_FILTERS else sorted(_FUNCTIONAL_PIPELINES)
    # 并发按各 pipeline 单值 filter 拉取（多值 filter 是 AND 语义，逐个取再合并）
    per_pipeline = max(1, limit // len(pipelines))

    def fetch(pipeline: str) -> list[dict[str, Any]]:
        try:
            kwargs: dict[str, Any] = {"filter": pipeline, "sort": "downloads", "limit": per_pipeline, "full": True}
            if query.strip():
                kwargs["search"] = query.strip()
            models = api.list_models(**kwargs)
        except Exception as error:  # noqa: BLE001
            raise HubSearchError(f"hf-mirror search failed: {error}") from error
        results = []
        for item in models:
            model_id = getattr(item, "id", None)
            if not isinstance(model_id, str) or not model_id.strip():
                continue
            pipeline_tag = getattr(item, "pipeline_tag", None) or pipeline
            results.append({
                "id": model_id.strip(),
                "source": "hf-mirror",
                "downloads": int(getattr(item, "downloads", None) or 0),
                "likes": int(getattr(item, "likes", None) or 0),
                "sha": getattr(item, "sha", None),
                "modified_at": getattr(item, "lastModified", None),
                "pipeline_tag": pipeline_tag,
                "tags": list(getattr(item, "tags", None) or []),
                "category": _categorize(pipeline_tag),
            })
        return results

    results: list[dict[str, Any]] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=min(len(pipelines), 6)) as pool:
        futures = {pool.submit(fetch, pipeline): pipeline for pipeline in pipelines}
        for future in futures:
            try:
                results.extend(future.result())
            except HubSearchError as error:
                errors.append(str(error))
    if not results and errors:
        raise HubSearchError(errors[0])
    # 按下载量排序并去重、截断
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in sorted(results, key=lambda it: it["downloads"], reverse=True):
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        unique.append(item)
        if len(unique) >= limit:
            break
    return unique


def _modelscope_revision(repository: str) -> str | None:
    """取 ModelScope 仓库默认分支（master）的不可变 commit hash。

    新版 files 接口每个条目携带 Revision 字段；取第一个 40 位 hex 即可。
    解析失败返回 None（前端会提示手动填写 hash）。
    """
    url = (
        "https://www.modelscope.cn/api/v1/models/"
        f"{_urlencode(repository, safe='/')}/repo/files?Revision=master&PageSize=1"
    )
    try:
        payload = json.loads(_request(url))
    except (HubSearchError, json.JSONDecodeError):
        return None
    entries = (payload.get("Data") or {}).get("Files") or []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        revision = entry.get("Revision")
        if isinstance(revision, str) and _SHA40_RE.fullmatch(revision):
            return revision
    return None


def search_modelscope(
    query: str,
    *,
    limit: int = 20,
    category: str = "all",
) -> list[dict[str, Any]]:
    """在 ModelScope 搜索模型仓库（新版 OpenAPI，无需鉴权）。

    返回 [{id, name, downloads, likes, revision, pipeline_tag, modified_at, tags, category}]，
    revision 为默认分支不可变 commit hash（并发解析，失败为 None）。
    category 指定时按任务类型过滤；默认只返回功能性小模型（不含 chat 大模型）。

    注意：OpenAPI 的 page_size 上限为 50，此前用 limit*3（=60）会触发 400
    导致 ModelScope 搜索结果为空。这里固定每页 50 并翻页拉取，直到凑够
    limit 个功能节点（热门榜前几页几乎全是对话大模型，翻页才能捞到）。
    """
    page_size = 50
    max_pages = 3
    collected: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        base = (
            "https://modelscope.cn/openapi/v1/models"
            f"?search={_urlencode(query)}&page_size={page_size}"
            f"&sort=downloads&page_number={page}"
        )
        _verify_host(base)
        payload = _request(base)
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError) as error:
            raise HubSearchError(f"invalid modelscope response: {error}") from error
        items = (data.get("data") or {}).get("models") if isinstance(data, dict) else []
        if not isinstance(items, list):
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if not isinstance(model_id, str) or not model_id.strip():
                continue
            tasks = item.get("tasks")
            tags = item.get("tags")
            pipeline_tag = (
                tasks[0]
                if isinstance(tasks, list) and tasks and isinstance(tasks[0], str)
                else None
            )
            item_category = _categorize(pipeline_tag)
            if category == "all" and item_category == "chat":
                continue
            if category != "all" and item_category != category:
                continue
            collected.append(
                {
                    "id": model_id.strip(),
                    "source": "modelscope",
                    "name": (
                        item.get("display_name")
                        if isinstance(item.get("display_name"), str)
                        else model_id.strip()
                    ),
                    "downloads": int(item.get("downloads") or 0),
                    "likes": int(item.get("likes") or 0),
                    "revision": None,
                    "pipeline_tag": pipeline_tag,
                    "modified_at": item.get("last_modified"),
                    "tags": (
                        [tag for tag in tags if isinstance(tag, str)]
                        if isinstance(tags, list)
                        else []
                    ),
                    "category": item_category,
                }
            )
        if len(collected) >= limit:
            break
        if len(items) < page_size:
            break  # 已到最后一页
    results = collected[:limit]
    # 并发解析默认分支 commit hash（供检视/下载使用）
    if results:
        ids = [result["id"] for result in results]
        with ThreadPoolExecutor(max_workers=6) as pool:
            revisions = list(pool.map(_modelscope_revision, ids))
        for result, revision in zip(results, revisions):
            result["revision"] = revision
    return results


def search_hub(
    query: str,
    source: str,
    *,
    limit: int = 20,
    category: str = "all",
) -> dict[str, Any]:
    """按来源搜索（双源并发、失败隔离、跨源合并），返回统一结构：

        {"results": [统一条目], "errors": [失败源的错误信息]}

    统一条目字段：id / name / sources（出现过的所有源）/ source（首选源，
    优先选带不可变 hash 的） / downloads / likes / sha / revision /
    pipeline_tag / modified_at / tags / category。
    同一 id 在两源都存在时合并为一行（来源标注双源），彻底统一展示。

    关键词为空时返回各源功能性小模型（默认过滤 chat 大模型——本机
    跑不动也无下载意义），供模型广场进入页面时自动获取。
    category 取值：all / embedding / rerank / chat / other。
    """
    normalized = (source or "all").strip().lower()
    if normalized not in {"all", "hf-mirror", "modelscope"}:
        raise HubSearchError(f"unsupported hub source: {source}")
    normalized_category = (category or "all").strip().lower()
    if normalized_category not in _CATEGORY_PIPELINES:
        normalized_category = "all"
    # source=all 时两源各取一半配额，避免一源占满 limit 截掉另一源
    per_source = limit if normalized != "all" else max(1, limit // 2)

    def run(source_name: str) -> list[dict[str, Any]]:
        if source_name == "hf-mirror":
            return search_hf_mirror(query, limit=per_source, category=normalized_category)
        return search_modelscope(query, limit=per_source, category=normalized_category)

    batch: dict[str, list[dict[str, Any]]] = {
        "hf-mirror": [],
        "modelscope": [],
    }
    errors: list[str] = []
    active = [name for name in batch if normalized in {"all", name}]
    with ThreadPoolExecutor(max_workers=len(active)) as pool:
        futures = {pool.submit(run, name): name for name in active}
        for future in futures:
            name = futures[future]
            try:
                batch[name] = future.result()
            except HubSearchError as error:
                errors.append(f"{name}: {error}")

    # 跨源合并：同 id 合并为一行，保留全部来源与各源 hash
    merged: dict[str, dict[str, Any]] = {}
    for name, items in batch.items():
        for item in items:
            entry = merged.get(item["id"])
            if entry is None:
                merged[item["id"]] = {
                    "id": item["id"],
                    "name": item.get("name") or item["id"],
                    "sources": [name],
                    "source": name,
                    "downloads": item["downloads"],
                    "likes": item["likes"],
                    "sha": item.get("sha"),
                    "revision": item.get("revision"),
                    "pipeline_tag": item.get("pipeline_tag"),
                    "modified_at": item.get("modified_at"),
                    "tags": item.get("tags") or [],
                    "category": item.get("category") or "other",
                }
                continue
            if name not in entry["sources"]:
                entry["sources"].append(name)
            entry["downloads"] = max(entry["downloads"], item["downloads"])
            entry["likes"] = max(entry["likes"], item["likes"])
            if name == "hf-mirror" and item.get("sha"):
                entry["sha"] = item["sha"]
            elif name == "modelscope" and item.get("revision"):
                entry["revision"] = item["revision"]
            if not entry["pipeline_tag"] and item.get("pipeline_tag"):
                entry["pipeline_tag"] = item["pipeline_tag"]
            if not entry["category"] or entry["category"] == "other":
                if item.get("category"):
                    entry["category"] = item["category"]

    # 首选源：优先带不可变 hash 的那一个（能检视/下载），保证两源都能操作
    for entry in merged.values():
        if len(entry["sources"]) > 1:
            if entry["revision"] and "modelscope" in entry["sources"]:
                entry["source"] = "modelscope"
            elif entry["sha"] and "hf-mirror" in entry["sources"]:
                entry["source"] = "hf-mirror"

    results = sorted(merged.values(), key=lambda it: it["downloads"], reverse=True)[:limit]
    return {"results": results, "errors": errors}


def _urlencode(value: str, safe: str = "") -> str:
    from urllib.parse import quote

    return quote(value, safe=safe)


__all__ = [
    "HubSearchError",
    "search_hf_mirror",
    "search_modelscope",
    "search_hub",
]
