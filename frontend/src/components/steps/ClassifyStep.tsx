import { ResourcePicker, type ResourcePickerProps } from "../ResourcePicker";
import { ToggleField, type FieldGuidanceCopy } from "../FormField";
import type { Draft } from "../../draft";
import type { UiLanguage } from "../../i18n";

type Translate = (key: string, values?: Record<string, string | number>) => string;

export type ClassifyStepProps = {
  draft: Pick<Draft, "classify">;
  taskLocked: boolean;
  rebuild: boolean;
  classifyEnabled: boolean;
  classificationResourceActive: boolean;
  language: UiLanguage;
  annotationProfile: string;
  resources: ResourcePickerProps["resources"];
  resourcesLoading: boolean;
  resourceError: string | null;
  invalidResourceCount: number;
  resourcePickerCopy: ResourcePickerProps["copy"];
  t: Translate;
  guidanceCopy: FieldGuidanceCopy;
  copy: { classificationIndex: string; classificationIndexHelp: string; anthroReplacementNote: string };
  onClassifyChange: (patch: Partial<Draft["classify"]>) => void;
  onRefreshResources: () => void;
};

export function ClassifyStep({
  draft, taskLocked, rebuild, classifyEnabled, classificationResourceActive, language, annotationProfile, resources,
  resourcesLoading, resourceError, invalidResourceCount, resourcePickerCopy, t, guidanceCopy, copy, onClassifyChange, onRefreshResources,
}: ClassifyStepProps) {
  return <div className="option-stack" data-config-surface="classify">
    <ToggleField id="classify-enabled" label={t("enableClassify")} checked={classifyEnabled} disabled={taskLocked} onChange={(enabled) => onClassifyChange({ enabled })} copy={guidanceCopy} guidance={{ description: t("fieldHelp_enableClassify"), defaultValue: classifyEnabled ? t("fieldEnabled") : t("fieldDisabled") }} />
    <ResourcePicker
      id="classify-resource"
      label={copy.classificationIndex} language={language} profile={annotationProfile} selectedId={draft.classify.resourceId ?? ""} resources={resources}
      loading={resourcesLoading} error={resourceError} invalidCount={invalidResourceCount}
      guidance={{ description: t("fieldHelp_classificationIndex") }} guidanceCopy={guidanceCopy}
      selectionDisabled={taskLocked || !classificationResourceActive} refreshDisabled={taskLocked} note={copy.classificationIndexHelp} copy={resourcePickerCopy}
      onChange={(resourceId) => onClassifyChange({ resourceId })} onRefresh={onRefreshResources}
    />
    {annotationProfile === "e621" ? <small>{copy.anthroReplacementNote}</small> : null}
    <ToggleField id="classify-overwrite-json" label={t("overwriteJson")} checked={draft.classify.overwriteJson} disabled={taskLocked || rebuild || !classifyEnabled} onChange={(overwriteJson) => onClassifyChange({ overwriteJson })} copy={guidanceCopy} guidance={{ description: t("fieldHelp_overwriteJson"), defaultValue: draft.classify.overwriteJson ? t("fieldEnabled") : t("fieldDisabled") }} />
    <ToggleField id="classify-overwrite-count" label={t("overwriteCount")} checked={draft.classify.overwriteCount} disabled={taskLocked || rebuild || !classifyEnabled} onChange={(overwriteCount) => onClassifyChange({ overwriteCount })} copy={guidanceCopy} guidance={{ description: t("fieldHelp_overwriteCount"), defaultValue: draft.classify.overwriteCount ? t("fieldEnabled") : t("fieldDisabled") }} />
  </div>;
}
