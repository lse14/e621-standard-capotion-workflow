import { ResourcePicker, type ResourcePickerProps } from "../ResourcePicker";
import { FormField, ToggleField, type FieldGuidanceCopy } from "../FormField";
import type { Draft } from "../../draft";
import type { UiLanguage } from "../../i18n";

type Translate = (key: string, values?: Record<string, string | number>) => string;

type ReplaceIndexSummary = { mode: string; path?: string; sha256?: string; ruleCount?: number } | null;

export type ReplaceStepProps = {
  draft: Pick<Draft, "replace">;
  annotationProfile: string;
  taskLocked: boolean;
  language: UiLanguage;
  resources: ResourcePickerProps["resources"];
  resourcesLoading: boolean;
  resourceError: string | null;
  invalidResourceCount: number;
  resourcePickerCopy: ResourcePickerProps["copy"];
  replaceIndex: ReplaceIndexSummary;
  t: Translate;
  guidanceCopy: FieldGuidanceCopy;
  copy: {
    replaceSkipped: string; replaceMode: string; bundledIndex: string; customIndex: string; customIndexPath: string;
    customIndexHelp: string; replacementIndex: string; replacementIndexHelp: string; indexRules: string;
  };
  onReplaceChange: (patch: Partial<Draft["replace"]>) => void;
  onIndexModeChange: (mode: Draft["replace"]["indexMode"]) => void;
  onRefreshResources: () => void;
};

export function ReplaceStep({
  draft, annotationProfile, taskLocked, language, resources, resourcesLoading, resourceError, invalidResourceCount,
  resourcePickerCopy, replaceIndex, t, guidanceCopy, copy, onReplaceChange, onIndexModeChange, onRefreshResources,
}: ReplaceStepProps) {
  if (annotationProfile === "danbooru") {
    return <div className="option-stack"><p className="hint">{copy.replaceSkipped}</p></div>;
  }

  return <div className="option-stack" data-config-surface="replace">
    <ToggleField id="replace-enabled" label={t("enableReplacement")} checked={draft.replace.enabled} disabled={taskLocked} onChange={(enabled) => onReplaceChange({ enabled })} copy={guidanceCopy} guidance={{ description: t("fieldHelp_enableReplacement"), defaultValue: draft.replace.enabled ? t("fieldEnabled") : t("fieldDisabled") }} />
    <FormField id="replace-mode" label={copy.replaceMode} copy={guidanceCopy} guidance={{ description: t("fieldHelp_replaceMode"), defaultValue: copy.bundledIndex }}>
      <select id="replace-mode" disabled={taskLocked || !draft.replace.enabled} value={draft.replace.indexMode} onChange={(event) => onIndexModeChange(event.target.value as Draft["replace"]["indexMode"])}><option value="bundled">{copy.bundledIndex}</option><option value="custom">{copy.customIndex}</option></select>
    </FormField>
    {draft.replace.indexMode === "bundled" && <ResourcePicker
      id="replace-resource"
      label={copy.replacementIndex} language={language} profile={annotationProfile} selectedId={draft.replace.resourceId ?? ""} resources={resources}
      loading={resourcesLoading} error={resourceError} invalidCount={invalidResourceCount}
      guidance={{ description: t("fieldHelp_replaceMode") }} guidanceCopy={guidanceCopy}
      selectionDisabled={taskLocked || !draft.replace.enabled} refreshDisabled={taskLocked} note={copy.replacementIndexHelp} copy={resourcePickerCopy}
      onChange={(resourceId) => onReplaceChange({ resourceId })} onRefresh={onRefreshResources}
    />}
    {draft.replace.indexMode === "custom" && <FormField id="replace-custom-index-path" label={copy.customIndexPath} copy={guidanceCopy} guidance={{ description: t("fieldHelp_customIndexPath") }}>
      <input id="replace-custom-index-path" disabled={taskLocked || !draft.replace.enabled} value={draft.replace.customIndexPath ?? ""} onChange={(event) => onReplaceChange({ customIndexPath: event.target.value })} placeholder={t("absoluteWindowsPath")} />
    </FormField>}
    {replaceIndex?.mode === "custom" && <section className="resource-summary"><span>{copy.customIndex}</span><code>{String(replaceIndex.path)}</code><small>SHA-256: {String(replaceIndex.sha256)}</small><small>{copy.indexRules}: {String(replaceIndex.ruleCount)}</small></section>}
  </div>;
}
