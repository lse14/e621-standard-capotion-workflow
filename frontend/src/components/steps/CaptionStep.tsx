import { ResourcePicker, resourceSelectable, type ResourcePickerProps } from "../ResourcePicker";
import { FormField, ToggleField, type FieldGuidanceCopy } from "../FormField";
import { ModuleBatchField } from "../ModuleBatchField";
import type { Draft } from "../../draft";
import type { UiLanguage } from "../../i18n";

type Translate = (key: string, values?: Record<string, string | number>) => string;

type TaggerResource = {
  available: boolean;
  compatibility: { status: string };
  adjustableCategories: string[];
  defaultThresholds: Record<string, number>;
};

export type CaptionStepProps = {
  draft: Pick<Draft, "caption" | "captionFormat">;
  defaults: Pick<Draft, "caption" | "captionFormat">;
  taskLocked: boolean;
  rebuild: boolean;
  language: UiLanguage;
  resources: ResourcePickerProps["resources"];
  resourcesLoading: boolean;
  resourceError: string | null;
  invalidResourceCount: number;
  resourcePickerCopy: ResourcePickerProps["copy"];
  selectedTagger: TaggerResource | undefined;
  triggerInput: string;
  t: Translate;
  guidanceCopy: FieldGuidanceCopy;
  copy: { captionModel: string; captionModelHelp: string };
  onCaptionChange: (patch: Partial<Draft["caption"]>) => void;
  onInputTxtModeChange: (value: Draft["caption"]["inputTxtMode"]) => void;
  onCaptionFormatChange: (patch: Partial<Draft["captionFormat"]>) => void;
  onSelectTagger: (resourceId: string) => void;
  onRefreshResources: () => void;
  onThresholdModeChange: (value: Draft["caption"]["thresholdMode"]) => void;
  onCategoryThresholdChange: (category: string, value: string) => void;
  onTriggerInputChange: (value: string) => void;
  batchSize: number;
  batchRecommended?: number;
  batchReason?: string;
  onBatchSizeChange: (value: number) => void;
};

export function CaptionStep({
  draft, defaults, taskLocked, rebuild, language, resources, resourcesLoading, resourceError, invalidResourceCount,
  resourcePickerCopy, selectedTagger, triggerInput, t, guidanceCopy, copy, onCaptionChange, onInputTxtModeChange, onCaptionFormatChange, onSelectTagger,
  onRefreshResources, onThresholdModeChange, onCategoryThresholdChange, onTriggerInputChange,
  batchSize, batchRecommended, batchReason, onBatchSizeChange,
}: CaptionStepProps) {
  const inputTxtNl = draft.caption.inputTxtMode === "nl";
  const captionOff = taskLocked || !draft.caption.enabled;
  const thresholdControlsDisabled = captionOff || !resourceSelectable(selectedTagger);

  return <div className="option-stack" data-config-surface="caption">
    <FormField id="caption-input-txt-mode" label={t("inputTxtMode")} copy={guidanceCopy} guidance={{ description: t("fieldHelp_inputTxtMode"), defaultValue: t("inputTxtModeTag") }}>
      <select id="caption-input-txt-mode" disabled={taskLocked} value={draft.caption.inputTxtMode} onChange={(event) => onInputTxtModeChange(event.target.value as Draft["caption"]["inputTxtMode"])}>
        <option value="tag">{t("inputTxtModeTag")}</option>
        <option value="nl">{t("inputTxtModeNl")}</option>
      </select>
    </FormField>
    <ToggleField id="caption-enabled" label={t("enableCaption")} checked={draft.caption.enabled} disabled={taskLocked || inputTxtNl} onChange={(enabled) => onCaptionChange({ enabled })} copy={guidanceCopy} guidance={{ description: t("fieldHelp_enableCaption"), defaultValue: defaults.caption.enabled ? t("fieldEnabled") : t("fieldDisabled") }} />
    <ModuleBatchField id="caption-batch-size" label={t("batchSize")} value={batchSize} defaultValue={4} recommended={batchRecommended} recommendationReason={batchReason} minimum={1} maximum={64} disabled={taskLocked || !draft.caption.enabled} t={t} guidanceCopy={guidanceCopy} onChange={onBatchSizeChange} />
    {!inputTxtNl && <ToggleField id="caption-tagger-fallback" label={t("taggerFallbackOnMissingTxt")} checked={draft.caption.taggerFallbackOnMissingTxt} disabled={captionOff} onChange={(taggerFallbackOnMissingTxt) => onCaptionChange({ taggerFallbackOnMissingTxt })} copy={guidanceCopy} guidance={{ description: t("fieldHelp_taggerFallbackOnMissingTxt"), defaultValue: defaults.caption.taggerFallbackOnMissingTxt ? t("fieldEnabled") : t("fieldDisabled") }} />}
    <ResourcePicker
      id="caption-tagging-model"
      label={copy.captionModel} language={language} selectedId={draft.caption.resourceId ?? ""} resources={resources}
      loading={resourcesLoading} error={resourceError} invalidCount={invalidResourceCount}
      guidance={{ description: t("fieldHelp_captionModel") }} guidanceCopy={guidanceCopy}
      selectionDisabled={captionOff} refreshDisabled={taskLocked} note={copy.captionModelHelp} copy={resourcePickerCopy}
      onChange={onSelectTagger} onRefresh={onRefreshResources}
    />
    <ToggleField id="caption-overwrite-txt" label={t("overwriteTxt")} checked={draft.caption.overwriteTxt} disabled={taskLocked || rebuild || inputTxtNl || draft.caption.inputTxtMode === "tag"} onChange={(overwriteTxt) => onCaptionChange({ overwriteTxt })} copy={guidanceCopy} guidance={{ description: t("fieldHelp_overwriteTxtInputMode"), defaultValue: defaults.caption.overwriteTxt ? t("fieldEnabled") : t("fieldDisabled") }} />
    <div className="form-grid">
      <FormField id="caption-threshold-mode" label={t("thresholdMode")} copy={guidanceCopy} guidance={{ description: t("fieldHelp_thresholdMode"), defaultValue: t("thresholdModelDefault") }}>
        <select id="caption-threshold-mode" disabled={thresholdControlsDisabled} value={draft.caption.thresholdMode} onChange={(event) => onThresholdModeChange(event.target.value as Draft["caption"]["thresholdMode"])}><option value="model_default">{t("thresholdModelDefault")}</option><option value="uniform">{t("thresholdUniform")}</option><option value="per_category">{t("thresholdPerCategory")}</option></select>
      </FormField>
      {draft.caption.thresholdMode === "uniform" && <FormField id="caption-uniform-threshold" label={t("uniformThreshold")} copy={guidanceCopy} guidance={{ description: t("fieldHelp_thresholdValue"), defaultValue: String(defaults.caption.uniformThreshold ?? ""), range: "0-1, step 0.01" }}>
        <input id="caption-uniform-threshold" disabled={thresholdControlsDisabled} type="number" min="0" max="1" step="0.01" value={draft.caption.uniformThreshold} onChange={(event) => onCaptionChange({ uniformThreshold: Number(event.target.value) })} />
      </FormField>}
      {draft.caption.thresholdMode === "per_category" && (selectedTagger?.adjustableCategories ?? []).map((category) => <FormField key={category} id={`caption-threshold-${category}`} label={category} copy={guidanceCopy} guidance={{ description: t("fieldHelp_thresholdValue"), defaultValue: String(defaults.caption.categoryThresholds?.[category] ?? selectedTagger?.defaultThresholds[category] ?? ""), range: "0-1, step 0.01" }}>
        <input id={`caption-threshold-${category}`} disabled={thresholdControlsDisabled} type="number" min="0" max="1" step="0.01" value={draft.caption.categoryThresholds?.[category] ?? selectedTagger?.defaultThresholds[category] ?? ""} onChange={(event) => onCategoryThresholdChange(category, event.target.value)} />
      </FormField>)}
    </div>
    <ToggleField id="caption-replace-underscores" label={t("replaceUnderscores")} checked={draft.captionFormat.replaceUnderscoresWithSpaces} disabled={captionOff} onChange={(replaceUnderscoresWithSpaces) => onCaptionFormatChange({ replaceUnderscoresWithSpaces })} copy={guidanceCopy} guidance={{ description: t("fieldHelp_replaceUnderscores"), defaultValue: defaults.captionFormat.replaceUnderscoresWithSpaces ? t("fieldEnabled") : t("fieldDisabled") }} />
    <ToggleField id="caption-preserve-escapes" label={t("preserveEscapes")} checked={draft.captionFormat.preserveEscapes} disabled={captionOff} onChange={(preserveEscapes) => onCaptionFormatChange({ preserveEscapes })} copy={guidanceCopy} guidance={{ description: t("fieldHelp_preserveEscapes"), defaultValue: defaults.captionFormat.preserveEscapes ? t("fieldEnabled") : t("fieldDisabled") }} />
    <ToggleField id="caption-enable-triggers" label={t("enableTriggers")} checked={draft.captionFormat.triggersEnabled} disabled={captionOff} onChange={(triggersEnabled) => onCaptionFormatChange({ triggersEnabled })} copy={guidanceCopy} guidance={{ description: t("fieldHelp_enableTriggers"), defaultValue: defaults.captionFormat.triggersEnabled ? t("fieldEnabled") : t("fieldDisabled") }} />
    {draft.captionFormat.triggersEnabled && <FormField id="caption-trigger-terms" label={t("triggerTerms")} copy={guidanceCopy} guidance={{ description: t("fieldHelp_triggerTerms") }}>
      <input id="caption-trigger-terms" disabled={captionOff} value={triggerInput} onChange={(event) => onTriggerInputChange(event.target.value)} />
    </FormField>}
  </div>;
}
