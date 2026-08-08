import type { Draft } from "../../draft";
import { FormField, type FieldGuidanceCopy } from "../FormField";

type Translate = (key: string, values?: Record<string, string | number>) => string;

type ExportSummary = {
  valid: number;
  invalid: number;
  exported: number;
  issueCount: number;
  convertedSamples: number;
} | null;

export type ExportStepProps = {
  draft: Pick<Draft, "export">;
  defaults: Pick<Draft, "export">;
  taskLocked: boolean;
  workspaceReady: boolean;
  startPending: boolean;
  exportSummary: ExportSummary;
  t: Translate;
  automatic: string;
  guidanceCopy: FieldGuidanceCopy;
  onExportChange: (patch: Partial<Draft["export"]>) => void;
  onStartPipeline: () => void;
};

export function ExportStep({
  draft, defaults, taskLocked, workspaceReady, startPending, exportSummary, t, automatic, guidanceCopy, onExportChange, onStartPipeline,
}: ExportStepProps) {
  return <>
    <div className="form-grid" data-config-surface="export"><FormField id="export-format" label={t("format")} copy={guidanceCopy} guidance={{ description: t("fieldHelp_exportFormat"), defaultValue: t("both") }}><select id="export-format" disabled={taskLocked} value={draft.export.format} onChange={(event) => onExportChange({ format: event.target.value as Draft["export"]["format"] })}><option value="both">{t("both")}</option><option value="json">{t("jsonOnly")}</option><option value="flat_txt">{t("flatTxtOnly")}</option></select></FormField></div>
    <div className="launch-area" aria-busy={startPending}><p>{automatic}</p><button type="button" disabled={!workspaceReady || startPending} aria-busy={startPending} onClick={onStartPipeline}>{t("startPipeline")}</button></div>
    {exportSummary && <dl className="facts"><div><dt>{t("validInvalid")}</dt><dd>{exportSummary.valid} / {exportSummary.invalid}</dd></div><div><dt>{t("committed")}</dt><dd>{exportSummary.exported}</dd></div><div><dt>{t("issues")}</dt><dd>{exportSummary.issueCount}</dd></div><div><dt>{t("converted")}</dt><dd>{exportSummary.convertedSamples}</dd></div></dl>}
  </>;
}
