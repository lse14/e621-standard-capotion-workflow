import type { Draft } from "../../draft";
import { formatLabel, type UiLanguage } from "../../i18n";
import { FormField, ToggleField, type FieldGuidanceCopy } from "../FormField";

type Translate = (key: string, values?: Record<string, string | number>) => string;

type SetupPreflight = {
  sampleCount: number;
  inScopeCount: number;
  nonblankTxtCount: number;
  nonblankJsonCount: number;
  blankTxtCount: number;
  blankJsonCount: number;
  annotationKeyCollisionCount: number;
  imageIssueCount: number;
  projection: {
    format?: string;
    retainedSamples?: number;
    jsonCreate?: number;
    jsonOverwrite?: number;
    jsonDelete?: number;
    txtCreate?: number;
    txtOverwrite?: number;
    txtDelete?: number;
  };
  estimate: { incrementalWriteBytes?: number; backupUpperBoundBytes?: number };
  api: { minRequests?: number; maxWithBackupRequests?: number; httpAttemptBudget?: number; estimatedUploadBytes?: number };
};

function formatBytes(value: number): string {
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let size = Math.max(0, value), unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size < 10 && unit ? size.toFixed(1) : Math.round(size)} ${units[unit]}`;
}

export type SetupStepProps = {
  draft: Pick<Draft, "sourceRoot" | "workMode" | "outputRoot" | "overwriteMode" | "imageDecode" | "recursive" | "export">;
  defaults: Pick<Draft, "workMode" | "overwriteMode" | "imageDecode" | "recursive">;
  taskLocked: boolean;
  resourcesLoading: boolean;
  profileResourcesReady: boolean;
  workspaceReady: boolean;
  preflightPending: boolean;
  workspacePending: boolean;
  preflight: SetupPreflight | null;
  enabledModules: string;
  language: UiLanguage;
  t: Translate;
  guidanceCopy: FieldGuidanceCopy;
  copy: { preflightHint: string; profileReady: string; profileUnavailable: string; workspaceHint: string };
  onSourceRootChange: (value: string) => void;
  onWorkModeChange: (value: Draft["workMode"]) => void;
  onOutputRootChange: (value: string) => void;
  onOverwriteModeChange: (value: Draft["overwriteMode"]) => void;
  onInvalidImageActionChange: (value: Draft["imageDecode"]["invalidImageAction"]) => void;
  onRecursiveChange: (value: boolean) => void;
  onRunPreflight: () => void;
  onPrepareWorkspace: () => void;
};

export function SetupStep({
  draft, defaults, taskLocked, resourcesLoading, profileResourcesReady, workspaceReady, preflightPending, workspacePending, preflight, enabledModules, language, t, guidanceCopy, copy,
  onSourceRootChange, onWorkModeChange, onOutputRootChange, onOverwriteModeChange, onInvalidImageActionChange, onRecursiveChange,
  onRunPreflight, onPrepareWorkspace,
}: SetupStepProps) {
  return <>
    <div className="step-intro"><p>{copy.preflightHint}</p><span className={`status ${profileResourcesReady ? "available" : "failed"}`}>{profileResourcesReady ? copy.profileReady : copy.profileUnavailable}</span></div>
    <div data-config-surface="setup" className="form-grid">
      <FormField id="setup-source-dataset" label={t("sourceDataset")} copy={guidanceCopy} guidance={{ description: t("fieldHelp_sourceDataset") }}>
        <input id="setup-source-dataset" disabled={taskLocked} value={draft.sourceRoot} onChange={(event) => onSourceRootChange(event.target.value)} placeholder={t("absoluteWindowsPath")} />
      </FormField>
      <FormField id="setup-work-mode" label={t("workMode")} copy={guidanceCopy} guidance={{ description: t("fieldHelp_workMode"), defaultValue: defaults.workMode === "in_place" ? t("inPlace") : t("fullCopy") }}>
        <select id="setup-work-mode" disabled={taskLocked} value={draft.workMode} onChange={(event) => onWorkModeChange(event.target.value as Draft["workMode"])}><option value="in_place">{t("inPlace")}</option><option value="full_copy">{t("fullCopy")}</option></select>
      </FormField>
      {draft.workMode === "full_copy" && <FormField id="setup-output-dataset" label={t("outputDataset")} copy={guidanceCopy} guidance={{ description: t("fieldHelp_outputDataset") }} wide>
        <input id="setup-output-dataset" disabled={taskLocked} value={draft.outputRoot ?? ""} onChange={(event) => onOutputRootChange(event.target.value)} placeholder={t("emptyOrAbsentDirectory")} />
      </FormField>}
      <FormField id="setup-overwrite-mode" label={t("overwriteMode")} copy={guidanceCopy} guidance={{ description: t("fieldHelp_overwriteMode"), defaultValue: defaults.overwriteMode === "incremental" ? t("incremental") : t("rebuild") }}>
        <select id="setup-overwrite-mode" disabled={taskLocked} value={draft.overwriteMode} onChange={(event) => onOverwriteModeChange(event.target.value as Draft["overwriteMode"])}><option value="incremental">{t("incremental")}</option><option value="rebuild">{t("rebuild")}</option></select>
      </FormField>
      <FormField id="setup-invalid-image-action" label={t("invalidImageAction")} copy={guidanceCopy} guidance={{ description: t("fieldHelp_invalidImageAction"), defaultValue: defaults.imageDecode.invalidImageAction === "block" ? t("blockInvalidImages") : t("skipInvalidImages") }}>
        <select id="setup-invalid-image-action" disabled={taskLocked} value={draft.imageDecode.invalidImageAction} onChange={(event) => onInvalidImageActionChange(event.target.value as Draft["imageDecode"]["invalidImageAction"])}><option value="block">{t("blockInvalidImages")}</option><option value="skip">{t("skipInvalidImages")}</option></select>
      </FormField>
      <ToggleField id="setup-recursive" label={t("searchSubfolders")} checked={draft.recursive} disabled={taskLocked} onChange={onRecursiveChange} copy={guidanceCopy} guidance={{ description: t("fieldHelp_recursive"), defaultValue: defaults.recursive ? t("fieldEnabled") : t("fieldDisabled") }} />
    </div>
    <div className="step-actions" aria-busy={preflightPending || workspacePending}><button type="button" disabled={taskLocked || resourcesLoading || preflightPending || !profileResourcesReady || !draft.sourceRoot.trim()} aria-busy={preflightPending} onClick={onRunPreflight}>{t("preflight")}</button><button className="secondary" type="button" disabled={!preflight || taskLocked || workspacePending || (draft.imageDecode.invalidImageAction === "block" && preflight.imageIssueCount > 0)} aria-busy={workspacePending} onClick={onPrepareWorkspace}>{t("confirmWorkspace")}</button></div>
    {preflight && <><dl className="facts preflight">
      <div><dt>{t("samples")}</dt><dd>{preflight.sampleCount}</dd></div>
      <div><dt>{t("inScope")}</dt><dd>{preflight.inScopeCount}</dd></div>
      <div><dt>{t("existingTxtJson")}</dt><dd>{preflight.nonblankTxtCount} / {preflight.nonblankJsonCount}</dd></div>
      <div><dt>{t("blankTxtJson")}</dt><dd>{preflight.blankTxtCount} / {preflight.blankJsonCount}</dd></div>
      <div><dt>{t("plannedJson")}</dt><dd>{preflight.projection.jsonCreate ?? 0} / {preflight.projection.jsonOverwrite ?? 0} / {preflight.projection.jsonDelete ?? 0}</dd></div>
      <div><dt>{t("plannedTxt")}</dt><dd>{preflight.projection.txtCreate ?? 0} / {preflight.projection.txtOverwrite ?? 0} / {preflight.projection.txtDelete ?? 0}</dd></div>
      <div><dt>{t("retainedSamples")}</dt><dd>{preflight.projection.retainedSamples ?? 0}</dd></div>
      <div><dt>{t("keyCollisions")}</dt><dd>{preflight.annotationKeyCollisionCount}</dd></div>
      <div><dt>{t("imageIssues")}</dt><dd>{preflight.imageIssueCount}</dd></div>
      <div><dt>{t("spaceEstimate")}</dt><dd>{formatBytes(preflight.estimate.incrementalWriteBytes ?? 0)} / {formatBytes(preflight.estimate.backupUpperBoundBytes ?? 0)}</dd></div>
      <div><dt>{t("apiRequests")}</dt><dd>{preflight.api.minRequests ?? 0} / {preflight.api.maxWithBackupRequests ?? 0}</dd></div>
      <div><dt>{t("httpBudget")}</dt><dd>{preflight.api.httpAttemptBudget ?? 0}</dd></div>
      <div><dt>{t("uploadEstimate")}</dt><dd>{formatBytes(preflight.api.estimatedUploadBytes ?? 0)}</dd></div>
      <div><dt>{t("format")}</dt><dd>{formatLabel(language, String(preflight.projection.format ?? draft.export.format))}</dd></div>
      <div><dt>{t("enabledModules")}</dt><dd>{enabledModules}</dd></div>
    </dl><p className="hint">{workspaceReady ? copy.workspaceHint : copy.preflightHint}</p></>}
  </>;
}
