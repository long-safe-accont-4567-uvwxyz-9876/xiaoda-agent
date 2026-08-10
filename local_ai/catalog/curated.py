from __future__ import annotations

import json
from pathlib import Path

from local_ai.catalog.schema import CatalogSchemaError, parse_catalog
from local_ai.contracts import CatalogModel, ModelPurpose


_DEFAULT_CATALOG_PATH = Path(__file__).resolve().parents[2] / "config" / "local_model_catalog.json"
_DEFAULT_MAX_DOWNLOAD_BYTES = 5 * 1024**3


class CatalogLoader:
    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else _DEFAULT_CATALOG_PATH

    def load_curated(self) -> list[CatalogModel]:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise CatalogSchemaError(f"catalog cannot be read: {error}") from error
        return list(parse_catalog(payload))

    def filter(
        self,
        purpose: ModelPurpose | None,
        max_download_bytes: int | None,
        advanced: bool,
    ) -> list[CatalogModel]:
        if purpose is not None:
            try:
                purpose = ModelPurpose(purpose)
            except ValueError as error:
                raise CatalogSchemaError(f"purpose is invalid: {purpose}") from error
        if max_download_bytes is not None and (type(max_download_bytes) is not int or max_download_bytes < 0):
            raise CatalogSchemaError("max_download_bytes must be a non-negative integer or null")
        limit = max_download_bytes
        if not advanced:
            limit = min(limit, _DEFAULT_MAX_DOWNLOAD_BYTES) if limit is not None else _DEFAULT_MAX_DOWNLOAD_BYTES
        return [
            model
            for model in self.load_curated()
            if (purpose is None or model.purpose is purpose)
            and (limit is None or model.download_size <= limit)
        ]
