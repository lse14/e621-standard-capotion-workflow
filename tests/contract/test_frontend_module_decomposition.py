from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FRONTEND_SOURCE = ROOT / "frontend" / "src"


class FrontendModuleDecompositionTests(unittest.TestCase):
    def test_form_field_is_presentational_and_draft_free(self) -> None:
        source = (FRONTEND_SOURCE / "components" / "FormField.tsx").read_text(encoding="utf-8")
        for marker in (
            "export type FieldGuidance",
            "export type FormFieldProps",
            "export function FieldHelp",
            "export function FormField",
            "export function ToggleField",
        ):
            self.assertIn(marker, source)
        for forbidden in ('from "../api"', 'from "../draft"', "localStorage", "fetch("):
            self.assertNotIn(forbidden, source)

    def test_typed_draft_and_page_copy_have_explicit_owners(self) -> None:
        app_source = (FRONTEND_SOURCE / "App.tsx").read_text(encoding="utf-8")
        draft_path = FRONTEND_SOURCE / "draft.ts"
        copy_path = FRONTEND_SOURCE / "appCopy.ts"

        self.assertTrue(draft_path.is_file())
        self.assertTrue(copy_path.is_file())

        draft_source = draft_path.read_text(encoding="utf-8")
        copy_source = copy_path.read_text(encoding="utf-8")

        self.assertNotIn("type Draft = Record<string, any>", app_source)
        self.assertIn('from "./draft"', app_source)
        self.assertIn('from "./appCopy"', app_source)
        self.assertIn("export type Draft", draft_source)
        self.assertIn("export function newDraft", draft_source)
        self.assertIn("export const text", copy_source)

    def test_resource_and_job_monitor_hooks_own_network_and_cursors(self) -> None:
        app_path = FRONTEND_SOURCE / "App.tsx"
        resource_hook_path = FRONTEND_SOURCE / "hooks" / "useResourceCatalog.ts"
        monitor_hook_path = FRONTEND_SOURCE / "hooks" / "useJobMonitor.ts"

        self.assertTrue(resource_hook_path.is_file())
        self.assertTrue(monitor_hook_path.is_file())

        app_source = app_path.read_text(encoding="utf-8")
        resource_hook_source = resource_hook_path.read_text(encoding="utf-8")
        monitor_hook_source = monitor_hook_path.read_text(encoding="utf-8")

        self.assertIn('from "./hooks/useResourceCatalog"', app_source)
        self.assertIn('from "./hooks/useJobMonitor"', app_source)
        self.assertNotIn("listResources", app_source)
        self.assertNotIn("listJobs", app_source)
        self.assertNotIn("pollJob", app_source)
        self.assertNotIn("window.setInterval", app_source)
        self.assertNotIn("const cursor = useRef", app_source)
        self.assertNotIn("const [issueCursor", app_source)
        self.assertNotIn("anima.ui.jobId.v1", app_source)
        self.assertIn("export function useResourceCatalog", resource_hook_source)
        self.assertIn("export function useJobMonitor", monitor_hook_source)

    def test_shared_components_have_explicit_presentational_owners(self) -> None:
        app_source = (FRONTEND_SOURCE / "App.tsx").read_text(encoding="utf-8")
        components = FRONTEND_SOURCE / "components"
        expected = {
            "ResourcePicker.tsx": ("ResourcePickerProps", "ResourcePicker", 'className="resource-picker"'),
            "WorkflowRail.tsx": ("WorkflowRailProps", "WorkflowRail", 'className="workflow-rail"'),
            "TaskMonitor.tsx": ("TaskMonitorProps", "TaskMonitor", 'className="task-monitor"'),
            "IssuePanel.tsx": ("IssuePanelProps", "IssuePanel", 'className="issues-panel"'),
        }

        for filename, (props_name, component_name, owner_marker) in expected.items():
            component_path = components / filename
            self.assertTrue(component_path.is_file(), filename)
            component_source = component_path.read_text(encoding="utf-8")
            self.assertIn(f"export type {props_name}", component_source)
            self.assertIn(f"export function {component_name}", component_source)
            self.assertIn(owner_marker, component_source)
            self.assertNotIn('from "../api"', component_source)
            self.assertNotIn("from '../api'", component_source)

        self.assertNotRegex(app_source, r"(?:type|interface)\s+ResourcePickerProps\b")
        self.assertNotRegex(app_source, r"function\s+ResourcePicker\b")
        self.assertNotIn('className="workflow-rail"', app_source)
        self.assertNotIn('className="task-monitor"', app_source)
        self.assertNotIn('className="issues-panel"', app_source)

    def test_path_picker_owns_the_local_path_request(self) -> None:
        component_path = FRONTEND_SOURCE / "components" / "PathPicker.tsx"
        self.assertTrue(component_path.is_file(), "PathPicker.tsx")
        if not component_path.is_file():
            return

        component = component_path.read_text(encoding="utf-8")
        self.assertIn("export type PathPickerProps", component)
        self.assertIn("export function PathPicker", component)
        self.assertIn('className="path-picker"', component)
        self.assertIn('from "../api"', component)
        self.assertIn("selectLocalPath", component)
        for filename in ("SetupStep.tsx", "ReplaceStep.tsx"):
            source = (FRONTEND_SOURCE / "components" / "steps" / filename).read_text(encoding="utf-8")
            self.assertNotIn('from "../../api"', source)
            self.assertNotIn("from '../../api'", source)

    def test_pipeline_steps_are_presentational_and_not_rendered_in_app(self) -> None:
        app_source = (FRONTEND_SOURCE / "App.tsx").read_text(encoding="utf-8")
        steps = FRONTEND_SOURCE / "components" / "steps"
        expected = {
            "SetupStep.tsx": ("SetupStepProps", "SetupStep"),
            "CaptionStep.tsx": ("CaptionStepProps", "CaptionStep"),
            "ClassifyStep.tsx": ("ClassifyStepProps", "ClassifyStep"),
            "ReplaceStep.tsx": ("ReplaceStepProps", "ReplaceStep"),
            "OcrStep.tsx": ("OcrStepProps", "OcrStep"),
            "NlStep.tsx": ("NlStepProps", "NlStep"),
            "PolicyStep.tsx": ("PolicyStepProps", "PolicyStep"),
            "ExportStep.tsx": ("ExportStepProps", "ExportStep"),
        }

        for filename, (props_name, component_name) in expected.items():
            step_path = steps / filename
            self.assertTrue(step_path.is_file(), filename)
            step_source = step_path.read_text(encoding="utf-8")
            self.assertIn(f"export type {props_name}", step_source)
            self.assertIn(f"export function {component_name}", step_source)
            self.assertNotIn('from "../../api"', step_source)
            self.assertNotIn("from '../../api'", step_source)

        for renderer in (
            "renderSetup", "renderCaption", "renderClassify", "renderReplace", "renderOcr", "renderNl", "renderPolicy", "renderExport",
            "renderStepContent",
        ):
            self.assertNotIn(renderer, app_source)

    def test_nl_api_tools_owns_diagnostics_and_preset_state(self) -> None:
        app_source = (FRONTEND_SOURCE / "App.tsx").read_text(encoding="utf-8")
        tools_path = FRONTEND_SOURCE / "components" / "NlApiTools.tsx"
        self.assertTrue(tools_path.is_file())
        tools_source = tools_path.read_text(encoding="utf-8")
        self.assertIn("export type NlApiToolsProps", tools_source)
        self.assertIn("export function NlApiTools", tools_source)
        self.assertIn('from "../api"', tools_source)
        self.assertIn('from "./FormField"', tools_source)
        for marker in ("presets", "basePrompt", "feedback", "discoverNlModels", "testNlMessage"):
            self.assertIn(marker, tools_source)
        self.assertNotIn("NlPromptPreset", app_source)
        self.assertNotIn("discoverNlModels", app_source)
        self.assertNotIn("testNlMessage", app_source)

    def test_token_budget_step_and_review_panel_have_separate_owners(self) -> None:
        app_source = (FRONTEND_SOURCE / "App.tsx").read_text(encoding="utf-8")
        step_path = FRONTEND_SOURCE / "components" / "steps" / "TokenBudgetStep.tsx"
        review_path = FRONTEND_SOURCE / "TokenBudgetReviewPanel.tsx"

        self.assertTrue(step_path.is_file())
        self.assertTrue(review_path.is_file())

        step_source = step_path.read_text(encoding="utf-8")
        review_source = review_path.read_text(encoding="utf-8")
        self.assertIn("export type TokenBudgetStepProps", step_source)
        self.assertIn("export function TokenBudgetStep", step_source)
        self.assertNotIn('from "../../api"', step_source)
        self.assertNotIn("from '../../api'", step_source)
        self.assertIn("export function TokenBudgetReviewPanel", review_source)
        self.assertIn('from "./api"', review_source)
        self.assertIn('from "./components/steps/TokenBudgetStep"', app_source)
        self.assertIn('from "./TokenBudgetReviewPanel"', app_source)

    def test_nl_step_owns_prompt_routing_and_preset_visibility(self) -> None:
        app_source = (FRONTEND_SOURCE / "App.tsx").read_text(encoding="utf-8")
        nl_source = (FRONTEND_SOURCE / "components" / "steps" / "NlStep.tsx").read_text(encoding="utf-8")
        tools_source = (FRONTEND_SOURCE / "components" / "NlApiTools.tsx").read_text(encoding="utf-8")
        token_budget_source = (FRONTEND_SOURCE / "components" / "steps" / "TokenBudgetStep.tsx").read_text(encoding="utf-8")

        for marker in ("captionPreset", "lengthDistribution", "NlPromptPresetLibrary", "nl-prompt-section"):
            self.assertIn(marker, nl_source)
        for forbidden in ("captionPreset", "lengthDistribution", "onNlChange", 'Pick<Draft, "nl"'):
            self.assertNotIn(forbidden, token_budget_source)
        self.assertNotIn('from "../../api"', nl_source)
        self.assertNotIn("fetchDefaultNlPrompt", app_source)
        self.assertNotIn("NL_PRESET_PROMPT_VERSIONS", app_source)
        for marker in ("NlPromptPresetLibrary", "nl-preset-cards", "listNlPromptPresets", "getNlPromptPreset"):
            self.assertIn(marker, tools_source)

    def test_v9_draft_contract_keeps_token_budget_and_prompt_routing_in_draft(self) -> None:
        draft_source = (FRONTEND_SOURCE / "draft.ts").read_text(encoding="utf-8")
        self.assertIn("schemaVersion: 9", draft_source)
        self.assertIn('inputTxtMode: "tag"', draft_source)
        self.assertIn("taggerFallbackOnMissingTxt: true", draft_source)
        self.assertIn('device: "auto"', draft_source)
        self.assertIn('promptVersion: "nl-default-prompt-v4"', draft_source)
        self.assertIn("captionPreset: \"general\"", draft_source)
        self.assertIn("lengthDistribution: { short: 33, medium: 34, long: 33 }", draft_source)
        self.assertIn('lengthSeed: "anima-nl-length-v1"', draft_source)
        self.assertIn('resourceId: "tokenizer-qwen3-0.6b-anima-v1"', draft_source)

    def test_v9_ocr_execution_is_task_only_and_runtime_status_stays_compact(self) -> None:
        draft_source = (FRONTEND_SOURCE / "draft.ts").read_text(encoding="utf-8")
        api_source = (FRONTEND_SOURCE / "api.ts").read_text(encoding="utf-8")
        ocr_source = (FRONTEND_SOURCE / "components" / "steps" / "OcrStep.tsx").read_text(encoding="utf-8")
        monitor_source = (FRONTEND_SOURCE / "components" / "TaskMonitor.tsx").read_text(encoding="utf-8")

        self.assertIn("schemaVersion: 9", draft_source)
        self.assertIn('device: "auto"', draft_source)
        self.assertIn("export type OcrExecutionRequest", api_source)
        self.assertIn("ocrExecution", api_source)
        self.assertIn("ocrExecution", ocr_source)
        self.assertIn("ocrRuntime", monitor_source)
        self.assertNotIn("runtimeFingerprint", monitor_source)
        self.assertNotIn("localStorage", ocr_source)


if __name__ == "__main__":
    unittest.main()
