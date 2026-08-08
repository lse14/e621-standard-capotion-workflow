import type { FormEvent } from "react";
import type { Draft } from "../../draft";
import { FormField, ToggleField, type FieldGuidanceCopy } from "../FormField";
import { NlApiTools } from "../NlApiTools";

type Translate = (key: string, values?: Record<string, string | number>) => string;

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
  t: Translate;
  guidanceCopy: FieldGuidanceCopy;
  profileHelp: string;
  onNlChange: (patch: Partial<Draft["nl"]>) => void;
  onApiPolicyChange: (patch: Partial<Draft["nl"]["apiPolicy"]>) => void;
  onRestoreDefaultPrompt: () => void;
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

export function NlStep({
  draft, defaults, taskLocked, attemptBudget, profile, profiles, secret, profileSavePending, credentialDeletePending, t,
  onNlChange, onApiPolicyChange, onRestoreDefaultPrompt, onConcurrencyChange, onRequestsPerMinuteChange,
  onUnlimitedRpmChange, onAttemptBudgetChange, onProfileSelect, onProfileChange, onSecretChange, onSaveProfile, onClearSecret, guidanceCopy, profileHelp,
}: NlStepProps) {
  const apiDisabled = taskLocked || !draft.nl.enabled || !draft.nl.apiEnabled;
  const unlimitedRpm = draft.nl.apiPolicy.maxRequestsPerMinute === "unlimited";
  const submitProfile = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    onSaveProfile();
  };
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

    <section className="settings-section">
      <h3>{t("userSupplementSection")}</h3>
      <FormField label={t("userSupplement")} id="nl-user-supplement" wide copy={guidanceCopy} guidance={{ description: t("fieldHelp_userSupplement") }}>
        <textarea id="nl-user-supplement" disabled={taskLocked || !draft.nl.enabled} rows={5} maxLength={4096} value={draft.nl.systemPrompt} onChange={(event) => onNlChange({ systemPrompt: event.target.value })} />
        <div className="step-actions"><button className="secondary" type="button" disabled={taskLocked || !draft.nl.enabled} onClick={onRestoreDefaultPrompt}>{t("restoreDefaultPrompt")}</button><small className="field-help">{draft.nl.promptVersion}</small></div>
      </FormField>
    </section>

    <section className="settings-section">
      <h3>{t("nlRequestLimits")}</h3>
      <div className="form-grid">
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
    </section>

    <section className="settings-section">
      <h3>{t("nlApiProfile")}</h3>
      <p className="hint">{profileHelp}</p>
      <FormField label={t("profileId")} id="nl-profile-select" copy={guidanceCopy} guidance={{ description: t("fieldHelp_profileId") }}>
        <select id="nl-profile-select" disabled={taskLocked} value={profile.profileId} onChange={(event) => onProfileSelect(event.target.value)}>
          <option value={profile.profileId}>{profile.profileId}</option>
          {profileOptions.map((item) => <option key={item.profileId} value={item.profileId}>{item.profileId}</option>)}
        </select>
      </FormField>
      <form onSubmit={submitProfile} className="form-grid" aria-busy={profileSavePending || credentialDeletePending}>
        <FormField label={t("profileId")} id="nl-profile-id" copy={guidanceCopy} guidance={{ description: t("fieldHelp_profileId") }}>
          <input id="nl-profile-id" disabled={taskLocked} value={profile.profileId} onChange={(event) => onProfileChange({ ...profile, profileId: event.target.value })} />
        </FormField>
        <FormField label={t("endpoint")} id="nl-endpoint" copy={guidanceCopy} guidance={{ description: t("fieldHelp_endpoint") }}>
          <input id="nl-endpoint" disabled={taskLocked} value={profile.endpoint} onChange={(event) => onProfileChange({ ...profile, endpoint: event.target.value })} />
        </FormField>
        <FormField label={t("model")} id="nl-model" copy={guidanceCopy} guidance={{ description: t("fieldHelp_model") }}>
          <input id="nl-model" disabled={taskLocked} value={profile.model} onChange={(event) => onProfileChange({ ...profile, model: event.target.value })} />
        </FormField>
        <FormField label={t("backupModel")} id="nl-backup-model" copy={guidanceCopy} guidance={{ description: t("fieldHelp_backupModelProvider") }}>
          <input id="nl-backup-model" disabled={taskLocked || !draft.nl.apiPolicy.backupEnabled} value={profile.backupModel ?? ""} onChange={(event) => onProfileChange({ ...profile, backupModel: event.target.value.trim() ? event.target.value : null })} />
        </FormField>
        <FormField label={t("credentialRef")} id="nl-credential-ref" copy={guidanceCopy} guidance={{ description: t("fieldHelp_credentialRef") }}>
          <input id="nl-credential-ref" disabled={taskLocked} value={profile.apiCredentialRef} onChange={(event) => onProfileChange({ ...profile, apiCredentialRef: event.target.value })} />
        </FormField>
        <FormField label={t("apiKey")} id="nl-api-key" wide copy={guidanceCopy} guidance={{ description: t("fieldHelp_apiKey") }}>
          <input id="nl-api-key" disabled={taskLocked} type="password" value={secret} autoComplete="off" onChange={(event) => onSecretChange(event.target.value)} placeholder={profile.hasCredential ? t("savedKey") : t("dpapiKey")} />
        </FormField>
        <div className="step-actions wide"><button disabled={taskLocked || profileSavePending} aria-busy={profileSavePending} type="submit">{t("saveProfile")}</button><button className="secondary" disabled={taskLocked || credentialDeletePending || !profile.apiCredentialRef} aria-busy={credentialDeletePending} type="button" onClick={onClearSecret}>{t("clearKey")}</button></div>
      </form>
      <NlApiTools disabled={apiDisabled} endpoint={profile.endpoint} model={profile.model} apiCredentialRef={profile.apiCredentialRef} transientApiKey={secret} onModelChange={(model) => onProfileChange({ ...profile, model })} t={t} />
    </section>
  </div>;
}
