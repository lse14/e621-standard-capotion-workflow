# Unified NL Presets And API UI Design

> Status: confirmed interaction design, pending written-spec review. 2026-08-08.

## Goal

Replace the split "caption preset" and diagnostic-only "prompt preset" concepts with one visible NL preset library. Put every API-related control in one cohesive NL-page area. Remove the user-supplement input from the new task path.

The user confirmed the following interaction decisions:

- General, Style, and Character are the three visible built-in preset types.
- Built-in presets are editable and can be reset to their packaged defaults.
- New custom presets require a General, Style, or Character type; custom name, type, and prompt text remain editable and custom presets can be deleted.
- The API area groups profile, endpoint, models, credentials, request limits, discovery, test, and feedback. When API generation is disabled, this area is collapsed but its configuration is retained.

## Evidence And Boundary

- `frontend/src/components/steps/NlStep.tsx` currently separates routing, a `systemPrompt` supplement, request limits, and profile fields.
- `frontend/src/components/NlApiTools.tsx` currently owns a second preset editor plus diagnostics; its preset cannot change production captions.
- `core/src/anima_core/nl_profiles.py` already defines the packaged v4 General, Style, and Character resource IDs.
- `core/src/anima_core/nl_prompt_presets.py` persists only diagnostic `basePrompt` records with no type and treats its one built-in record as immutable.
- v4 tasks still send `captionPreset`, `primaryCharacterName`, and `userSupplement` through `core/src/anima_core/nl_runner.py` to `workers/nl/src/anima_nl_worker/worker.py`.
- `contracts/schemas/job-config-v8.schema.json` and `contracts/schemas/nl-worker-v1.schema.json` preserve the old v4 shape. They must not be changed semantically.

This design creates a new NL compatibility path for a new JobConfig v9 only. v2-v8 canonical bytes/hashes, old jobs, SQLite schema, existing worker message branches, Token Budget behavior, OCR behavior, launcher behavior, and backend-process lifecycle are out of scope.

## User Experience

### NL Page Layout

The NL page has three ordered areas:

1. Generation switches remain compact at the top.
2. **Prompt presets** is a library-plus-editor surface. The left side lists built-in and custom entries; the right side edits the selected entry. Short/medium/long distribution remains immediately below this editor because it is NL task behavior, not a Token Budget setting.
3. **API connection and diagnostics** is one contiguous area containing all API controls. It is expanded while API generation is enabled and collapsed while disabled. Collapse only changes presentation: endpoint, models, credentials, limits, profile, and selected values stay in the draft/profile state and return unchanged when re-enabled.

At narrow widths, each area stacks in that same order. There is no separate prompt-management page or detached diagnostics widget.

### Preset Library

Each visible preset has:

```ts
type NlPresetType = "general" | "style" | "character";

type NlPromptPreset = {
  presetId: string;
  name: string;
  type: NlPresetType;
  promptText: string;
  builtIn: boolean;
  sha256: string;
  sizeBytes: number;
};
```

Rules:

- The built-ins have stable IDs, labels, and types: General, Style, and Character. Only their prompt text can be saved as a local override. Reset removes that override and restores the packaged text.
- New custom presets require a nonblank name, nonblank prompt text, and an explicit type selection. The Create action stays disabled until all three are valid.
- Custom presets allow later name, type, and text edits. Delete requires confirmation.
- The currently selected preset is the only editable user-prompt source for both task creation and API test. There is no second caption-preset list, base-prompt editor, or user-supplement textarea.
- Type groups the library and provides stable structured context behavior. It does not add another hidden textual prompt. For Character, the worker continues to receive the existing per-sample primary-character context; General and Style receive `null` there.
- Deleting the selected custom preset switches the editable draft to built-in General. Queued and running tasks retain their already frozen prompt.

### API Connection And Diagnostics

The one API area contains, in visual order:

1. profile selection and save;
2. endpoint, primary model, optional backup model, credential reference, and transient API-key input;
3. concurrency, requests-per-minute, attempt budget, and backup toggle;
4. model discovery, discovered-model selector, Send Test command, and structured feedback.

Model discovery remains usable with the current unsaved connection fields. A test requires a saved selected preset; while the preset editor is dirty, the test command explains that the preset must be saved first. The diagnostic request receives the selected preset ID, resolves the same saved text server-side, and uses the v9 prompt composer with a fixed safe diagnostic context and the medium length tier. It therefore tests the same prompt source and protocol family as a v9 task without introducing a second editable prompt.

API disable/collapse never starts, stops, or hides the local backend process. It only prevents API-dependent interactions and task traffic while preserving configuration.

## Persistence And Migration

The existing local prompt store moves from schema version 1 to version 2:

```json
{
  "schemaVersion": 2,
  "builtInOverrides": [
    {
      "presetId": "builtin:nl-preset-v1-general",
      "promptText": "Saved local override"
    }
  ],
  "customPresets": [
    {
      "presetId": "custom:0123456789abcdef0123456789abcdef",
      "name": "Cinematic style",
      "type": "style",
      "promptText": "Describe cinematic composition."
    }
  ]
}
```

The built-in values are reconstructed from packaged, verified resources plus optional local overrides. A reset deletes only the matching override. Custom records retain stable IDs.

Existing v1 custom `{presetId, name, basePrompt}` records migrate atomically to v2 custom records with `type: "general"` and `promptText: basePrompt`; no user text is discarded. The old built-in diagnostic record is not persisted and is replaced by the three packaged v9 built-ins. Store bounds remain conservative: valid UTF-8/no NUL, name at most 256 UTF-8 bytes, prompt text at most 65,536 UTF-8 bytes, at most 100 custom records, and at most 8 MiB serialized data. Malformed stores fail closed and are never silently repaired.

Legacy profile persistence remains readable for v2-v8. The new visible profile form removes `systemPrompt`; v9 never reads it. Compatibility code may retain legacy data only long enough to load old profile shapes, not to expose or append it to v9 prompt composition.

## New Task And Worker Contract

JobConfig v9 freezes the selected preset into the task configuration rather than storing a mutable library reference:

```json
{
  "nl": {
    "promptVersion": "nl-preset-library-v1",
    "promptPreset": {
      "presetId": "custom:0123456789abcdef0123456789abcdef",
      "name": "Cinematic style",
      "type": "style",
      "promptText": "Describe cinematic composition.",
      "sha256": "c518b86b9dd6bd68e0379c9e59ef6a4e302ecaf83ddf4387eef7f88031156532"
    },
    "lengthDistribution": {"short": 33, "medium": 34, "long": 33},
    "lengthSeed": "frozen-v9-example-seed"
  }
}
```

`captionPreset` and `systemPrompt` are absent from the v9 NL object. The v9 worker message branch carries the frozen `promptText`, preset type, selected length tier, and structured per-sample context. It uses a new immutable v9 protocol resource plus the frozen preset text; the resource explicitly contains no user-supplement layer. The selected type may select structured `primaryCharacterName` context, but it never causes a second user-visible prompt fragment to be appended.

The worker must not consult the mutable local preset store during execution. Editing or deleting a library entry after task creation cannot change an existing job. v2-v8 retain their current v4 composition and worker schema branches unchanged.

## Errors And State Changes

- Invalid create/update bodies receive strict validation errors and leave the selected local editor state intact.
- Attempting to delete or change a built-in type fails locally and on the API with a stable conflict response.
- Resetting a built-in with an unsaved edit asks for confirmation; a successful reset selects the packaged value and clears only its local override.
- Switching presets with dirty changes asks for confirmation. API test stays disabled while that prompt is dirty, avoiding ambiguity about which text was tested.
- API discovery/test failures retain typed connection values and show bounded sanitized feedback. No transient key is persisted or echoed.
- An API-disabled section cannot invoke discovery or test. Re-enabling restores the preserved configuration and expands the section.

## Implementation Ownership

- **Frontend:** `NlStep.tsx` becomes the composition owner for the three visual areas. A focused preset-library component owns selection, editor state, CRUD, reset, and dirty handling. API controls remain a focused component but no longer own an independent prompt editor. `App.tsx` passes narrow callbacks only; `api.ts`, local mocks, i18n, and CSS update to the new contract.
- **Core API/store:** `nl_prompt_presets.py`, `api_models.py`, and `api_nl.py` own typed preset persistence, v1-to-v2 migration, built-in overrides/reset, and diagnostic request validation. Diagnostics resolve a saved preset ID rather than accepting an independent prompt text body.
- **Task/runtime:** a v9 schema and centralized capability helper define the new task path. `nl_runner.py`, worker schemas/validation, `worker.py`, prompt resources, and runtime synchronization add a new explicit v9 branch only.
- **Documentation:** `ROADMAP.md`, `MEMORY.md`, the implementation plan, resource manifests, and user-facing guidance record the new version boundary.

## Verification

1. Contract tests prove every v2-v8 schema bytes/hash remains unchanged and v9 strictly rejects the old supplement/routing fields.
2. Store/API tests cover default visibility, default edit/save/reset, required custom type, custom create/edit/type-change/delete, v1 migration, invalid data, and atomic persistence.
3. Runner/worker tests prove v9 freezes prompt text, honors General/Style/Character structured context, has no user supplement, and never reads mutable preset storage during execution.
4. Diagnostic tests prove the selected saved preset and v9 composer are used, with one bounded local-mock request and no secret persistence or echo.
5. Browser tests cover desktop and narrow layouts, all built-ins visible, API controls grouped, API disabled/collapsed with restored values after re-enable, and the absence of the former user-supplement/independent-prompt controls.
6. Use only project-local Python with `-B -I`, project-local Node/Chromium, and local API mocks. Run focused tests, typecheck, build, relevant full suites, runtime sync/drift validation for worker/resource changes, and clean up temporary test artifacts and visual-companion files/processes at completion.

## Acceptance

- A user can see, edit, save, reset, create, change type, and delete exactly the intended preset records without encountering a separate caption or diagnostic prompt concept.
- Every new custom preset requires General, Style, or Character, and a saved custom type can later be corrected.
- The selected one preset supplies both v9 production tasks and API diagnostics; the fixed protocol remains internal and no user supplement exists in v9.
- All API controls are together and collapse without data loss when API generation is disabled.
- Existing jobs and v2-v8 contracts behave exactly as before.
