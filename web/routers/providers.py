from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from llm_gateway.provider_service import ProviderConnectionError, ProviderInUseError
from web.routers.auth import get_current_user
from web.schemas import Envelope

router = APIRouter(tags=["providers"], dependencies=[Depends(get_current_user)])


def _service(request: Request):
    return request.app.state.provider_service


def _report_data(report) -> dict[str, Any]:
    return {
        "available": report.available,
        "capabilities": asdict(report.capabilities),
        "models": list(report.models),
        "error": report.error,
    }


def _definition_data(definition) -> dict[str, Any]:
    return {
        "id": definition.id,
        "protocol": definition.protocol.value,
        "base_url": definition.endpoint.base_url,
        "chat_path": definition.endpoint.chat_path,
        "models_path": definition.endpoint.models_path,
        "default_model": definition.default_model,
        "builtin": definition.builtin,
        "capabilities": asdict(definition.capabilities),
        "label": definition.metadata.get("label", definition.id),
        "enabled": definition.metadata.get("enabled", True),
        "auth": {
            "required": definition.auth.required,
            "header": definition.auth.header,
            "scheme": definition.auth.scheme,
        },
        "mapping": dict(definition.metadata.get("mapping") or {}),
        "headers": dict(definition.metadata.get("headers") or {}),
    }


@router.post("/providers/test", response_model=Envelope[dict])
async def test_provider(body: dict, request: Request) -> Any:
    try:
        report = await _service(request).test(body.get("draft", body), body.get("credentials"))
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    return Envelope(data=_report_data(report))


@router.get("/providers", response_model=Envelope[list[dict]])
async def list_providers(request: Request) -> Any:
    return Envelope(data=[_definition_data(item) for item in _service(request).list()])


@router.post("/providers", response_model=Envelope[dict])
async def create_provider(body: dict, request: Request) -> Any:
    try:
        definition = await _service(request).create(body.get("draft", body), body.get("credentials"))
    except ProviderConnectionError as error:
        raise HTTPException(422, str(error)) from None
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    return Envelope(data=_definition_data(definition))


@router.put("/providers/{provider_id}", response_model=Envelope[dict])
async def update_provider(provider_id: str, body: dict, request: Request) -> Any:
    try:
        definition = await _service(request).update(provider_id, body.get("draft", body), body.get("credentials"))
    except KeyError:
        raise HTTPException(404, f"provider {provider_id} not found") from None
    except ProviderConnectionError as error:
        raise HTTPException(422, str(error)) from None
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    return Envelope(data=_definition_data(definition))


@router.delete("/providers/{provider_id}", response_model=Envelope[dict])
async def delete_provider(provider_id: str, request: Request) -> Any:
    if request.headers.get("X-Confirm") != "yes":
        raise HTTPException(400, "missing X-Confirm: yes")
    try:
        await _service(request).delete(provider_id)
    except KeyError:
        raise HTTPException(404, f"provider {provider_id} not found") from None
    except ProviderInUseError as error:
        raise HTTPException(409, f"provider is used by routes: {error}") from None
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    return Envelope(data={"deleted": provider_id})


@router.get("/providers/{provider_id}/capabilities", response_model=Envelope[dict])
async def provider_capabilities(provider_id: str, request: Request) -> Any:
    try:
        report = await _service(request).capabilities(provider_id)
    except KeyError:
        raise HTTPException(404, f"provider {provider_id} not found") from None
    return Envelope(data=_report_data(report))


@router.get("/providers/{provider_id}/models", response_model=Envelope[dict])
async def provider_models(provider_id: str, request: Request) -> Any:
    try:
        models = await _service(request).discover_models(provider_id)
    except KeyError:
        raise HTTPException(404, f"provider {provider_id} not found") from None
    except ProviderConnectionError as error:
        raise HTTPException(503, str(error)) from None
    return Envelope(data={"provider": provider_id, "models": list(models)})
