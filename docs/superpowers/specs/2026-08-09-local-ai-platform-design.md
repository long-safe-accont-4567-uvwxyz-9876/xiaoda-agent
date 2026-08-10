# Local AI Platform and Provider Integration Design

## Purpose

This design reorganizes the local deployment and model-provider areas into maintainable modules, replaces hard-coded hardware descriptions with real capability discovery, adds a ModelScope-backed ONNX model market, and completes local, cloud, preset, and custom provider onboarding.

The implementation remains inside the existing Vue 3 Web UI and follows its current visual language. It does not introduce a separate frontend application or design system.

## Confirmed Product Decisions

- The existing Local Deployment sidebar entry remains the only navigation entry for local AI.
- Its page contains five peer tabs: Deployments, Model Market, Installed Models, Compute Devices, and Download Tasks.
- The market supports chat, embedding, and reranker model types through one lifecycle.
- Chat models use ONNX Runtime GenAI. Embedding and reranker models use standard ONNX Runtime.
- The first market uses a curated ModelScope catalog plus an advanced custom repository installer.
- Curated models are primarily quantized 3B–4B models and default to a maximum download size of 5 GB. Advanced mode can unlock larger models after explicit confirmation.
- Windows x64, Linux x64, and Linux ARM64 have complete download and execution paths. Android receives reusable contracts and manifest formats, not a new Android client in this scope.
- Device selection uses automatic recommendations with a per-model manual override.
- OpenAI-compatible, native Anthropic, Ollama, and configurable custom-mapping providers are first-class protocols.
- Except for the existing bundled BGE model, downloads require a server-side directory selection dialog. The dialog can save the selected directory as the default. If the user does not save it, every later download asks again.
- Installation completion asks whether to start the model. It never allocates significant memory without confirmation.
- The application does not silently switch to another provider when the selected model or provider is unavailable.

## Current-State Problems

The current Local Deployment page manages only the embedding engine, despite its broad name. Its description is hard-coded around the bundled BGE model. Device detection, runtime availability, and display labels are not derived from a shared capability model.

The NPU card reports `Vivante VIP9000 (3 TOPS INT8)` after a runner probe succeeds, even though the probe does not read that model or throughput. The standard ONNX adapter always constructs `CPUExecutionProvider`; detected GPUs cannot be selected. The NPU path is a separate VIP runner and NBG adapter rather than an ONNX Runtime execution provider.

Local model identity, paths, dimensions, download lifecycle, runtime compatibility, and installation status are spread across VectorStore, embedding adapters, API routes, build files, and scripts. There is no model registry or installer.

Provider data is duplicated across metadata, setup, server startup, agent registry, discovery, and router defaults. Custom providers have incomplete capability discovery and native Anthropic streaming support. Provider creation can accept unreachable credentials or endpoints before a later health test discovers the problem.

## Architectural Direction

The system adopts a unified native local runtime platform. Internal Agent calls do not require an extra localhost HTTP hop. An optional OpenAI-compatible local gateway can be added later for external consumers without becoming an internal dependency.

```text
local_ai/
├── contracts.py
├── devices/
├── catalog/
├── models/
├── downloads/
├── runtimes/
└── instances/
```

### Public Contracts

`contracts.py` defines stable, serializable domain types:

- `ComputeDevice`: identity, hardware type, architecture, system metadata, execution providers, memory, state, and probe evidence.
- `ExecutionBackend`: runtime kind, provider name, provider options, supported model types, precision support, and health.
- `CatalogModel`: source, repository, revision, purpose, parameter count, quantization, files, size, license, compatibility, and runtime requirements.
- `InstalledModel`: immutable model identity, source revision, absolute directory, manifest checksum, validation state, and installation ownership.
- `DownloadTask`: lifecycle state, bytes, speed, remaining estimate, resumability, destination, and error details.
- `RuntimeProfile`: runtime adapter, device binding, model options, memory estimate, and fallback policy.
- `ModelInstance`: process/session state, health, resource usage, active routes, and timestamps.

Consumers depend on these contracts rather than internal adapter classes.

### Device Registry

`DeviceRegistry` is the sole authority for local compute capability. It combines system hardware discovery, runtime provider enumeration, provider smoke tests, vendor extensions, and model compatibility.

System discovery uses platform-specific adapters:

- Windows x64: CIM/PowerShell hardware data and DirectML-visible adapters.
- Linux x64: CPU metadata, PCI devices, NVIDIA/CUDA, AMD/ROCm, and OpenVINO availability.
- Linux ARM64: device tree, DRM devices, vendor nodes, drivers, and CPU architecture.

ONNX Runtime capability comes from `onnxruntime.get_available_providers()`. A provider is marked usable only after a minimal session initializes successfully. Merely detecting a GPU does not imply the corresponding ONNX Runtime package or driver is usable.

Vendor extensions are separate execution backends. The VIP runner appears only when the executable, model artifact, permission path, driver, and probe all succeed. The displayed model comes from probe JSON, device tree, or driver data. Unknown throughput is displayed as unknown; no TOPS value is invented.

Automatic recommendation ranks only compatible backends:

1. Manifest and architecture compatibility.
2. Available memory against minimum and recommended requirements.
3. A healthy acceleration backend.
4. Standard CPU fallback when the manifest permits it.

Users can persist a per-model override. Startup fallback follows only the manifest-declared order and reports each failed backend. It never silently changes the selected cloud provider.

### Model Catalog

`ModelCatalog` exposes a versioned curated catalog and a custom ModelScope repository parser. The catalog is data, not Python branches.

Every catalog record contains:

- Stable model ID, source repository, immutable revision, and license.
- Purpose: `chat`, `embedding`, or `reranker`.
- Parameter count, quantization, download size, and installed size.
- Required file list, expected sizes, and SHA256 hashes.
- Runtime adapter and protocol version.
- Supported OS, architecture, execution provider, and precision combinations.
- Minimum and recommended RAM/VRAM.
- Input/output schema, tokenizer requirements, context limits, dimensions or score shape.
- Post-install smoke-test definition.

The default view filters curated models to 5 GB or less and emphasizes quantized 3B–4B chat models. Advanced mode may show larger entries after a warning.

Custom repositories require an explicit revision. The parser inspects the repository file list and metadata before download. A repository with an unknown runtime schema can be registered as requiring configuration, but cannot claim one-click execution.

### Installation and Downloads

`ModelInstaller` owns destination selection, disk checks, downloads, verification, atomic installation, and removal. `DownloadManager` owns task state, cancellation, persistence, and WebSocket events.

For every non-bundled model:

1. The download action opens a server-directory picker when no saved default exists or when the previous choice was not saved.
2. The picker allows browsing and manual input. The backend validates existence or creatability, write permission, platform path rules, allowed root policy, and free space.
3. A checkbox stores the selected directory as the default model root. Without it, the choice applies only to that download and the next download asks again.
4. Confirmation shows repository, immutable revision, license, download bytes, temporary-space requirement, final size, destination, and remaining disk space.
5. Files download into destination-local `.part` files using HTTP Range when supported.
6. Tasks report bytes, percentage, speed, remaining time, and current file through WebSocket events. Pause, resume, and cancel operate on running tasks.
7. Each file is checked against expected size and SHA256.
8. Runtime structure validation and a minimal load/inference smoke test run before registration.
9. A successful installation is made visible atomically with `os.replace()` and written to the installed-model registry.
10. The UI offers compatible devices and asks whether to start the model.

Failed downloads retain resumable partial files unless the user discards them. Failed verification moves files to a quarantine state and never registers them as runnable. Restart reconstructs task state from persisted records and partial files.

The existing bundled BGE model is represented as an immutable built-in registry entry. It does not ask for a destination and is not removable through the market.

Removal is explicit and destructive. A running model or one referenced by an active route cannot be removed until the dependency is stopped or changed. The installer only deletes paths owned by the corresponding registry entry.

### Runtime Registry

`RuntimeRegistry` maps model manifests to adapters:

- `OrtGenAiChatRuntime`: tokenization, KV cache, sampling, streaming tokens, cancellation, and tool-capability boundaries for chat models.
- `OrtEmbeddingRuntime`: standard ONNX Runtime session, batching, pooling, normalization, and dimension validation.
- `OrtRerankerRuntime`: paired input encoding, batching, score extraction, and output validation.
- `VipNbgRuntime`: explicit vendor runner for compatible NBG artifacts only.

Runtime adapters implement prepare, start, health, infer/stream, cancel, and stop contracts. Provider options are supplied by the selected execution backend; they are not hard-coded inside model adapters.

`InstanceManager` serializes startup and shutdown, tracks resources and routes, and exposes actual health. A missing device changes an instance to degraded. It does not keep reporting running.

Local chat instances reach the existing model router through `LocalProviderTransport`. Embedding instances are consumed through a narrow embedding interface. Reranker instances are consumed by the memory retrieval ranking seam. VectorStore no longer owns model directory discovery or hardware probing.

### Provider Catalog

Cloud and API providers use a single `ProviderCatalog` as the authority for metadata and lifecycle. The following concerns no longer maintain independent provider-name tables:

- Setup wizard.
- Server startup restoration.
- Model discovery.
- Main and sub-agent routing.
- Credential lookup.
- Health probes.

A provider definition contains identity, protocol, endpoints, authentication, capability declarations, discovery behavior, health checks, and defaults.

Supported protocol transports are:

- OpenAI-compatible: streaming, tools, vision, model discovery, JSON mode, and normalized errors.
- Native Anthropic: Messages streaming, tool calls and results, model discovery fallback, and normalized errors.
- Ollama: localhost or LAN endpoint discovery, model discovery, and local service health.
- Custom mapping: configurable chat path, model-list path, authentication header, request fields, response fields, streaming framing, and templates.
- Local ORT: native in-process transport for installed local chat instances.

Provider onboarding is a four-step flow:

1. Choose a preset, local service, or custom protocol.
2. Enter endpoint and credential data; apply SSRF and local-network policies.
3. Run real connectivity, authentication, model, streaming, and optional tool tests.
4. Review the capability matrix and atomically save/register after confirmation.

Creation or update does not persist partial configuration when client construction or required tests fail. API keys remain in the encrypted credential store and are injected directly into clients rather than global environment variables.

An unavailable model-list API does not block a compatible provider: users can enter a model ID manually. Route saves validate that the provider is enabled, credentials are available, the model belongs to or is explicitly accepted by the provider, and the runtime client exists.

Local models appear in the shared model selector with local status, execution device, and memory usage. No automatic cross-provider fallback is introduced.

## Web UI

The existing sidebar entry and visual system remain unchanged. `LocalDeployView.vue` becomes a thin page shell with a dynamic summary and five peer tabs:

- Deployments: instances, purpose, device, resource use, start/stop, and default role selection.
- Model Market: curated catalog, custom repository, filters, details, and download actions.
- Installed Models: built-in and user models, path, version, validation, migration, start, and removal.
- Compute Devices: real hardware, ORT providers, drivers, memory, compatible model types, evidence, and rescan.
- Download Tasks: progress, speed, remaining time, pause, resume, cancel, errors, and recovery.

The frontend is split into focused components under `components/local-ai/` and a dedicated `stores/localAi.ts`. Business decisions stay in backend services; components render contracts and dispatch actions.

The Models page remains responsible for providers and task routes. It gains the unified onboarding wizard, capability test matrix, and custom mapping editor. Shared model-selection and health components are reused rather than copied.

All additions use existing CSS variables, Naive UI controls, spacing, cards, motion, responsive breakpoints, and interaction conventions. Narrow layouts use horizontally scrollable tabs and keep actions clickable.

The hard-coded BGE subtitle becomes a dynamic summary. Device cards never display model names or throughput not present in backend evidence.

## API Boundaries

Existing `/api/v1/local-deploy/*` endpoints remain temporarily as compatibility facades. New APIs are grouped by resource:

- `/api/v1/local-ai/devices`
- `/api/v1/local-ai/catalog`
- `/api/v1/local-ai/models`
- `/api/v1/local-ai/downloads`
- `/api/v1/local-ai/instances`
- `/api/v1/local-ai/storage`
- `/api/v1/providers`

Long-running work returns a task ID and publishes state changes over the existing authenticated WebSocket. REST reads are idempotent. Mutations use request IDs where repeated submission could duplicate a download or instance.

The storage API lists only server directories permitted by policy and validates a submitted path server-side. It does not rely on browser-local filesystem paths.

## Persistence

SQLite stores catalog cache metadata, installed-model registry, download tasks, runtime profiles, and instance history. Large files and partial files remain on the selected filesystem.

Configuration stores only preferences such as default model root and per-model device override. Credentials remain in the existing encrypted credential store.

Manifest and schema versions are explicit. Migrations are monotonic and preserve the existing bundled model as a generated built-in entry.

## Error and Recovery Semantics

Download, verification, installation, startup, and runtime health are distinct states with structured errors. A generic failed state does not erase the failing stage.

- Network failure: resumable when the source supports Range.
- User cancellation: task becomes cancelled and retains or discards partials according to the user action.
- Insufficient disk: rejected before download and rechecked before atomic installation.
- Hash mismatch: quarantined, never runnable.
- Runtime incompatibility: installation may remain valid for another device, but startup is blocked with compatibility evidence.
- Device loss: instance becomes degraded and rejects new work.
- Selected local model stopped: new requests return an actionable local-model-unavailable error; they do not silently use cloud inference.
- Application shutdown: download tasks checkpoint and runtime instances stop in dependency order.

Destructive operations require confirmation. Paths outside explicitly selected roots or registered model directories are never deleted.

## Cross-Platform Scope

Windows x64, Linux x64, and Linux ARM64 implement full discovery, installation, and runtime selection. Each platform exposes only execution providers that the installed ONNX Runtime build can initialize.

AMD acceleration is based on actual provider availability: ROCm on compatible Linux installations and DirectML on compatible Windows installations. Linux ARM64 always supports a CPU fallback when the model manifest permits it. Vendor NPU adapters require their matching runtime and model artifact.

Android reuses catalog manifests and domain contracts. Building an Android application, packaging JNI/AAR artifacts, and Android lifecycle integration are excluded from this implementation.

Market models are downloaded at runtime and are not included in PyInstaller, Docker, or release archives. The existing bundled BGE model remains the only exception unless a later release decision changes it.

## Project Organization Strategy

The project is reorganized gradually, not through a repository-wide mechanical move:

1. Add contracts and characterization tests around current behavior.
2. Establish DeviceRegistry, ModelRegistry, DownloadManager, RuntimeRegistry, and ProviderCatalog as authoritative seams.
3. Convert current Local Deployment and Provider APIs into facade calls.
4. Migrate VectorStore, ModelRouter, startup restoration, and UI consumers.
5. Remove duplicate provider maps, hard-coded device descriptions, and obsolete compatibility paths only after reference searches and regression tests prove they are unused.

Unrelated large modules are not refactored in this feature. This keeps organization work tied to maintainability of local deployment and provider integration rather than creating an unbounded rewrite.

## Testing and Acceptance

### Device and Runtime

- Contract tests for Windows x64, Linux x64, and Linux ARM64 discovery evidence.
- ORT provider enumeration and minimal-session verification tests.
- VIP probe parsing tests that prove unknown model or TOPS data is never invented.
- Compatibility, recommendation, manual override, fallback, and device-loss tests.
- ORT GenAI chat streaming and cancellation tests.
- Embedding dimension/pooling tests and reranker score-shape tests.

### Market and Storage

- Curated manifest schema, revision, license, file-list, size, and hash tests.
- ModelScope repository listing and custom-repository compatibility tests.
- Directory picker authorization, writeability, Windows/Linux path, free-space, and collision tests.
- First-download, save-default, and ask-again behavior tests.
- HTTP Range resume, progress, pause, running cancellation, restart recovery, SHA256 failure, quarantine, and atomic install tests.
- Installation and removal ownership tests.

### Providers

- OpenAI, Anthropic, Ollama, custom mapping, and local ORT transport contract suites.
- Streaming, tools, model discovery fallback, authentication, and normalized error tests.
- Atomic create/update rollback and encrypted credential tests.
- Route-provider-model validation and no-cross-provider-fallback tests.

### Frontend and Delivery

- Store and component tests for all five tabs and state transitions.
- Browser verification of directory selection, download, pause/resume, install, start, route selection, stop, and removal.
- Vue type checking and production build.
- Full Python regression suite.
- Windows x64, Linux x64, and Linux ARM64 package smoke tests that verify runtime dependencies without bundling market models.
- Documentation for provider requirements, platform execution providers, storage behavior, and Android contract-only scope.

## Non-Goals

- A standalone Android application.
- Bundling the market catalog models in release artifacts.
- Arbitrary ModelScope repositories claiming one-click compatibility without a recognized manifest.
- Silent cross-provider model switching.
- Refactoring unrelated large subsystems solely to reduce file length.
