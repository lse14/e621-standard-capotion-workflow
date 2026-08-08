# Global Native Path Picker Design

> Status: confirmed interaction design, pending written-spec review. 2026-08-08.

## Goal

Make every user-editable local filesystem path use one reusable control: a manually editable path input plus a Select button. A Select click opens a Windows-native chooser through the local backend and writes the confirmed absolute path back into that one field.

The confirmed scope is:

| Field | Picker purpose | Native selection |
| --- | --- | --- |
| Source dataset | `source_dataset` | Existing folder |
| Full-copy output dataset | `output_dataset` | Existing folder, including a folder created in the dialog |
| Custom replacement index | `replacement_csv` | One `.csv` file |

Future user-editable local filesystem paths reuse this control after registering an explicit purpose. `ResourcePicker.tsx` is excluded because it selects catalog resources already known to the application, not filesystem paths.

## Evidence And Boundary

- `frontend/src/components/steps/SetupStep.tsx` currently renders source and output roots as plain inputs.
- `frontend/src/components/steps/ReplaceStep.tsx` currently renders the custom replacement CSV path as a plain input.
- Those fields already require absolute Windows paths in `frontend/src/i18n.ts` and are validated later by preflight.
- `core/src/anima_core/api.py` composes routers from `ControlPlaneContext`; `api_application.py` already owns application-level UI actions such as shutdown.
- The project core interpreter successfully imports `tkinter 8.6`, including `filedialog.askdirectory` and `filedialog.askopenfilename`, with `-B -I`.

This is not a web-rendered Explorer clone and does not expose a filesystem listing API. The exact visual appearance is supplied by the installed Windows dialog, not CSS. The picker only returns a path the user explicitly selected. Existing path safety, source/output overlap checks, extension validation, preflight, workspace preparation, JobConfig, SQLite, workers, resource catalog, launcher, and backend lifecycle remain unchanged.

## Chosen Approach

The selected implementation uses a local control-plane route backed by a small `NativePathPicker` service.

Other approaches are intentionally excluded:

- Browser `<input type="file">` does not reliably return the absolute Windows path required by the current backend contract and cannot safely provide folder selection semantics here.
- A browser Explorer implementation would require arbitrary directory enumeration and would not be the native Windows chooser requested by the user.

## API And Service Contract

Add one strict application router endpoint:

```text
POST /api/application/select-path
```

Request body:

```json
{
  "purpose": "replacement_csv",
  "currentPath": "E:\\datasets\\rules.csv"
}
```

`purpose` is a closed enum: `source_dataset`, `output_dataset`, or `replacement_csv`. `currentPath` is nullable and bounded; it is used only as a best-effort initial location. The browser cannot supply arbitrary chooser modes, shell commands, file filters, or a destination path.

Successful response:

```json
{
  "cancelled": false,
  "path": "E:\\datasets\\rules.csv"
}
```

Cancellation is a normal success response with `cancelled: true` and `path: null`. A selected value is an absolute path; the existing field validator/preflight remains the authority for whether that path is acceptable for the selected workflow.

Errors are bounded and have stable semantics:

- malformed body or unsupported purpose: `400`;
- another chooser is open: `409 path_picker_busy`;
- the native dialog is unavailable or fails: `503 path_picker_unavailable`.

No exception message, local path, credential, raw Tcl/Tk error, or stack trace is returned on failure. The frontend leaves the typed field unchanged and shows a localized inline error.

### Native Picker Service

`NativePathPicker` is injected through `ControlPlaneContext` with an optional default in `build_control_app`, matching the project’s existing testable-service pattern. Its production implementation lazily imports `tkinter` only when a picker is invoked:

- `source_dataset` and `output_dataset` use `filedialog.askdirectory`.
- `replacement_csv` uses `filedialog.askopenfilename` with a CSV-only filter.
- A temporary hidden Tk root is created and destroyed in `finally` for every call, including cancellation and failure.
- A nonblocking process-local lock permits one modal dialog at a time. The route returns `409` rather than opening competing dialogs from another tab/click.
- The synchronous dialog executes outside the async event loop. It does not start a worker, retain a runtime resource, or alter backend shutdown behavior.
- For an existing current path, the service starts at that path or its parent. An invalid/nonexistent typed path falls back to the platform dialog’s normal location.
- The output-folder dialog may select an existing empty folder or one created through the system dialog. Manual entry remains available for an intentionally absent output path, which the existing preflight already supports.

The service is Windows-local behavior. If a packaged runtime lacks usable Tk support or has no interactive desktop session, it returns the stable unavailable error and manual typing remains fully functional.

## Frontend Contract

Create a focused reusable `PathPicker` field component. It owns only UI interaction and receives a value, disabled state, picker purpose, localized labels, and an `onChange` callback.

- The input preserves the surrounding field's existing disabled condition; otherwise it remains manually editable.
- The Select button is a clear command control with an accessible name and pending state. It is disabled whenever the field is disabled or while that picker request is pending.
- One request can update only the originating field. Cancellation and request failure do not clear or overwrite any value.
- Source and output uses pass their respective directory purposes; Replace’s custom index path passes `replacement_csv`.
- Existing `FormField` labels, help buttons, validation, task locking, form-grid behavior, translations, and responsive layout remain intact. The input/button row uses stable `minmax(0, 1fr) auto` sizing so the button cannot resize the field or create mobile overflow.
- `frontend/src/api.ts` exposes a narrow typed `selectLocalPath` function. The browser mock returns fixture selections and never launches a real OS dialog.

## Security And State Rules

- The route has no directory-list, search, rename, delete, create-file, upload, download, or arbitrary command capability.
- The user initiates every modal dialog through an explicit Select click. The server returns only the final selected absolute path or cancellation.
- The route does not write to the selected path, inspect its content, or bypass existing preflight/path-safety checks.
- Current path text is never persisted by the picker endpoint. It remains draft state controlled by the existing field handlers.
- A task created before later field edits retains its existing frozen config behavior; the picker only changes the draft before preflight/task creation.

## Testing And Acceptance

1. Unit tests inject a fake picker and cover each allowed purpose, matching dialog request configuration, cancellation, selected absolute path, invalid body, busy conflict, unavailable error, and temporary-root cleanup paths without opening a real dialog.
2. API/router inventory tests cover the strict request/response shape and the single new application route. Existing routes remain unchanged.
3. Frontend unit/static tests verify all three current path inputs use the reusable component and no duplicate chooser implementation appears.
4. Local-mock Playwright tests click each Select button, verify the correct API purpose and field update, verify cancellation keeps the prior value, and verify locked/disabled fields cannot invoke the route. Desktop and narrow layout checks confirm the input/button row has no overflow.
5. The project core runtime is checked with `-B -I` for `tkinter.filedialog` availability. A manual Windows acceptance pass selects a source folder, output folder, and CSV file, then cancels each dialog once; it also verifies that preflight remains the only path-validation authority.
6. No live provider, external download, real dataset mutation, worker execution, stress suite, or system Python/Node is used. Temporary browser artifacts and any visual-companion files/processes are cleaned after the completed implementation task.

## Acceptance

- Every current user-editable filesystem path field has a path input and Select button with the correct native chooser type.
- The source/output fields select folders; the replacement field selects one CSV file; manual typing remains available.
- Cancel, busy, and unavailable states never overwrite a field or leave a hidden Tk resource behind.
- The browser never receives arbitrary filesystem listings, and existing preflight validation still determines whether a selected path can be used.
- The implementation adds no dependency and changes no task, worker, database, resource, or launcher contract.
