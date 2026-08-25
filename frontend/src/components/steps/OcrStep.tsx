import type { Draft } from "../../draft";
import { FieldHelp, FormField, ToggleField, type FieldGuidanceCopy } from "../FormField";

type OcrExecutionTuning = { mode: "auto"; value: null } | { mode: "manual"; value: number };
type OcrExecutionRequest = {
  textDetLimitSideLen: OcrExecutionTuning;
  textBatchSize: OcrExecutionTuning;
};
type OcrRuntimeStatus = {
  availability: "pending" | "available" | "unavailable";
  runtimeId: "ocr-paddle" | "ocr-paddle-gpu" | null;
  gpuName: string | null;
  totalVramBytes: number | null;
  requestedDevice: "auto" | "cuda" | "cpu" | null;
  observedDevice: "cpu" | "cuda" | null;
  recommended: { textDetLimitSideLen: number; textBatchSize: number } | null;
  effective: { textDetLimitSideLen: number; textBatchSize: number } | null;
  startupReason: "gpu_runtime_unavailable" | "binding_invalid" | null;
};

type OcrResource = {
  resourceId: string;
  available: boolean;
  compatibility: { status: string };
  distribution: { mode: "bundled" | "local-only"; sourceUrl?: string; licenseStatus?: "unverified" };
};

type OcrDiagnostic = { code: string; count: number };
type Translate = (key: string, values?: Record<string, string | number>) => string;

type Copy = {
  enableOcr: string; llmMinConfidence: string; forceReprocess: string; ocrFixedResource: string;
  ocrInstalled: string; ocrMissing: string; ocrRefresh: string; ocrSource: string; ocrDistribution: string;
  ocrLicense: string; ocrLocalOnly: string; ocrUnverified: string; ocrInstallCommand: string; ocrPerformance: string;
  ocrDevice: string; ocrDeviceAuto: string; ocrDeviceCuda: string; ocrDeviceCpu: string;
  ocrDetectionLimit: string; ocrAutomaticDetectionLimit: string; ocrManualDetectionLimit: string;
  ocrTextBatch: string; ocrAutomaticTextBatch: string; ocrManualTextBatch: string;
  ocrSummary: string; ocrTotal: string; ocrNew: string; ocrReused: string; ocrSuccess: string;
  ocrNoText: string; ocrFailed: string; ocrTextItems: string; ocrIncludedForLlm: string; ocrContextOmitted: string;
  ocrFailureReason: string;
};

export type OcrStepProps = {
  draft: Pick<Draft, "ocr">;
  defaults: Pick<Draft, "ocr">;
  taskLocked: boolean;
  ocrExecution: OcrExecutionRequest;
  runtime: OcrRuntimeStatus | null;
  resource: OcrResource | undefined;
  resourcesLoading: boolean;
  resourceError: string | null;
  diagnostics: ReadonlyArray<OcrDiagnostic>;
  failureMessage: string | null;
  guidanceCopy: FieldGuidanceCopy;
  t: Translate;
  copy: Copy;
  onOcrChange: (patch: Partial<Draft["ocr"]>) => void;
  onOcrExecutionChange: (value: OcrExecutionRequest) => void;
  onRefreshResources: () => void;
};

const FIXED_RESOURCE_ID = "ocr-ppocrv5-server-paddle-v1";

type TuningControlProps = {
  field: keyof OcrExecutionRequest;
  label: string;
  automaticLabel: string;
  manualLabel: string;
  minimum: number;
  maximum: number;
  step: number;
  disabled: boolean;
  request: OcrExecutionRequest;
  guidanceCopy: FieldGuidanceCopy;
  guidance: string;
  recommendation?: string | null;
  onChange: (field: keyof OcrExecutionRequest, tuning: OcrExecutionTuning) => void;
};

function TuningControl({
  field, label, automaticLabel, manualLabel, minimum, maximum, step, disabled, request, guidanceCopy, guidance, recommendation, onChange,
}: TuningControlProps) {
  const tuning = request[field];
  const manual = tuning.mode === "manual";
  const modeName = `ocr-${field}-mode`;
  const updateManualValue = (raw: string) => {
    const value = Number(raw);
    if (Number.isInteger(value) && value >= minimum && value <= maximum && (value - minimum) % step === 0) {
      onChange(field, { mode: "manual", value });
    }
  };
  return <fieldset className="ocr-tuning" disabled={disabled} data-setting-field>
    <legend><span>{label}</span><FieldHelp label={label} guidance={{ description: guidance, defaultValue: automaticLabel, recommendation: recommendation ?? null, range: `${minimum}-${maximum} step ${step}` }} copy={guidanceCopy} /></legend>
    <div className="ocr-tuning-choice">
      <label><input type="radio" name={modeName} checked={!manual} onChange={() => onChange(field, { mode: "auto", value: null })} />{automaticLabel}</label>
      <label><input type="radio" name={modeName} checked={manual} onChange={() => onChange(field, { mode: "manual", value: minimum })} />{manualLabel}</label>
    </div>
    <label>{label}<input aria-label={label} type="number" min={minimum} max={maximum} step={step} disabled={disabled || !manual} value={manual ? tuning.value : ""} onChange={(event) => updateManualValue(event.target.value)} /></label>
  </fieldset>;
}

export function OcrStep({
  draft, defaults, taskLocked, ocrExecution, runtime, resource, resourcesLoading, resourceError, diagnostics, failureMessage, guidanceCopy, t, copy, onOcrChange, onOcrExecutionChange, onRefreshResources,
}: OcrStepProps) {
  const controlsDisabled = taskLocked || !draft.ocr.enabled;
  const resourceReady = Boolean(resource?.available && !["incompatible", "unavailable"].includes(resource.compatibility.status));
  const counts = new Map(diagnostics.map((item) => [item.code, item.count]));
  const summary = [
    [copy.ocrTotal, "ocr_total"], [copy.ocrNew, "ocr_new"], [copy.ocrReused, "ocr_reused"],
    [copy.ocrSuccess, "ocr_success"], [copy.ocrNoText, "ocr_no_text"], [copy.ocrFailed, "ocr_failed"],
    [copy.ocrTextItems, "ocr_text_items"], [copy.ocrIncludedForLlm, "ocr_included_for_llm"],
    [copy.ocrContextOmitted, "nl_ocr_context_omitted_too_large"],
  ] as const;
  const updateConfidence = (value: string) => {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) onOcrChange({ llmMinConfidence: Math.min(1, Math.max(0, parsed)) });
  };
  const updateTuning = (field: keyof OcrExecutionRequest, tuning: OcrExecutionTuning) => {
    onOcrExecutionChange({ ...ocrExecution, [field]: tuning });
  };

  return <div className="option-stack ocr-step" data-config-surface="ocr">
    <ToggleField id="ocr-enabled" label={copy.enableOcr} checked={draft.ocr.enabled} disabled={taskLocked} onChange={(enabled) => onOcrChange({ enabled })} copy={guidanceCopy} guidance={{ description: t("fieldHelp_enableOcr"), defaultValue: defaults.ocr.enabled ? t("fieldEnabled") : t("fieldDisabled") }} />
    <div className="form-grid">
      <FormField id="ocr-llm-confidence" label={copy.llmMinConfidence} copy={guidanceCopy} guidance={{ description: t("fieldHelp_ocrConfidence"), defaultValue: String(defaults.ocr.llmMinConfidence), range: "0-1, step 0.01" }}>
        <input id="ocr-llm-confidence" disabled={controlsDisabled} type="number" min="0" max="1" step="0.01" value={draft.ocr.llmMinConfidence} onChange={(event) => updateConfidence(event.target.value)} />
      </FormField>
      <ToggleField id="ocr-force-reprocess" label={copy.forceReprocess} checked={draft.ocr.forceReprocess} disabled={controlsDisabled} onChange={(forceReprocess) => onOcrChange({ forceReprocess })} copy={guidanceCopy} guidance={{ description: t("fieldHelp_ocrForceReprocess"), defaultValue: defaults.ocr.forceReprocess ? t("fieldEnabled") : t("fieldDisabled") }} />
    </div>
    <FormField id="ocr-device" label={copy.ocrDevice} copy={guidanceCopy} guidance={{ description: t("fieldHelp_ocrDevice"), defaultValue: copy.ocrDeviceAuto, recommendation: copy.ocrDeviceAuto }}>
      <select id="ocr-device" disabled={controlsDisabled} value={draft.ocr.device} onChange={(event) => onOcrChange({ device: event.target.value as Draft["ocr"]["device"] })}>
        <option value="auto">{copy.ocrDeviceAuto}</option><option value="cuda">{copy.ocrDeviceCuda}</option><option value="cpu">{copy.ocrDeviceCpu}</option>
      </select>
    </FormField>
    <div className="ocr-tuning-grid">
      <TuningControl field="textDetLimitSideLen" label={copy.ocrDetectionLimit} automaticLabel={copy.ocrAutomaticDetectionLimit} manualLabel={copy.ocrManualDetectionLimit} minimum={1920} maximum={3840} step={32} disabled={controlsDisabled} request={ocrExecution} guidance={t("fieldHelp_ocrDetection")} recommendation={runtime?.recommended ? String(runtime.recommended.textDetLimitSideLen) : null} guidanceCopy={guidanceCopy} onChange={updateTuning} />
      <TuningControl field="textBatchSize" label={copy.ocrTextBatch} automaticLabel={copy.ocrAutomaticTextBatch} manualLabel={copy.ocrManualTextBatch} minimum={1} maximum={8} step={1} disabled={controlsDisabled} request={ocrExecution} guidance={t("fieldHelp_ocrBatch")} recommendation={runtime?.recommended ? String(runtime.recommended.textBatchSize) : null} guidanceCopy={guidanceCopy} onChange={updateTuning} />
    </div>
    {runtime?.recommended ? <p className="hint ocr-recommendation">{copy.ocrPerformance}: {runtime.recommended.textDetLimitSideLen} / {runtime.recommended.textBatchSize}</p> : null}
    <section className="resource-summary" aria-label={copy.ocrFixedResource}>
      <span>{copy.ocrFixedResource}</span><code>{FIXED_RESOURCE_ID}</code>
      <small>{resourceReady ? copy.ocrInstalled : copy.ocrMissing}</small>
      <div className="step-actions"><button className="icon-button secondary" type="button" disabled={taskLocked || resourcesLoading} onClick={onRefreshResources} aria-label={copy.ocrRefresh} title={copy.ocrRefresh}><span aria-hidden="true">&#8635;</span></button></div>
      {resourceError && <small className="resource-error" role="alert">{resourceError}</small>}
      {resource?.distribution.sourceUrl && <small>{copy.ocrSource}: <a href={resource.distribution.sourceUrl} target="_blank" rel="noreferrer">{resource.distribution.sourceUrl}</a></small>}
      <small>{copy.ocrDistribution}: {resource?.distribution.mode === "local-only" ? copy.ocrLocalOnly : "-"}</small>
      <small>{copy.ocrLicense}: {resource?.distribution.licenseStatus === "unverified" ? copy.ocrUnverified : "-"}</small>
      {!resourceReady && <><strong>{copy.ocrInstallCommand}</strong><code>Import-OcrResource.bat -Apply</code></>}
    </section>
    {failureMessage && <section className="action-feedback ocr-failure" aria-live="assertive"><strong>{copy.ocrFailureReason}</strong><p role="alert">{failureMessage}</p></section>}
    {diagnostics.length > 0 && <section className="ocr-summary" aria-label={copy.ocrSummary}><h3>{copy.ocrSummary}</h3><dl>{summary.map(([label, code]) => <div key={code}><dt>{label}</dt><dd>{counts.get(code) ?? 0}</dd></div>)}</dl></section>}
  </div>;
}
