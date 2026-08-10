import { useEffect, useState } from "react";
import type { Draft } from "../../draft";
import { FormField, ToggleField, type FieldGuidanceCopy } from "../FormField";
import { NlApiTools, NlPromptPresetLibrary } from "../NlApiTools";

type Translate = (key: string, values?: Record<string, string | number>) => string;
type NlPresetType = "general" | "style" | "character";

type NlProfileForm = {
  profileId: string;
  endpoint: string;
  model: string;
  backupModel: string | null;
  apiCredentialRef: string;
  hasCredential: boolean;
};

export type NlStepProps = {
  draft: Pick<Draft, "nl">;
  defaults: Pick<Draft, "nl">;
  taskLocked: boolean;
  attemptBudget: string;
  profile: NlProfileForm;
  profiles: readonly NlProfileForm[];
  secret: string;
  profileSavePending: boolean;
  credentialDeletePending: boolean;
  diagnosticResetToken: number;
  t: Translate;
  guidanceCopy: FieldGuidanceCopy;
  profileHelp: string;
  onNlChange: (patch: Partial<Draft["nl"]>) => void;
  onApiPolicyChange: (patch: Partial<Draft["nl"]["apiPolicy"]>) => void;
  onConcurrencyChange: (value: string) => void;
  onRequestsPerMinuteChange: (value: string) => void;
  onUnlimitedRpmChange: (enabled: boolean) => void;
  onAttemptBudgetChange: (value: string) => void;
  onProfileSelect: (profileId: string) => void;
  onProfileChange: (profile: NlProfileForm) => void;
  onSecretChange: (value: string) => void;
  onSaveProfile: () => void;
  onClearSecret: () => void;
};

function distributionError(distribution: Draft["nl"]["lengthDistribution"]): boolean {
  const values = Object.values(distribution);
  return !values.every((value) => Number.isInteger(value) && value >= 0 && value <= 100)
    || values.reduce((total, value) => total + value, 0) !== 100;
}

export function NlStep({
  draft, defaults, taskLocked, attemptBudget, profile, profiles, secret, profileSavePending, credentialDeletePending, t,
  onNlChange, onApiPolicyChange, onConcurrencyChange, onRequestsPerMinuteChange,
  onUnlimitedRpmChange, onAttemptBudgetChange, onProfileSelect, onProfileChange, onSecretChange, onSaveProfile, onClearSecret, guidanceCopy, profileHelp, diagnosticResetToken,
}: NlStepProps) {
  const apiDisabled = taskLocked || !draft.nl.enabled || !draft.nl.apiEnabled;
  const apiCollapsed = !draft.nl.enabled || !draft.nl.apiEnabled;
  const unlimitedRpm = draft.nl.apiPolicy.maxRequestsPerMinute === "unlimited";
  const invalidDistribution = distributionError(draft.nl.lengthDistribution);
  const [promptDirty, setPromptDirty] = useState(false);
  const [keyVisible, setKeyVisible] = useState(false);
  useEffect(() => {
    if (!secret || taskLocked || apiDisabled) setKeyVisible(false);
  }, [apiDisabled, secret, taskLocked]);
  useEffect(() => {
    setKeyVisible(false);
  }, [diagnosticResetToken, profile.endpoint, profile.profileId]);
  const profileOptions = profiles.filter((item) => item.profileId !== profile.profileId);

  return <div className="nl-step settings-section" data-config-surface="nl">
    <section className="settings-section">
      <h3>{t("nlGenerationSettings")}</h3>
      <div className="form-grid">
        <ToggleField id="nl-enabled" label={t("enableNl")} checked={draft.nl.enabled} disabled={taskLocked} guidance={{ description: t("fieldHelp_enableNl") }} copy={guidanceCopy} onChange={(enabled) => onNlChange({ enabled })} />
        <ToggleField id="nl-reuse-original" label={t("reuseOriginalNl")} checked={draft.nl.reuseOriginalNl} disabled={taskLocked || !draft.nl.enabled} guidance={{ description: t("fieldHelp_reuseOriginalNl") }} copy={guidanceCopy} onChange={(reuseOriginalNl) => onNlChange({ reuseOriginalNl })} />
        <ToggleField id="nl-api-enabled" label={t("generateNl")} checked={draft.nl.apiEnabled} disabled={taskLocked || !draft.nl.enabled} guidance={{ description: t("fieldHelp_generateNl") }} copy={guidanceCopy} onChange={(apiEnabled) => onNlChange({ apiEnabled })} />
        <ToggleField id="nl-use-image" label={t("useImage")} checked={draft.nl.useImage} disabled={taskLocked || !draft.nl.enabled || !draft.nl.apiEnabled} guidance={{ description: t("fieldHelp_useImage") }} copy={guidanceCopy} onChange={(useImage) => onNlChange({ useImage })} />
        <ToggleField id="nl-use-json" label={t("useJsonContext")} checked={draft.nl.useFullJson} disabled={taskLocked || !draft.nl.enabled || !draft.nl.apiEnabled} guidance={{ description: t("fieldHelp_useJsonContext") }} copy={guidanceCopy} onChange={(useFullJson) => onNlChange({ useFullJson })} />
      </div>
    </section>

    <section className="settings-section nl-prompt-section">
      <h3>{t("nlPromptPresets")}</h3>
      <NlPromptPresetLibrary
        disabled={taskLocked || !draft.nl.enabled}
        promptText={draft.nl.systemPrompt}
        presetType={draft.nl.captionPreset as NlPresetType}
        t={t}
        guidanceCopy={guidanceCopy}
        onPromptChange={(systemPrompt) => onNlChange({ systemPrompt, promptVersion: "nl-default-prompt-v4" })}
        onPresetTypeChange={(captionPreset) => onNlChange({ captionPreset, promptVersion: "nl-default-prompt-v4" })}
        onDirtyChange={setPromptDirty}
      />
      <div className="form-grid nl-length-settings">
        {(["short", "medium", "long"] as const).map((tier) => <FormField key={tier} id={`nl-length-${tier}`} label={t(`lengthTier_${tier}`)} copy={guidanceCopy} guidance={{ description: t("fieldHelp_lengthTiers"), defaultValue: String(defaults.nl.lengthDistribution[tier]), range: "0-100 integer; total 100" }}>
          <input id={`nl-length-${tier}`} disabled={taskLocked} type="number" min="0" max="100" step="1" value={draft.nl.lengthDistribution[tier]} onChange={(event) => onNlChange({ lengthDistribution: { ...draft.nl.lengthDistribution, [tier]: Number(event.target.value) } })} />
        </FormField>)}
      </div>
      {invalidDistribution && <small className="resource-error" role="alert">{t("lengthDistributionInvalid")}</small>}
    </section>

    <section className={`settings-section nl-api-section${apiCollapsed ? " is-collapsed" : ""}`}>
      <details open={!apiCollapsed} onClick={(event) => { if (apiCollapsed) event.preventDefault(); }}>
        <summary><span>{t("nlApiProfile")}</span><small>{apiCollapsed ? t("apiControlsCollapsed") : t("apiControlsExpanded")}</small></summary>
        <div className="nl-api-content">
          <p className="hint">{profileHelp}</p>
          <div className="form-grid">
            <FormField label={t("profileId")} id="nl-profile-select" copy={guidanceCopy} guidance={{ description: t("fieldHelp_profileId") }}>
              <select id="nl-profile-select" disabled={taskLocked} value={profile.profileId} onChange={(event) => onProfileSelect(event.target.value)}>
                <option value={profile.profileId}>{profile.profileId}</option>
                {profileOptions.map((item) => <option key={item.profileId} value={item.profileId}>{item.profileId}</option>)}
              </select>
            </FormField>
          </div>
          <NlApiTools
            disabled={apiDisabled}
            profileId={profile.profileId}
            endpoint={profile.endpoint}
            model={profile.model}
            backupModel={profile.backupModel}
            backupEnabled={draft.nl.apiPolicy.backupEnabled}
            apiCredentialRef={profile.apiCredentialRef}
            transientApiKey={secret}
            promptText={draft.nl.systemPrompt}
            promptDirty={promptDirty}
            resetToken={diagnosticResetToken}
            onEndpointChange={(endpoint) => onProfileChange({ ...profile, endpoint })}
            onModelChange={(model) => onProfileChange({ ...profile, model })}
            onBackupModelChange={(backupModel) => onProfileChange({ ...profile, backupModel })}
            t={t}
            guidanceCopy={guidanceCopy}
          >
            <div className="form-grid nl-api-secret-grid">
              <FormField label={t("apiKey")} id="nl-api-key" wide copy={guidanceCopy} guidance={{ description: t("fieldHelp_apiKey") }}>
                <div className="secret-input-control">
                  <input id="nl-api-key" disabled={taskLocked || !draft.nl.enabled || !draft.nl.apiEnabled} type={keyVisible ? "text" : "password"} value={secret} autoComplete="off" onChange={(event) => onSecretChange(event.target.value)} placeholder={profile.hasCredential ? t("savedKey") : t("dpapiKey")} />
                  <button className="icon-button secondary" type="button" disabled={taskLocked || apiDisabled || !secret} aria-label={t(keyVisible ? "hideApiKey" : "showApiKey")} title={t(keyVisible ? "hideApiKey" : "showApiKey")} onClick={() => setKeyVisible((visible) => !visible)}><span aria-hidden="true">&#128065;</span></button>
                </div>
              </FormField>
            </div>
            <div className="step-actions"><button disabled={taskLocked || profileSavePending} aria-busy={profileSavePending} type="button" onClick={onSaveProfile}>{t("saveProfile")}</button><button className="secondary" disabled={taskLocked || credentialDeletePending || (!profile.hasCredential && !profile.apiCredentialRef)} aria-busy={credentialDeletePending} type="button" onClick={onClearSecret}>{t("clearKey")}</button></div>

            <div className="form-grid nl-request-limits">
              <FormField label={t("concurrency")} id="nl-concurrency" copy={guidanceCopy} guidance={{ description: t("fieldHelp_concurrency"), defaultValue: String(defaults.nl.apiPolicy.concurrency), range: "1-16" }}>
                <input id="nl-concurrency" disabled={apiDisabled} type="number" min="1" max="16" step="1" value={draft.nl.apiPolicy.concurrency} onChange={(event) => onConcurrencyChange(event.target.value)} />
              </FormField>
              <FormField label={t("requestsPerMinute")} id="nl-rpm" copy={guidanceCopy} guidance={{ description: t("fieldHelp_requestsPerMinute"), defaultValue: String(defaults.nl.apiPolicy.maxRequestsPerMinute === "unlimited" ? 60 : defaults.nl.apiPolicy.maxRequestsPerMinute), range: "1-100000" }}>
                <input id="nl-rpm" disabled={apiDisabled || unlimitedRpm} type="number" min="1" max="100000" step="1" value={unlimitedRpm ? 60 : draft.nl.apiPolicy.maxRequestsPerMinute} onChange={(event) => onRequestsPerMinuteChange(event.target.value)} />
              </FormField>
              <ToggleField id="nl-unlimited-rpm" label={t("unlimitedRpm")} checked={unlimitedRpm} disabled={apiDisabled} guidance={{ description: t("fieldHelp_unlimitedRpm"), defaultValue: t("fieldDisabled") }} copy={guidanceCopy} onChange={onUnlimitedRpmChange} />
              <FormField label={t("attemptBudget")} id="nl-attempt-budget" copy={guidanceCopy} guidance={{ description: t("fieldHelp_attemptBudget") }}>
                <input id="nl-attempt-budget" disabled={apiDisabled} inputMode="numeric" value={attemptBudget} onChange={(event) => onAttemptBudgetChange(event.target.value)} /><small className="field-help">{t("attemptBudgetHelp")}</small>
              </FormField>
              <ToggleField id="nl-backup-enabled" label={t("enableBackupModel")} checked={draft.nl.apiPolicy.backupEnabled} disabled={apiDisabled} guidance={{ description: t("fieldHelp_backupModel"), defaultValue: t("fieldDisabled") }} copy={guidanceCopy} onChange={(backupEnabled) => onApiPolicyChange({ backupEnabled })} />
            </div>
          </NlApiTools>
        </div>
      </details>
    </section>
  </div>;
}
