import json
from dataclasses import FrozenInstanceError

import pytest

from local_ai.catalog import CatalogLoader, CatalogSchemaError
from local_ai.contracts import CatalogFile, ModelPurpose


@pytest.fixture
def loader():
    return CatalogLoader()


def _catalog(models=(), **overrides):
    payload = {
        "schema_version": 1,
        "remote_catalog_url": None,
        "models": list(models),
    }
    payload.update(overrides)
    return payload


def _model(model_id, purpose, download_size):
    return {
        "id": model_id,
        "source": "modelscope",
        "repository": "verified/repository",
        "revision": "abcdef0",
        "purpose": purpose,
        "files": [
            {
                "path": "model.onnx",
                "size": download_size,
                "sha256": "a" * 64,
            }
        ],
        "download_size": download_size,
        "license": "Apache-2.0",
        "compatibility": {"runtimes": ["ort"]},
        "runtime_requirements": {"minimum_ram": 1},
    }


def _write_catalog(tmp_path, payload):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_curated_entries_are_immutable_and_verifiable(loader):
    for model in loader.load_curated():
        assert model.revision not in {"main", "master", "latest"}
        assert model.files
        assert all(file.sha256 and file.size > 0 for file in model.files)
        with pytest.raises(FrozenInstanceError):
            model.files[0].size = 0


def test_default_market_hides_models_over_five_gib(loader):
    assert all(
        model.download_size <= 5 * 1024**3
        for model in loader.filter(None, None, advanced=False)
    )


def test_loader_parses_versioned_catalog_and_typed_files(tmp_path):
    path = _write_catalog(
        tmp_path,
        _catalog([_model("embedding:small", "embedding", 1024)]),
    )

    model = CatalogLoader(path).load_curated()[0]

    assert model.purpose is ModelPurpose.EMBEDDING
    assert model.files == (
        CatalogFile(path="model.onnx", size=1024, sha256="a" * 64),
    )


def test_filter_combines_purpose_explicit_limit_and_default_limit(tmp_path):
    gib = 1024**3
    path = _write_catalog(
        tmp_path,
        _catalog(
            [
                _model("chat:small", "chat", gib),
                _model("embedding:medium", "embedding", 3 * gib),
                _model("embedding:large", "embedding", 6 * gib),
            ]
        ),
    )
    loader = CatalogLoader(path)

    assert [model.id for model in loader.filter(ModelPurpose.EMBEDDING, 4 * gib, False)] == [
        "embedding:medium"
    ]
    assert [model.id for model in loader.filter(None, None, True)] == [
        "chat:small",
        "embedding:medium",
        "embedding:large",
    ]


@pytest.mark.parametrize(
    "payload, match",
    [
        (_catalog(schema_version=2), "schema_version"),
        (_catalog(remote_catalog_url="ftp://example.com/catalog.json"), "remote_catalog_url"),
        ({**_catalog(), "unexpected": True}, "unexpected"),
        (_catalog([_model("chat:one", "chat", 1) | {"unexpected": True}]), "unexpected"),
        (
            _catalog(
                [
                    _model("chat:one", "chat", 1)
                    | {
                        "files": [
                            {
                                "path": "model.onnx",
                                "size": 1,
                                "sha256": "a" * 64,
                                "unexpected": True,
                            }
                        ]
                    }
                ]
            ),
            "unexpected",
        ),
    ],
)
def test_strict_schema_rejects_unsupported_or_unknown_data(tmp_path, payload, match):
    path = _write_catalog(tmp_path, payload)

    with pytest.raises(CatalogSchemaError, match=match):
        CatalogLoader(path).load_curated()


@pytest.mark.parametrize(
    "models, match",
    [
        ([_model("chat:one", "chat", 1) | {"license": ""}], "license"),
        ([_model("chat:one", "chat", 2) | {"download_size": 3}], "download_size"),
        (
            [
                _model("chat:one", "chat", 1),
                _model("chat:one", "chat", 1),
            ],
            "duplicate model id",
        ),
        (
            [
                _model("chat:one", "chat", 2)
                | {"files": [_model("unused", "chat", 1)["files"][0]] * 2}
            ],
            "duplicate file path",
        ),
    ],
)
def test_strict_schema_rejects_unverifiable_catalog_metadata(tmp_path, models, match):
    path = _write_catalog(tmp_path, _catalog(models))

    with pytest.raises(CatalogSchemaError, match=match):
        CatalogLoader(path).load_curated()
