import { ResourcePicker, type ResourcePickerProps } from "../ResourcePicker";
import { FormField, ToggleField, type FieldGuidanceCopy } from "../FormField";
import type { Draft } from "../../draft";
import type { UiLanguage } from "../../i18n";

type TokenizerResource = {
  resourceId: string;
  resourceVersion: string;
  available: boolean;
  compatibility: { status: string };
  profile: string;
  displayName: Record<UiLanguage, string>;
  description: Record<UiLanguage, string>;
  distribution: { mode: "bundled" | "local-only"; sourceUrl?: string; licenseUrl?: string };
  adjustableCategories: string[];
  excludedCategories: string[];
  defaultForProfiles: string[];
  officialModelId?: string;
  contextLimit?: number;
};

export type TokenBudgetStepProps = {
  draft: Pick<Draft, "tokenBudget">;
  defaults: Pick<Draft, "tokenBudget">;
  taskLocked: boolean;
  language: UiLanguage;
  resources: TokenizerResource[];
  resourcesLoading: boolean;
  resourceError: string | null;
  invalidResourceCount: number;
  resourcePickerCopy: ResourcePickerProps["copy"];
  t: (key: string, values?: Record<string, string | number>) => string;
  guidanceCopy: FieldGuidanceCopy;
  onTokenBudgetChange: (patch: Partial<Draft["tokenBudget"]>) => void;
  onRefreshResources: () => void;
};

export function TokenBudgetStep({
  draft, defaults, taskLocked, language, resources, resourcesLoading, resourceError, invalidResourceCount, resourcePickerCopy, t, guidanceCopy,
  onTokenBudgetChange, onRefreshResources,
}: TokenBudgetStepProps) {
  const selected = resources.find((item) => item.resourceId === draft.tokenBudget.resourceId);
  const cap = selected?.contextLimit;
  const controlsDisabled = taskLocked || !draft.tokenBudget.enabled;
  const invalidMax = !Number.isInteger(draft.tokenBudget.maxTokens) || draft.tokenBudget.maxTokens < 1
    || (typeof cap === "number" && draft.tokenBudget.maxTokens > cap);
  const selectResource = (resourceId: string) => {
    const next = resources.find((item) => item.resourceId === resourceId);
    const nextCap = next?.contextLimit;
    onTokenBudgetChange({
      resourceId,
      maxTokens: typeof nextCap === "number" && draft.tokenBudget.maxTokens > nextCap ? nextCap : draft.tokenBudget.maxTokens,
    });
  };

  return <div className="option-stack token-budget-step" data-config-surface="token-budget">
    <ToggleField id="token-budget-enabled" label={t("enableTokenBudget")} checked={draft.tokenBudget.enabled} disabled={taskLocked} onChange={(enabled) => onTokenBudgetChange({ enabled })} copy={guidanceCopy} guidance={{ description: t("fieldHelp_tokenBudgetEnabled"), defaultValue: defaults.tokenBudget.enabled ? t("fieldEnabled") : t("fieldDisabled") }} />
    <div className="form-grid">
      <FormField id="token-budget-max" label={t("maximumTrainingTokens")} copy={guidanceCopy} guidance={{ description: t("fieldHelp_maximumTokens"), defaultValue: String(defaults.tokenBudget.maxTokens), range: `1-${cap ?? t("tokenizerContextLimit")}` }}>
        <input id="token-budget-max" disabled={controlsDisabled} type="number" min="1" max={cap} step="1" value={draft.tokenBudget.maxTokens} onChange={(event) => onTokenBudgetChange({ maxTokens: Number(event.target.value) })} />
      </FormField>
    </div>
    {invalidMax && <small className="resource-error" role="alert">{t("tokenBudgetMaxInvalid")}</small>}
    <ResourcePicker
      id="tokenizer-resource"
      label={t("tokenizerResource")}
      language={language}
      profile="shared"
      selectedId={draft.tokenBudget.resourceId}
      resources={resources}
      loading={resourcesLoading}
      error={resourceError}
      invalidCount={invalidResourceCount}
      selectionDisabled={controlsDisabled}
      refreshDisabled={taskLocked}
      note={typeof cap === "number" ? `${t("tokenizerContextLimit")}: ${cap}` : t("tokenizerUnavailable")}
      guidance={{ description: t("fieldHelp_tokenizer") }}
      guidanceCopy={guidanceCopy}
      copy={resourcePickerCopy}
      onChange={selectResource}
      onRefresh={onRefreshResources}
    />
    {draft.tokenBudget.enabled && (!selected || typeof cap !== "number") && <p className="resource-warning" role="status">{t("tokenizerUnavailable")} <code>Import-TokenizerResources.bat -Apply</code>{selected?.distribution.sourceUrl && <>: <a href={selected.distribution.sourceUrl} target="_blank" rel="noreferrer">{selected.distribution.sourceUrl}</a></>}</p>}
  </div>;
}
