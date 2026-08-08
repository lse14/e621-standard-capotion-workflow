import { useEffect, useMemo, useState } from "react";
import {
  createNlPromptPreset,
  deleteNlPromptPreset,
  discoverNlModels,
  getNlPromptPreset,
  listNlPromptPresets,
  testNlMessage,
  updateNlPromptPreset,
  type ModelDiscoveryResult,
  type NlPromptPresetDetail,
  type NlPromptPresetSummary,
  type TestMessageResult,
} from "../api";
import { FormField, makeFieldGuidanceCopy } from "./FormField";

type Translate = (key: string, values?: Record<string, string | number>) => string;

export type NlApiToolsProps = {
  disabled: boolean;
  endpoint: string;
  model: string;
  apiCredentialRef: string;
  transientApiKey: string;
  onModelChange: (model: string) => void;
  t: Translate;
};

type Feedback = { kind: "discovery"; result: ModelDiscoveryResult } | { kind: "test"; result: TestMessageResult };

const BUILTIN_PRESET_ID = "builtin:nl-default-prompt-v4-base";

function diagnosticCredentials(endpoint: string, apiCredentialRef: string, transientApiKey: string) {
  const credentials = { endpoint, apiCredentialRef };
  return transientApiKey.trim() ? { ...credentials, apiKey: transientApiKey } : credentials;
}

export function NlApiTools({ disabled, endpoint, model, apiCredentialRef, transientApiKey, onModelChange, t }: NlApiToolsProps) {
  const guidanceCopy = useMemo(() => makeFieldGuidanceCopy(t), [t]);
  const [presets, setPresets] = useState<NlPromptPresetSummary[]>([]);
  const [selectedId, setSelectedId] = useState(BUILTIN_PRESET_ID);
  const [selected, setSelected] = useState<NlPromptPresetDetail | null>(null);
  const [presetName, setPresetName] = useState("");
  const [basePrompt, setBasePrompt] = useState("");
  const [savedName, setSavedName] = useState("");
  const [savedPrompt, setSavedPrompt] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState<"preset" | "discovery" | "test" | null>(null);

  const dirty = presetName !== savedName || basePrompt !== savedPrompt;
  const isBuiltin = selected?.builtIn ?? selectedId === BUILTIN_PRESET_ID;

  const applyDetail = (detail: NlPromptPresetDetail) => {
    setSelected(detail);
    setSelectedId(detail.presetId);
    setPresetName(detail.name);
    setBasePrompt(detail.basePrompt);
    setSavedName(detail.name);
    setSavedPrompt(detail.basePrompt);
  };

  const loadPreset = async (presetId: string, confirmDirty = true) => {
    if (confirmDirty && dirty && !window.confirm(t("confirmDiscardPreset"))) {
      setSelectedId(selected?.presetId ?? BUILTIN_PRESET_ID);
      return;
    }
    setPending("preset");
    try {
      applyDetail(await getNlPromptPreset(presetId));
    } catch (cause) {
      setFeedback({ kind: "discovery", result: {
        ok: false, latencyMs: 0, models: [], errorCode: "preset_load_failed", errorReason: cause instanceof Error ? cause.message : t("presetLoadFailed"),
      } });
    } finally {
      setPending(null);
    }
  };

  useEffect(() => {
    let active = true;
    void (async () => {
      try {
        const result = await listNlPromptPresets();
        if (!active) return;
        setPresets(result.presets);
        const builtin = result.presets.find((preset) => preset.builtIn) ?? result.presets[0];
        if (builtin) applyDetail(await getNlPromptPreset(builtin.presetId));
      } catch (cause) {
        if (active) setFeedback({ kind: "discovery", result: {
          ok: false, latencyMs: 0, models: [], errorCode: "preset_load_failed", errorReason: cause instanceof Error ? cause.message : t("presetLoadFailed"),
        } });
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => { active = false; };
  }, []);

  const savePreset = async () => {
    if (!presetName.trim() || !basePrompt.trim()) return;
    setPending("preset");
    try {
      const body = { name: presetName, basePrompt };
      const detail = isBuiltin ? await createNlPromptPreset(body) : await updateNlPromptPreset(selectedId, body);
      setPresets((current) => {
        const next = current.filter((item) => item.presetId !== detail.presetId);
        return [...next, { ...detail }];
      });
      applyDetail(detail);
    } catch (cause) {
      setFeedback({ kind: "discovery", result: {
        ok: false, latencyMs: 0, models: [], errorCode: "preset_save_failed", errorReason: cause instanceof Error ? cause.message : t("presetSaveFailed"),
      } });
    } finally {
      setPending(null);
    }
  };

  const deletePreset = async () => {
    if (isBuiltin || !window.confirm(t("confirmDeletePreset"))) return;
    setPending("preset");
    try {
      await deleteNlPromptPreset(selectedId);
      const result = await listNlPromptPresets();
      setPresets(result.presets);
      await loadPreset(BUILTIN_PRESET_ID, false);
    } catch (cause) {
      setFeedback({ kind: "discovery", result: {
        ok: false, latencyMs: 0, models: [], errorCode: "preset_delete_failed", errorReason: cause instanceof Error ? cause.message : t("presetDeleteFailed"),
      } });
    } finally {
      setPending(null);
    }
  };

  const discover = async () => {
    if (!endpoint.trim() || pending) return;
    setPending("discovery");
    try {
      const result = await discoverNlModels(diagnosticCredentials(endpoint, apiCredentialRef, transientApiKey));
      setFeedback({ kind: "discovery", result });
      if (result.ok) setModels(result.models);
    } catch (cause) {
      setFeedback({ kind: "discovery", result: {
        ok: false, latencyMs: 0, models: [], errorCode: "request_failed", errorReason: cause instanceof Error ? cause.message : t("diagnosticFailed"),
      } });
    } finally {
      setPending(null);
    }
  };

  const sendTest = async () => {
    if (!endpoint.trim() || !model.trim() || !basePrompt.trim() || pending) return;
    setPending("test");
    try {
      const result = await testNlMessage({ ...diagnosticCredentials(endpoint, apiCredentialRef, transientApiKey), model, basePrompt });
      setFeedback({ kind: "test", result });
    } catch (cause) {
      setFeedback({ kind: "test", result: {
        ok: false, latencyMs: 0, actualModel: null, replyText: null, usage: null, errorCode: "request_failed", errorReason: cause instanceof Error ? cause.message : t("diagnosticFailed"),
      } });
    } finally {
      setPending(null);
    }
  };

  return <section className="nl-api-tools" aria-busy={loading || pending !== null}>
    <div className="form-grid">
      <FormField label={t("promptPreset")} id="nl-prompt-preset" copy={guidanceCopy} guidance={{ description: t("fieldHelp_promptPreset"), defaultValue: t("builtinPromptDefault") }}>
        <select id="nl-prompt-preset" disabled={disabled || loading || pending !== null} value={selectedId} onChange={(event) => void loadPreset(event.target.value)}>
          {presets.map((preset) => <option key={preset.presetId} value={preset.presetId}>{preset.name}</option>)}
        </select>
      </FormField>
      <FormField label={t("presetName")} id="nl-preset-name" copy={guidanceCopy} guidance={{ description: t("fieldHelp_presetFields") }}>
        <input id="nl-preset-name" disabled={disabled || loading} value={presetName} onChange={(event) => setPresetName(event.target.value)} />
      </FormField>
      <FormField label={t("basePrompt")} id="nl-base-prompt" wide copy={guidanceCopy} guidance={{ description: t("fieldHelp_presetFields") }}>
        <textarea id="nl-base-prompt" rows={8} disabled={disabled || loading} value={basePrompt} onChange={(event) => setBasePrompt(event.target.value)} />
      </FormField>
    </div>
    <div className="step-actions">
      <button type="button" disabled={disabled || loading || pending !== null || !presetName.trim() || !basePrompt.trim()} aria-busy={pending === "preset"} onClick={() => void savePreset()}>{isBuiltin ? t("saveAsCustom") : t("saveChanges")}</button>
      <button className="secondary" type="button" disabled={disabled || loading || pending !== null || isBuiltin} aria-busy={pending === "preset"} onClick={() => void deletePreset()}>{t("deletePreset")}</button>
    </div>
    <div className="form-grid nl-diagnostics">
      <FormField label={t("discoveredModels")} id="nl-discovered-models" wide copy={guidanceCopy} guidance={{ description: t("fieldHelp_modelDiscovery") }}>
        <select id="nl-discovered-models" disabled={disabled || pending !== null || models.length === 0} value={models.includes(model) ? model : ""} onChange={(event) => onModelChange(event.target.value)}>
          <option value="">{t("selectDiscoveredModel")}</option>
          {models.map((item) => <option key={item} value={item}>{item}</option>)}
        </select>
      </FormField>
      <div className="step-actions wide"><button className="secondary" type="button" disabled={disabled || pending !== null || !endpoint.trim()} aria-busy={pending === "discovery"} onClick={() => void discover()}>{t("getModels")}</button><button type="button" disabled={disabled || pending !== null || !endpoint.trim() || !model.trim() || !basePrompt.trim()} aria-busy={pending === "test"} onClick={() => void sendTest()}>{t("sendTestMessage")}</button></div>
    </div>
    {feedback?.kind === "discovery" && <div className="nl-api-feedback" aria-live="polite"><strong>{feedback.result.ok ? t("diagnosticSuccess") : t("diagnosticFailure")}</strong><span>{feedback.result.latencyMs} ms</span>{feedback.result.ok ? <span>{t("modelsFound", { count: feedback.result.models.length })}</span> : <span>{feedback.result.errorReason ?? t("diagnosticUnavailable")}</span>}</div>}
    {feedback?.kind === "test" && <div className="nl-api-feedback" aria-live="polite"><strong>{feedback.result.ok ? t("diagnosticSuccess") : t("diagnosticFailure")}</strong><span>{feedback.result.latencyMs} ms</span><span>{feedback.result.actualModel ?? t("diagnosticUnavailable")}</span><span>{feedback.result.replyText ?? ""}</span><span>{t("promptTokens")}: {feedback.result.usage?.promptTokens ?? t("diagnosticUnavailable")}</span><span>{t("completionTokens")}: {feedback.result.usage?.completionTokens ?? t("diagnosticUnavailable")}</span><span>{t("totalTokens")}: {feedback.result.usage?.totalTokens ?? t("diagnosticUnavailable")}</span>{!feedback.result.ok && <span>{feedback.result.errorReason ?? t("diagnosticUnavailable")}</span>}</div>}
  </section>;
}
