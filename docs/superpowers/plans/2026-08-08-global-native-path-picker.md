# Global Native Path Picker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one reusable path-input-plus-Select control whose explicit user action opens the appropriate Windows-native directory or CSV dialog through the local control plane.

**Architecture:** A small injected Core service owns the only Tk interaction, including a process-local modal lock and deterministic cleanup of each temporary Tk root. One strict application route exposes only three fixed chooser purposes. The React component owns request/pending/error UI; existing steps retain draft ownership and preflight invalidation, so manually typed paths and all path validation semantics stay unchanged.

**Tech Stack:** Python 3 embedded runtime, FastAPI, Pydantic v2, Tkinter filedialog, React 19, TypeScript, Playwright, unittest.

---

## File Structure

- Create: `core/src/anima_core/native_path_picker.py` - lazy Windows-native chooser service, purpose enum, lock, and stable errors.
- Create: `tests/unit/test_native_path_picker.py` - fake-Tk unit tests that never show a desktop dialog.
- Modify: `core/src/anima_core/api_context.py` - inject the picker into `ControlPlaneContext`.
- Modify: `core/src/anima_core/api_models.py` - strict select-path parser and bounded request body.
- Modify: `core/src/anima_core/api_application.py` - application-level `POST /api/application/select-path` route.
- Modify: `core/src/anima_core/api.py` - construct the default picker, accept test injection, and retain facade imports.
- Modify: `tests/unit/test_api.py` - route contract and injected-fake tests.
- Modify: `tests/contract/test_core_module_decomposition.py` - API model and route inventories.
- Create: `frontend/src/components/PathPicker.tsx` - reusable input/button row and local request state.
- Modify: `frontend/src/api.ts` - typed select-path client request/response.
- Modify: `frontend/src/components/steps/SetupStep.tsx` - use the reusable picker for source and full-copy output roots.
- Modify: `frontend/src/components/steps/ReplaceStep.tsx` - use it for the custom replacement CSV field.
- Modify: `frontend/src/App.tsx` - pass localized picker copy while keeping draft/preflight ownership in the page.
- Modify: `frontend/src/i18n.ts`, `frontend/src/appCopy.ts`, `frontend/src/styles.css` - bilingual state copy and non-overflowing input/button layout.
- Modify: `frontend/tests/e2e/mockApi.ts` - deterministic local route fixture for selected paths, cancellation, and failure.
- Create: `frontend/tests/e2e/path-picker.spec.ts` - rendered interaction, disabled-state, cancellation, and narrow-layout tests.
- Modify: `tests/contract/test_frontend_module_decomposition.py` - assert the dedicated component owns network interaction and steps remain presentational.
- Modify: `ROADMAP.md`, `MEMORY.md` - record completion and fresh verification evidence after the implementation gates pass.

### Task 1: Native Picker Service

**Files:**
- Create: `tests/unit/test_native_path_picker.py`
- Create: `core/src/anima_core/native_path_picker.py`

- [ ] **Step 1: Write failing fake-Tk tests for all chooser outcomes.**

  Add local fakes that record `Tk()`, `withdraw()`, `destroy()`, `askdirectory()`, and `askopenfilename()` calls, then add the following concrete assertions. The tests must pass a loader into `NativePathPicker`; they must never import real `tkinter` or display a dialog.

  ```python
  class NativePathPickerTests(unittest.TestCase):
      def test_source_and_output_use_directory_dialog_with_distinct_mustexist(self) -> None:
          dialog = FakeFileDialog(directory_result="E:\\picked")
          picker = NativePathPicker(tk_loader=lambda: (FakeTk(), dialog))
          self.assertEqual("E:\\picked", picker.select("source_dataset", "E:\\typed\\source"))
          self.assertEqual("E:\\picked", picker.select("output_dataset", "E:\\typed\\output"))
          self.assertEqual([True, False], [call["mustexist"] for call in dialog.directory_calls])

      def test_replacement_uses_csv_file_filter_and_cancellation_is_none(self) -> None:
          dialog = FakeFileDialog(file_result="")
          root = FakeRoot()
          picker = NativePathPicker(tk_loader=lambda: (FakeTk(root), dialog))
          self.assertIsNone(picker.select("replacement_csv", "E:\\rules\\replace.csv"))
          self.assertEqual((("CSV files", "*.csv"),), dialog.file_calls[0]["filetypes"])
          self.assertTrue(root.withdrawn)
          self.assertTrue(root.destroyed)

      def test_invalid_purpose_busy_and_loader_failure_have_stable_errors(self) -> None:
          picker = NativePathPicker(tk_loader=lambda: (_ for _ in ()).throw(ImportError("missing")))
          with self.assertRaises(ValueError):
              picker.select("other", None)  # type: ignore[arg-type]
          with self.assertRaises(NativePathPickerUnavailableError):
              picker.select("source_dataset", None)
          busy = NativePathPicker(tk_loader=lambda: (FakeTk(), FakeFileDialog()))
          self.assertTrue(busy._dialog_lock.acquire(blocking=False))
          try:
              with self.assertRaises(NativePathPickerBusyError):
                  busy.select("source_dataset", None)
          finally:
              busy._dialog_lock.release()

      def test_dialog_failure_destroys_root_before_reporting_unavailable(self) -> None:
          root = FakeRoot()
          picker = NativePathPicker(tk_loader=lambda: (FakeTk(root), FakeFileDialog(raises=RuntimeError("tcl"))))
          with self.assertRaises(NativePathPickerUnavailableError):
              picker.select("replacement_csv", None)
          self.assertTrue(root.destroyed)
  ```

- [ ] **Step 2: Run the focused test to record RED.**

  Run:

  ```powershell
  & .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\unit -t . -p 'test_native_path_picker.py' -v
  ```

  Expected: import failure for `anima_core.native_path_picker` or its missing `NativePathPicker` symbols.

- [ ] **Step 3: Implement the minimal lazy, locked service.**

  Create `core/src/anima_core/native_path_picker.py` with exactly the closed purpose surface and only lazy Tk imports. `select()` must acquire the lock before loading Tk, return `None` for a cancelled native dialog, and always destroy a created root and release the lock.

  ```python
  from __future__ import annotations

  import os
  from threading import Lock
  from typing import Callable, Literal

  PathPickerPurpose = Literal["source_dataset", "output_dataset", "replacement_csv"]
  TkLoader = Callable[[], tuple[object, object]]


  class NativePathPickerBusyError(RuntimeError):
      pass


  class NativePathPickerUnavailableError(RuntimeError):
      pass


  def _load_tk() -> tuple[object, object]:
      import tkinter
      from tkinter import filedialog
      return tkinter, filedialog


  class NativePathPicker:
      def __init__(self, *, tk_loader: TkLoader | None = None) -> None:
          self._tk_loader = tk_loader or _load_tk
          self._dialog_lock = Lock()

      def select(self, purpose: PathPickerPurpose, current_path: str | None) -> str | None:
          if purpose not in ("source_dataset", "output_dataset", "replacement_csv"):
              raise ValueError("invalid path picker purpose")
          if not self._dialog_lock.acquire(blocking=False):
              raise NativePathPickerBusyError("path picker is busy")
          root: object | None = None
          try:
              tkinter, filedialog = self._tk_loader()
              root = tkinter.Tk()  # type: ignore[attr-defined]
              root.withdraw()  # type: ignore[attr-defined]
              initialdir = self._initialdir(purpose, current_path)
              if purpose == "replacement_csv":
                  selected = filedialog.askopenfilename(  # type: ignore[attr-defined]
                      parent=root, initialdir=initialdir, title="Select replacement CSV",
                      filetypes=(("CSV files", "*.csv"),),
                  )
              else:
                  selected = filedialog.askdirectory(  # type: ignore[attr-defined]
                      parent=root, initialdir=initialdir, title="Select dataset folder",
                      mustexist=purpose == "source_dataset",
                  )
              return str(selected) or None
          except NativePathPickerBusyError:
              raise
          except Exception as exc:
              raise NativePathPickerUnavailableError("native path picker unavailable") from exc
          finally:
              if root is not None:
                  try:
                      root.destroy()  # type: ignore[attr-defined]
                  except Exception:
                      pass
              self._dialog_lock.release()

      @staticmethod
      def _initialdir(purpose: PathPickerPurpose, current_path: str | None) -> str | None:
          if not current_path:
              return None
          candidate = os.path.normpath(current_path)
          if purpose == "replacement_csv" or not os.path.isdir(candidate):
              parent = os.path.dirname(candidate)
              return parent or None
          return candidate
  ```

  Do not add filesystem listing, selected-path validation, persistence, subprocesses, or any non-Tk dependency. The dialog filter is UI guidance; existing preflight remains the only path-policy authority.

- [ ] **Step 4: Run the focused service test to verify GREEN.**

  Run the command from Step 2.

  Expected: every fake-dialog test passes, including cancellation, `mustexist`, CSV filter, lock, unavailable error, and root cleanup.

- [ ] **Step 5: Commit the isolated service and tests.**

  ```powershell
  git add core/src/anima_core/native_path_picker.py tests/unit/test_native_path_picker.py
  git commit -m "feat: add native path picker service"
  ```

### Task 2: Strict Application Route And Injection

**Files:**
- Modify: `core/src/anima_core/api_models.py`
- Modify: `core/src/anima_core/api_context.py`
- Modify: `core/src/anima_core/api_application.py`
- Modify: `core/src/anima_core/api.py`
- Modify: `tests/unit/test_api.py`
- Modify: `tests/contract/test_core_module_decomposition.py`

- [ ] **Step 1: Write failing API tests and frozen inventory changes.**

  Add a fake picker with `calls: list[tuple[str, str | None]]` and `select()` returning a configured value or raising one of the native errors. Extend the route inventory with the exact new route and build parameter, then call the endpoint directly with raw bodies:

  ```python
  class FakeNativePathPicker:
      def __init__(self, result: str | None = "E:\\picked") -> None:
          self.result = result
          self.calls: list[tuple[str, str | None]] = []

      def select(self, purpose: str, current_path: str | None) -> str | None:
          self.calls.append((purpose, current_path))
          return self.result

  def test_select_path_is_purpose_limited_and_cancellation_does_not_mutate_state(self) -> None:
      picker = FakeNativePathPicker(result=None)
      app = build_control_app(
          database_path=self.database_path, profile_store=self.profiles,
          credential_store=self.credentials, preparation_service=self.preparation,
          native_path_picker=picker,
      )
      endpoint = _endpoint(app, "/api/application/select-path", "POST")
      self.assertEqual({"cancelled": True, "path": None}, endpoint({"purpose": "output_dataset", "currentPath": "E:\\old"}))
      self.assertEqual([("output_dataset", "E:\\old")], picker.calls)
      for invalid in ({}, {"purpose": "shell", "currentPath": None}, {"purpose": "source_dataset", "extra": True}):
          with self.assertRaises(HTTPException) as rejected:
              endpoint(invalid)
          self.assertEqual(400, rejected.exception.status_code)

  def test_select_path_maps_busy_and_unavailable_without_leaking_native_error(self) -> None:
      for error, status, detail in (
          (NativePathPickerBusyError("dialog already open"), 409, "path_picker_busy"),
          (NativePathPickerUnavailableError("raw Tcl failure"), 503, "path_picker_unavailable"),
      ):
          class RaisingNativePathPicker:
              def select(self, purpose: str, current_path: str | None) -> str | None:
                  raise error
          app = build_control_app(
              database_path=self.database_path, profile_store=self.profiles,
              credential_store=self.credentials, preparation_service=self.preparation,
              native_path_picker=RaisingNativePathPicker(),  # type: ignore[arg-type]
          )
          endpoint = _endpoint(app, "/api/application/select-path", "POST")
          with self.assertRaises(HTTPException) as rejected:
              endpoint({"purpose": "source_dataset", "currentPath": "E:\\typed"})
          self.assertEqual((status, detail), (rejected.exception.status_code, rejected.exception.detail))
  ```

  Update `EXPECTED_BUILD_PARAMETERS`, `EXPECTED_CONTROL_ROUTES`, `API_REQUEST_MODELS`, `API_MODEL_CLASSES`, and `API_APPLICATION_ROUTES` so those contracts fail until the route and parser are present.

- [ ] **Step 2: Run focused API and decomposition tests to record RED.**

  Run:

  ```powershell
  & .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\unit -t . -p 'test_api.py' -v
  & .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\contract -t . -p 'test_core_module_decomposition.py' -v
  ```

  Expected: the new parameter/route/model inventory and `select-path` endpoint assertions fail.

- [ ] **Step 3: Add the parser, injectable context field, and application route.**

  Add this bounded parser to `api_models.py`; manual parsing deliberately converts all malformed bodies from FastAPI's default `422` into the agreed `400` response.

  ```python
  class _SelectPathBody(BaseModel):
      model_config = ConfigDict(extra="forbid", strict=True)
      purpose: Literal["source_dataset", "output_dataset", "replacement_csv"]
      currentPath: str | None = Field(default=None, max_length=32_767)


  @dataclass(frozen=True)
  class SelectPathRequest:
      purpose: Literal["source_dataset", "output_dataset", "replacement_csv"]
      current_path: str | None


  def parse_select_path_body(value: object) -> SelectPathRequest:
      body = _SelectPathBody.model_validate(value)
      return SelectPathRequest(body.purpose, body.currentPath)
  ```

  Add `native_path_picker: NativePathPicker` to `ControlPlaneContext`, append `native_path_picker: NativePathPicker | None = None` to `build_control_app`, construct `native_path_picker = native_path_picker or NativePathPicker()`, and pass it into the context. Keep the default construction inert: it must not import Tk or retain a GUI resource.

  Add this route to `build_application_router`; catch `ValidationError` around the parser so unsupported/malformed bodies are `400`, and do not return any native exception text.

  ```python
  @router.post("/api/application/select-path")
  def select_path(body: object = Body()) -> dict[str, object]:
      try:
          request = parse_select_path_body(body)
      except ValidationError as exc:
          raise HTTPException(status_code=400, detail="invalid_path_picker_request") from exc
      try:
          selected = context.native_path_picker.select(request.purpose, request.current_path)
      except NativePathPickerBusyError as exc:
          raise HTTPException(status_code=409, detail="path_picker_busy") from exc
      except NativePathPickerUnavailableError as exc:
          raise HTTPException(status_code=503, detail="path_picker_unavailable") from exc
      return {"cancelled": selected is None, "path": selected}
  ```

  Import `Body` and `ValidationError` locally in the owner modules as needed. Re-export `_SelectPathBody`, `SelectPathRequest`, and `parse_select_path_body` through the existing `api.py` facade import block only if the current test surface requires it. Do not add a route to any jobs, NL, resource, worker, or database module.

- [ ] **Step 4: Run the API and decomposition tests to verify GREEN.**

  Run the commands from Step 2.

  Expected: selected and cancelled responses match exactly; invalid purpose/body is `400`; busy is `409`; unavailable is `503`; existing route inventory remains otherwise unchanged.

- [ ] **Step 5: Commit the Core API slice.**

  ```powershell
  git add core/src/anima_core/api_context.py core/src/anima_core/api_models.py core/src/anima_core/api_application.py core/src/anima_core/api.py tests/unit/test_api.py tests/contract/test_core_module_decomposition.py
  git commit -m "feat: expose native path picker route"
  ```

### Task 3: Typed Frontend Picker Component

**Files:**
- Modify: `frontend/src/api.ts`
- Create: `frontend/src/components/PathPicker.tsx`
- Modify: `frontend/src/i18n.ts`
- Modify: `frontend/src/appCopy.ts`
- Modify: `frontend/src/styles.css`
- Modify: `tests/contract/test_frontend_module_decomposition.py`

- [ ] **Step 1: Add a failing static component-ownership contract.**

  Require the new component to be the only shared UI owner that imports `selectLocalPath`; ensure it exports an explicit props type, contains the `path-picker` marker, and that neither `SetupStep.tsx` nor `ReplaceStep.tsx` imports `../../api`.

  ```python
  def test_path_picker_owns_the_local_path_request(self) -> None:
      component = (FRONTEND_SOURCE / "components" / "PathPicker.tsx").read_text(encoding="utf-8")
      self.assertIn("export type PathPickerProps", component)
      self.assertIn("export function PathPicker", component)
      self.assertIn('className="path-picker"', component)
      self.assertIn('from "../api"', component)
      self.assertIn("selectLocalPath", component)
      for filename in ("SetupStep.tsx", "ReplaceStep.tsx"):
          self.assertNotIn('from "../../api"', (FRONTEND_SOURCE / "components" / "steps" / filename).read_text(encoding="utf-8"))
  ```

- [ ] **Step 2: Run the frontend contract to record RED.**

  Run:

  ```powershell
  & .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\contract -t . -p 'test_frontend_module_decomposition.py' -v
  ```

  Expected: `PathPicker.tsx` is missing.

- [ ] **Step 3: Implement the typed client and component with field-local state.**

  Add the following API contract to `frontend/src/api.ts` next to the other application APIs:

  ```ts
  export type PathPickerPurpose = "source_dataset" | "output_dataset" | "replacement_csv";
  export type SelectLocalPathResponse = { cancelled: boolean; path: string | null };

  export function selectLocalPath(purpose: PathPickerPurpose, currentPath: string | null): Promise<SelectLocalPathResponse> {
    return request("/api/application/select-path", {
      method: "POST",
      body: JSON.stringify({ purpose, currentPath }),
    });
  }
  ```

  Create `PathPicker.tsx`. It must render the editable text input and a visible `button type="button"`, retain the current value on cancellation/error, and map only stable API statuses to localized text. The parent continues to own the actual draft update.

  ```tsx
  import { useState } from "react";
  import { ApiError, selectLocalPath, type PathPickerPurpose } from "../api";

  export type PathPickerProps = {
    id: string;
    value: string;
    purpose: PathPickerPurpose;
    disabled: boolean;
    placeholder: string;
    selectLabel: string;
    selectingLabel: string;
    busyMessage: string;
    unavailableMessage: string;
    failedMessage: string;
    onChange: (value: string) => void;
  };

  export function PathPicker(props: PathPickerProps) {
    const [pending, setPending] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const select = async () => {
      if (props.disabled || pending) return;
      setPending(true);
      setError(null);
      try {
        const result = await selectLocalPath(props.purpose, props.value || null);
        if (!result.cancelled && result.path) props.onChange(result.path);
      } catch (cause) {
        setError(cause instanceof ApiError && cause.status === 409 ? props.busyMessage
          : cause instanceof ApiError && cause.status === 503 ? props.unavailableMessage : props.failedMessage);
      } finally {
        setPending(false);
      }
    };
    return <div className="path-picker">
      <div className="path-picker-control">
        <input id={props.id} disabled={props.disabled} value={props.value} onChange={(event) => props.onChange(event.target.value)} placeholder={props.placeholder} />
        <button type="button" disabled={props.disabled || pending} aria-busy={pending} onClick={() => void select()}>{pending ? props.selectingLabel : props.selectLabel}</button>
      </div>
      {error && <small role="alert">{error}</small>}
    </div>;
  }
  ```

  Add `selectPath`, `selectingPath`, `pathPickerBusy`, `pathPickerUnavailable`, and `pathPickerFailed` to both language records in `i18n.ts`. Put no feature explanation in `appCopy.ts`; only add a short per-field picker label if the step prop shape needs it. Add CSS only for stable layout:

  ```css
  .path-picker { display: grid; gap: 6px; min-width: 0; }
  .path-picker-control { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; min-width: 0; }
  .path-picker-control > input { min-width: 0; width: 100%; }
  .path-picker-control > button { min-height: 36px; white-space: nowrap; }
  .path-picker small { color: #9d3d32; font-size: 12px; line-height: 1.4; }
  ```

  At `max-width: 400px`, retain the same two columns so the Select button stays fixed and the input consumes the remaining width; do not create a broad card or a custom file browser.

- [ ] **Step 4: Run the static contract and frontend typecheck to verify GREEN.**

  Run:

  ```powershell
  & .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\contract -t . -p 'test_frontend_module_decomposition.py' -v
  $nodeRoot = (Resolve-Path '.\.toolchains\node-v24.18.0-win-x64').Path
  $savedPath = $env:PATH
  try { $env:PATH = "$nodeRoot;$savedPath"; Push-Location frontend; & "$nodeRoot\npm.cmd" run typecheck } finally { Pop-Location; $env:PATH = $savedPath }
  ```

  Expected: the ownership contract passes and TypeScript reports no errors.

- [ ] **Step 5: Commit the shared frontend primitive.**

  ```powershell
  git add frontend/src/api.ts frontend/src/components/PathPicker.tsx frontend/src/i18n.ts frontend/src/appCopy.ts frontend/src/styles.css tests/contract/test_frontend_module_decomposition.py
  git commit -m "feat: add reusable path picker control"
  ```

### Task 4: Connect All Current Local Path Fields And Rendered Tests

**Files:**
- Modify: `frontend/src/components/steps/SetupStep.tsx`
- Modify: `frontend/src/components/steps/ReplaceStep.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/tests/e2e/mockApi.ts`
- Create: `frontend/tests/e2e/path-picker.spec.ts`

- [ ] **Step 1: Write failing local-mock Playwright coverage.**

  Add a `selectedPaths` map to `ApiScenario`, keyed by the three exact purposes. Handle `POST /api/application/select-path` by returning `{ cancelled: true, path: null }` when the configured entry is `null`, otherwise `{ cancelled: false, path: configuredPath }`. The existing mutation recorder must preserve the purpose/currentPath payload.

  ```ts
  test("selects the correct native purpose without replacing a value on cancellation", async ({ page, api }) => {
    api.selectedPaths.source_dataset = "E:\\picked\\source";
    api.selectedPaths.output_dataset = "E:\\picked\\output";
    api.selectedPaths.replacement_csv = null;
    await openApp(page, { language: "en" });
    await page.getByRole("button", { name: "Select path", exact: true }).first().click();
    await expect(page.getByLabel("Source dataset", { exact: true })).toHaveValue("E:\\picked\\source");
    await page.getByLabel("Work mode", { exact: true }).selectOption("full_copy");
    await page.getByRole("button", { name: "Select path", exact: true }).nth(1).click();
    await expect(page.getByLabel("Output dataset", { exact: true })).toHaveValue("E:\\picked\\output");
    await page.locator(".workflow-rail").getByRole("button", { name: /Replace/ }).click();
    await page.getByLabel("Index source", { exact: true }).selectOption("custom");
    const custom = page.getByLabel("Custom index path", { exact: true });
    await custom.fill("E:\\typed\\keep.csv");
    await page.getByRole("button", { name: "Select path", exact: true }).click();
    await expect(custom).toHaveValue("E:\\typed\\keep.csv");
    expect(mutationsFor(api, "POST", "/api/application/select-path").map((item) => item.body)).toEqual([
      { purpose: "source_dataset", currentPath: null },
      { purpose: "output_dataset", currentPath: null },
      { purpose: "replacement_csv", currentPath: "E:\\typed\\keep.csv" },
    ]);
  });

  test("disables path picker buttons for locked fields and keeps the narrow control inside the viewport", async ({ page, api }) => {
    setJobSnapshot(api, makeSnapshot({ status: "running", currentModuleId: "caption" }));
    await openApp(page, { language: "en" });
    await page.setViewportSize({ width: 320, height: 844 });
    const sourcePicker = page.locator("#setup-source-dataset").locator("xpath=ancestor::div[contains(@class, 'path-picker')][1]");
    await expect(sourcePicker).toBeVisible();
    expect(await sourcePicker.evaluate((element) => element.getBoundingClientRect().right <= window.innerWidth)).toBeTruthy();
    const sourceSelect = sourcePicker.getByRole("button", { name: "Select path", exact: true });
    await expect(sourceSelect).toBeDisabled();
    expect(mutationsFor(api, "POST", "/api/application/select-path")).toHaveLength(0);
  });
  ```

- [ ] **Step 2: Run the new E2E file to record RED.**

  Run:

  ```powershell
  $nodeRoot = (Resolve-Path '.\.toolchains\node-v24.18.0-win-x64').Path
  $savedPath = $env:PATH
  try { $env:PATH = "$nodeRoot;$savedPath"; $env:PLAYWRIGHT_BROWSERS_PATH = '0'; Push-Location frontend; & "$nodeRoot\npm.cmd" run test:e2e -- path-picker.spec.ts } finally { Pop-Location; $env:PATH = $savedPath }
  ```

  Expected: the controls or mocked route are absent, so at least one test fails before integration.

- [ ] **Step 3: Replace only the three existing bare inputs.**

  Import `PathPicker` in `SetupStep.tsx` and replace the `setup-source-dataset` input with `purpose="source_dataset"`; replace `setup-output-dataset` with `purpose="output_dataset"`; retain their exact ids, labels, placeholders, `taskLocked` conditions, `FormField` wrappers, and change callbacks. Add a typed `pathPickerCopy` prop to `SetupStepProps` containing the five localized strings.

  Import `PathPicker` in `ReplaceStep.tsx` and replace only the custom-mode `replace-custom-index-path` input with `purpose="replacement_csv"`; use the existing disabled condition `taskLocked || !draft.replace.enabled`. Add the same typed copy prop. Do not change `ResourcePicker.tsx`, the bundled resource selection branch, the Danbooru early return, or the input mode conditions.

  In `App.tsx`, pass the identical localized object to both step components and preserve existing callback bodies exactly:

  ```tsx
  pathPickerCopy={{
    selectLabel: t("selectPath"),
    selectingLabel: t("selectingPath"),
    busyMessage: t("pathPickerBusy"),
    unavailableMessage: t("pathPickerUnavailable"),
    failedMessage: t("pathPickerFailed"),
  }}
  ```

  In `mockApi.ts`, add the selected-path fixture and route before the generic unhandled-API branch. Failure handling already allows test coverage of `409` and `503`; configure those errors with `failRoute()` and assert the field value stays unchanged.

- [ ] **Step 4: Run rendered, static, and build verification.**

  Run:

  ```powershell
  & .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\contract -t . -p 'test_frontend_module_decomposition.py' -v
  $nodeRoot = (Resolve-Path '.\.toolchains\node-v24.18.0-win-x64').Path
  $savedPath = $env:PATH
  try { $env:PATH = "$nodeRoot;$savedPath"; $env:PLAYWRIGHT_BROWSERS_PATH = '0'; Push-Location frontend; & "$nodeRoot\npm.cmd" run typecheck; & "$nodeRoot\npm.cmd" run build; & "$nodeRoot\npm.cmd" run test:e2e -- path-picker.spec.ts } finally { Pop-Location; $env:PATH = $savedPath }
  ```

  Expected: the static contract, TypeScript check, production build, and all new Chromium interaction tests pass. Inspect the 320px test result or screenshot to confirm the picker row has no horizontal overflow.

- [ ] **Step 5: Commit the integration and E2E coverage.**

  ```powershell
  git add frontend/src/components/steps/SetupStep.tsx frontend/src/components/steps/ReplaceStep.tsx frontend/src/App.tsx frontend/tests/e2e/mockApi.ts frontend/tests/e2e/path-picker.spec.ts
  git commit -m "feat: connect native path picker fields"
  ```

### Task 5: End-To-End Regression, Documentation, And Cleanup

**Files:**
- Modify: `ROADMAP.md`
- Modify: `MEMORY.md`
- Modify: `docs/superpowers/plans/2026-08-08-global-native-path-picker.md`

- [ ] **Step 1: Run the final focused Core gate and the full local-mock frontend gate.**

  Run:

  ```powershell
  & .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\unit -t . -p 'test_native_path_picker.py' -v
  & .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\unit -t . -p 'test_api.py' -v
  & .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\contract -t . -p 'test_core_module_decomposition.py' -v
  & .\.runtime-build\runtimes\core\python.exe -B -I -m unittest discover -s tests\contract -t . -p 'test_frontend_module_decomposition.py' -v
  $nodeRoot = (Resolve-Path '.\.toolchains\node-v24.18.0-win-x64').Path
  $savedPath = $env:PATH
  try { $env:PATH = "$nodeRoot;$savedPath"; $env:PLAYWRIGHT_BROWSERS_PATH = '0'; Push-Location frontend; & "$nodeRoot\npm.cmd" run typecheck; & "$nodeRoot\npm.cmd" run build; & "$nodeRoot\npm.cmd" run test:e2e } finally { Pop-Location; $env:PATH = $savedPath }
  ```

  Expected: every targeted Core/contract test, typecheck, build, and full local-mock Playwright suite exits `0`. Do not run stress, worker jobs, external providers, downloads, or mutation of a real dataset.

- [ ] **Step 2: Perform the bounded manual Windows acceptance pass.**

  Start the existing local application once, then in the running UI: select an existing source folder, select an output folder created from the native dialog, select one CSV, and cancel each dialog once. Confirm that every successful choice populates only its own field; cancellation leaves the typed value untouched; preflight still reports any invalid/overlapping/non-CSV path instead of the picker doing that validation. Stop the backend with the existing shutdown mechanism after the check, so the modal UI leaves no persistent backend resource.

- [ ] **Step 3: Update delivery records with actual results only.**

  Change the path-picker item in `ROADMAP.md` from pending to `[x]` only after Step 1 and Step 2 succeed. Add the exact test counts/commands and the native/manual results to `MEMORY.md`; mark every completed checkbox in this plan `[x]`. Do not mark an unavailable desktop-dialog check complete if it could not run.

- [ ] **Step 4: Remove only task-created temporary artifacts.**

  Inspect `frontend/test-results`, `frontend/playwright-report`, and any path-picker-only `.test-tmp` directory before deletion. Remove only files created by this plan after verifying their absolute paths are under those exact generated directories; retain all existing user evidence, screenshots, models, runtimes, and unrelated dirty worktree changes.

- [ ] **Step 5: Commit the verification record.**

  ```powershell
  git add -f docs/superpowers/plans/2026-08-08-global-native-path-picker.md
  git add ROADMAP.md MEMORY.md
  git commit -m "docs: verify native path picker"
  ```

## Plan Self-Review

- Spec coverage: Tasks 1-2 implement the fixed purpose allowlist, native directory/CSV modes, cancellation, busy/unavailable errors, injection, lazy Tk, root cleanup, and no filesystem listing. Tasks 3-4 implement the reusable input/button UI, all three current fields, manual typing, localization, mock behavior, disabled state, cancellation, and responsive layout. Task 5 records the final gates and manual Windows behavior while preserving preflight as the validation authority.
- Completeness scan: no deferred marker or unspecified error-handling step is present. Each state has a named status, response, test, and owner.
- Type consistency: Core uses `PathPickerPurpose`, `NativePathPicker`, `NativePathPickerBusyError`, `NativePathPickerUnavailableError`, `SelectPathRequest`, `current_path`, and `/api/application/select-path` consistently. Frontend uses `PathPickerPurpose`, `SelectLocalPathResponse`, `selectLocalPath`, `PathPicker`, `currentPath`, and the same three string literals consistently.
- Scope check: the plan changes only the local UI control plane and the three established editable path fields. It does not modify JobConfig, preflight behavior, tasks, workers, SQLite, resources, launcher behavior, or the separate NL-v9 specification.
