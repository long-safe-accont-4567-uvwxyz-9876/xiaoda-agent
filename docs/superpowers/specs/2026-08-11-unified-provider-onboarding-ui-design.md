# Unified Provider Onboarding UI Design

## Scope

Task 20 replaces the legacy provider modal and split provider lifecycle with one typed onboarding flow backed by `/api/v1/providers`. The work includes the frontend wizard, provider API and Pinia store, capability presentation, safe custom mapping configuration, ModelsView integration, and the backend contract needed for `custom-map` to work end to end.

The existing Models page remains the only navigation entry. Task routing, model parameters, credential-pool status, and usage charts remain in `ModelsView.vue`.

## Goals

- Support OpenAI-compatible, Anthropic, Ollama, and custom mapping providers through one four-step flow.
- Require a successful test of the current draft before create or update.
- Save configuration, credentials, catalog registration, and runtime client atomically.
- Display tested capabilities and discovered models before confirmation.
- Allow explicit manual model entry when discovery is unavailable or empty.
- Keep provider components independent from raw HTTP and legacy provider CRUD endpoints.
- Preserve backend validation as the security authority for URLs, headers, endpoints, and mapping paths.

## Non-Goals

- Adding a sidebar route.
- Redesigning task routing, usage charts, or model generation controls.
- Executing arbitrary request templates or scripts.
- Silently falling back to a different provider.
- Adding Local ORT to the provider onboarding wizard; Local ORT remains managed by the Local AI deployment flow.
- Preserving provider drag ordering unless the unified provider API gains an explicit ordering contract.

## Architecture

### Provider API Module

`web/frontend/src/api/providers.ts` owns the frontend contracts and HTTP calls:

- `ProviderProtocolInput`: `openai`, `anthropic`, `ollama`, `custom-map`.
- `ProviderProtocol`: backend canonical protocol values.
- `ProviderDraft`: identity, endpoint, authentication, defaults, capabilities, and optional custom mapping.
- `ProviderDefinition`: persisted provider data returned by the server.
- `CapabilityReport`: availability, capability booleans, discovered models, and a safe error string.
- Operations for list, test, create, update, delete, capabilities, and model discovery.

The shared API client continues to provide authentication, envelope unwrapping, renewal handling, and confirmation headers.

### Provider Store

`web/frontend/src/stores/providers.ts` is the single frontend authority for provider lifecycle state. It owns:

- The normalized provider collection.
- Loading, mutation, test, and error states.
- The current tested-draft fingerprint and capability report.
- List, test, create, update, delete, capability, and discovery actions.
- Draft invalidation whenever a field affecting connectivity or behavior changes.

Components call store actions and never import the raw HTTP helpers.

### Components

`ProviderWizard.vue` owns the four-step state machine and form composition.

1. Protocol selection.
2. Connection, authentication, endpoint, and default-model configuration.
3. Live draft test and optional manual model entry.
4. Capability review and atomic create or update confirmation.

`CapabilityMatrix.vue` renders tools, vision, streaming, model discovery, and JSON mode as explicit supported or unsupported states. It also presents availability and discovered models without interpreting a failed test as partial success.

`CustomMappingEditor.vue` edits structured values only:

- Chat and models endpoint paths.
- Authentication headers with supported placeholders.
- Request field mappings.
- Non-stream response field mappings.
- Stream field mappings.
- Model-list response path.

It does not accept executable code, arbitrary interpolation, absolute endpoint URLs, or unrestricted object paths.

### ModelsView Integration

`ModelsView.vue` consumes the provider store for list and lifecycle operations. The existing provider modal is replaced by `ProviderWizard`. Built-in providers remain visible but immutable. Existing task routes and unrelated page sections are not moved.

The page no longer uses legacy provider create, update, delete, key, or health-test endpoints. Provider ordering UI is removed if the new API has no supported ordering operation.

## Backend Contract Completion

The provider service accepts frontend aliases and normalizes them before validation:

- `openai` to `openai_compatible`.
- `custom-map` to `custom_mapping`.

Custom mapping fields are retained in provider metadata, restored from configuration, passed into `CustomMappingTransport`, and returned to the editor without exposing credentials. The transport receives validated endpoint paths, mapping sections, header templates, capabilities, and the default model.

The provider service applies the existing SSRF policy before testing or persistence. Local Ollama endpoints receive the same explicit local-service treatment as the established backend policy. Validation failures, duplicate IDs, immutable built-ins, missing providers, failed tests, and providers in use map to stable 4xx responses.

Credentials never appear in provider list, capability, model, or error responses. Empty credentials are permitted only when the selected protocol and authentication definition allow them.

Create and update continue to stage a transport test and runtime client before committing. Commit failures roll back configuration, credential storage, catalog state, and runtime client state.

## Data Flow

1. The user opens the wizard for a new or existing custom provider.
2. The wizard creates a local draft and invalidates any previous test fingerprint.
3. The store sends the draft and transient credentials to `/api/v1/providers/test`.
4. The backend validates the draft, constructs a temporary transport, performs the capability test, closes the transport, and returns a report.
5. A successful report is bound to a deterministic fingerprint of the tested draft.
6. Any subsequent relevant field change invalidates the report and disables saving.
7. The user reviews capabilities and discovered models, or explicitly supplies a manual model ID.
8. Create or update sends the current draft and credentials.
9. The backend repeats staging to prevent time-of-check/time-of-use drift, then commits atomically.
10. The store reloads the provider list and closes the wizard only after success.

## Error Handling

- Connection and validation failures remain in the wizard with entered non-secret values intact.
- Credentials remain local to the active form and are cleared when the wizard closes.
- A stale test report can never enable saving after the draft changes.
- Empty discovery results show manual model entry rather than inventing a model.
- Provider deletion requires confirmation and reports route references on conflict.
- Backend errors expose stable user-facing messages without credentials, raw response bodies, or stack traces.
- Failed create or update leaves the previous persisted and runtime state unchanged.

## Testing

`tests/test_frontend_provider_contracts.py` verifies:

- All four frontend protocol choices.
- Four wizard steps and component composition.
- Store-only component data access.
- Test-before-save enforcement.
- Draft fingerprint invalidation.
- Capability matrix fields.
- Manual model fallback.
- Structured custom mapping fields and the absence of executable template features.
- ModelsView integration and removal of legacy provider lifecycle calls.

Backend tests extend `tests/test_provider_onboarding.py` and transport coverage to verify:

- Protocol alias normalization.
- Custom mapping persistence and restoration.
- Custom mapping transport construction.
- Safe endpoint, path, header, and SSRF validation.
- Optional authentication behavior.
- Stable HTTP error mapping.
- Atomic rollback for create and update.
- Credential redaction from all responses.

Verification gates:

1. Focused provider backend tests.
2. Frontend provider contract tests.
3. Existing ModelsView and Local AI frontend contracts.
4. `npx vue-tsc --noEmit`.
5. `npm run build`.
6. Python formatting and diff checks applicable to changed files.
7. Independent code review with all critical and important findings resolved.

## Acceptance Criteria

- A user can create and edit each supported provider type from ModelsView.
- Saving is impossible until the exact current draft passes a real backend test.
- The review step shows the tested capability matrix and model result.
- Manual model entry is explicit and works when model discovery is unavailable.
- Custom mapping works through the real backend transport and survives restart.
- Failed saves do not leave partial credentials, configuration, catalog entries, or runtime clients.
- Provider UI code no longer uses legacy provider lifecycle endpoints.
- Focused tests, type checking, and production build exit successfully.
