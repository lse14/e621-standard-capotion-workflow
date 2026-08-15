import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  addNlBudget, cancelJob, confirmNlOutcomes, confirmWorkspace, deleteNlSecret, discardJob, listNlProfiles, manualNlRetry, manualNlWrite,
  pauseJob, preflightJob, recoverJob, restoreOriginalAnnotations, resumeJob, saveNlProfile,
  setJobPin, repairJob, saveNlSecret, startPipeline, type NlProfile, type OcrExecutionRequest, type PreflightSummary,
  type AnnotationProfile, type PipelineModuleId, type ResourceCatalogResponse, type ResourceKind,
} from "./api";
import { CountReviewPanel } from "./CountReviewPanel";
import { IssuePanel } from "./components/IssuePanel";
import { makeFieldGuidanceCopy, ToggleField } from "./components/FormField";
import { resourceSelectable } from "./components/ResourcePicker";
import { TaskMonitor } from "./components/TaskMonitor";
import { WorkflowRail } from "./components/WorkflowRail";
import { CaptionStep } from "./components/steps/CaptionStep";
import { ClassifyStep } from "./components/steps/ClassifyStep";
import { ExportStep } from "./components/steps/ExportStep";
import { NlStep } from "./components/steps/NlStep";
import { OcrStep } from "./components/steps/OcrStep";
import { PolicyStep } from "./components/steps/PolicyStep";
import { ReplaceStep } from "./components/steps/ReplaceStep";
import { SetupStep } from "./components/steps/SetupStep";
import { TokenBudgetStep } from "./components/steps/TokenBudgetStep";
import { applyResourceCatalogDefaults, newDraft, newOcrExecution, patchDraftSection, replaceDraftValue, type Draft, type DraftSectionKey } from "./draft";
import { text } from "./appCopy";
import { isActiveJobStatus, useJobMonitor } from "./hooks/useJobMonitor";
import { useResourceCatalog } from "./hooks/useResourceCatalog";
import { loadUiLanguage, moduleLabel, saveUiLanguage, statusLabel, translate, type UiLanguage } from "./i18n";
import { TokenBudgetReviewPanel } from "./TokenBudgetReviewPanel";
import "./styles.css";

type StepId = "setup" | PipelineModuleId;
type PendingAction =
  | "preflight" | "confirm_workspace" | "start" | "repair" | "recover"
  | "terminate" | "pause" | "resume" | "pin" | "discard" | "restore" | "nl_manual_retry" | "nl_manual_write"
  | "nl_budget" | "nl_confirm_outcomes"
  | "profile_save" | "credential_delete"
  | null;
type ActionName = Exclude<PendingAction, null>;

const draftModuleOrder: readonly PipelineModuleId[] = ["caption", "classify", "replace", "ocr", "nl", "count_review", "dropout", "token_budget", "export"];
const emptyProfile: NlProfile = { profileId: "default", endpoint: "", model: "", backupModel: null, apiCredentialRef: "", systemPrompt: "", apiPolicy: { maxRequestsPerMinute: 60 }, hasCredential: false };
const CREDENTIAL_REFERENCE = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;

function effectiveCredentialReference(profile: NlProfile): string {
  return CREDENTIAL_REFERENCE.test(profile.apiCredentialRef) ? profile.apiCredentialRef : `nl-profile:${profile.profileId}`;
}

export function App() {
  const [language, setLanguage] = useState<UiLanguage>(loadUiLanguage);
  const [profiles, setProfiles] = useState<NlProfile[]>([]);
  const [draft, setDraft] = useState<Draft>(newDraft);
  const draftDefaults = useMemo(newDraft, []);
  const [ocrExecution, setOcrExecution] = useState<OcrExecutionRequest>(newOcrExecution);
  const [preflight, setPreflight] = useState<PreflightSummary | null>(null);
  const [profile, setProfile] = useState<NlProfile>(emptyProfile);
  const [secret, setSecret] = useState("");
  const [diagnosticResetToken, setDiagnosticResetToken] = useState(0);
  const [budget, setBudget] = useState("100");
  const [triggerInput, setTriggerInput] = useState("");
  const [attemptBudget, setAttemptBudget] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const [pendingActions, setPendingActions] = useState<ReadonlySet<ActionName>>(() => new Set<ActionName>());
  const pendingActionRef = useRef<Set<ActionName>>(new Set<ActionName>());
  const [activeStep, setActiveStep] = useState(0);
  const [openGuide, setOpenGuide] = useState(true);
  const t = (key: string, values?: Record<string, string | number>) => translate(language, key, values);
  const guidanceCopy = makeFieldGuidanceCopy(t);
  const copy = text[language];
  const isActionPending = (name: ActionName) => pendingActions.has(name);
  const applyCatalog = useCallback((catalog: ResourceCatalogResponse, invalidate: boolean) => {
    setDraft((current) => applyResourceCatalogDefaults(current, catalog));
    if (invalidate) setPreflight(null);
  }, []);
  const {
    catalog: resourceCatalog,
    loading: resourcesLoading,
    error: resourceError,
    refresh: refreshResources,
  } = useResourceCatalog({ failureMessage: copy.resourceLoadFailed, onCatalog: applyCatalog });
  const {
    jobId,
    jobs,
    jobsCursor,
    jobsLoading,
    jobsError,
    snapshot,
    snapshotLoading,
    snapshotError,
    issueCursor,
    selectJob,
    refreshJobs,
    loadMoreJobs,
    refreshSnapshot,
    firstIssuePage,
    nextIssuePage,
  } = useJobMonitor({
    jobListFailureMessage: t("jobListFailed"),
    jobRequestFailureMessage: t("jobRequestFailed"),
  });

  const moduleOrder = snapshot?.moduleOrder?.length ? snapshot.moduleOrder : draftModuleOrder;
  const steps = useMemo(() => {
    const guides: Record<PipelineModuleId, readonly string[]> = {
      caption: copy.captionGuide,
      classify: copy.classifyGuide,
      replace: draft.profile === "danbooru" ? copy.replaceSkippedGuide : copy.replaceGuide,
      ocr: copy.ocrGuide,
      nl: copy.nlGuide,
      count_review: copy.countReviewGuide,
      dropout: copy.policyGuide,
      token_budget: copy.tokenBudgetGuide,
      export: copy.exportGuide,
    };
    return [
      { id: "setup" as StepId, title: copy.setup, guide: copy.setupGuide },
      ...moduleOrder.map((id) => ({ id: id as StepId, title: moduleLabel(language, id), guide: guides[id] })),
    ];
  }, [copy, draft.profile, language, moduleOrder]);

  const updateSection = <K extends DraftSectionKey>(name: K, patch: Partial<Draft[K]>) => {
    setPreflight(null);
    setDraft((current) => patchDraftSection(current, name, patch));
  };
  const updateInputTxtMode = (inputTxtMode: Draft["caption"]["inputTxtMode"]) => {
    setPreflight(null);
    setDraft((current) => patchDraftSection(current, "caption", {
      inputTxtMode,
      enabled: inputTxtMode === "nl" ? true : current.caption.enabled,
      overwriteTxt: false,
    }));
  };
  const selectedTagger = resourceCatalog?.resources.find((item) =>
    item.kind === "tagging-model" && item.resourceId === draft.caption.resourceId,
  );
  // F37: the three frozen threshold modes carry mutually exclusive fields, so the section is rebuilt.
  const updateThresholdMode = (mode: Draft["caption"]["thresholdMode"]) => {
    const defaults = selectedTagger?.defaultThresholds ?? {};
    setPreflight(null);
    setDraft((current) => {
      const base: Draft["caption"] = {
        enabled: current.caption.enabled,
        thresholdMode: mode,
        overwriteTxt: current.caption.overwriteTxt,
        inputTxtMode: current.caption.inputTxtMode,
        taggerFallbackOnMissingTxt: current.caption.taggerFallbackOnMissingTxt,
        resourceId: current.caption.resourceId,
      };
      return replaceDraftValue(current, "caption",
        mode === "uniform" ? { ...base, uniformThreshold: defaults.general ?? Object.values(defaults)[0] ?? 0.5 }
        : mode === "per_category" ? { ...base, categoryThresholds: { ...defaults } }
        : base);
    });
  };
  const selectTagger = (resourceId: string) => {
    const resource = resourceCatalog?.resources.find((item) => item.resourceId === resourceId);
    if (!resourceSelectable(resource)) return;
    setPreflight(null);
    setDraft((current) => replaceDraftValue(current, "caption", {
        enabled: current.caption.enabled,
        thresholdMode: "model_default",
        overwriteTxt: current.caption.overwriteTxt,
        inputTxtMode: current.caption.inputTxtMode,
        taggerFallbackOnMissingTxt: current.caption.taggerFallbackOnMissingTxt,
        resourceId,
      }));
  };
  const selectAnnotationProfile = (nextProfile: AnnotationProfile) => {
    const defaults = resourceCatalog?.defaults[nextProfile];
    if (!defaults) return;
    const classification = resourceCatalog?.resources.find(
      (item) => item.kind === "classification-index" && item.resourceId === defaults.classificationIndex,
    );
    setPreflight(null);
    setDraft((current) => ({
      ...current,
      schemaVersion: 8,
      profile: nextProfile,
      caption: {
        enabled: current.caption.inputTxtMode === "nl" || current.caption.enabled,
        thresholdMode: "model_default",
        overwriteTxt: false,
        inputTxtMode: current.caption.inputTxtMode,
        taggerFallbackOnMissingTxt: current.caption.taggerFallbackOnMissingTxt,
        resourceId: defaults.taggingModel,
      },
      classify: {
        ...current.classify,
        resourceId: defaults.classificationIndex,
        wikiDataSourceId: typeof classification?.metadata.wikiDataSourceId === "string"
          ? classification.metadata.wikiDataSourceId
          : current.classify.wikiDataSourceId,
      },
      replace: nextProfile === "danbooru"
        ? { enabled: false, indexMode: "bundled" }
        : { enabled: true, indexMode: "bundled", resourceId: defaults.replacementIndex },
      dropout: {
        ...current.dropout,
        quality: { ...current.dropout.quality, resourceId: defaults.dropoutModel },
      },
    }));
  };
  const updateCategoryThreshold = (category: string, value: string) => {
    setPreflight(null);
    setDraft((current) => patchDraftSection(current, "caption", {
      categoryThresholds: { ...current.caption.categoryThresholds, [category]: Number(value) },
    }));
  };
  const updateTriggerTerms = (value: string) => {
    setTriggerInput(value);
    updateSection("captionFormat", { triggerTerms: value.split(",").map((term) => term.trim()).filter(Boolean) });
  };
  const updateApiPolicy = (patch: Partial<Draft["nl"]["apiPolicy"]>) => {
    setPreflight(null);
    setDraft((current) => patchDraftSection(current, "nl", {
      apiPolicy: { ...current.nl.apiPolicy, ...patch },
    }));
  };
  // The runtime bounds (nl_runner.DEFAULT_POLICY) are enforced here so a cleared field cannot
  // fail the whole task later.
  const clamp = (value: string, low: number, high: number, fallback: number) =>
    Math.min(high, Math.max(low, Math.round(Number(value)) || fallback));
  // An empty budget must remove the key so the backend keeps deriving it from the dataset size.
  const updateAttemptBudget = (value: string) => {
    setAttemptBudget(value);
    setPreflight(null);
    setDraft((current) => {
      const { maxHttpAttempts: _frozen, ...rest } = current.nl.apiPolicy;
      return patchDraftSection(current, "nl", {
        apiPolicy: /^[1-9]\d*$/.test(value) ? { ...rest, maxHttpAttempts: Number(value) } : rest,
      });
    });
  };
  const updateDropoutNested = <K extends "artist" | "quality" | "appearanceNl">(
    section: K,
    patch: Partial<Draft["dropout"][K]>,
  ) => {
    setPreflight(null);
    setDraft((current) => ({
      ...current,
      dropout: {
        ...current.dropout,
        [section]: { ...current.dropout[section], ...patch },
      },
    } as Draft));
  };
  const updateAppearanceProbability = (group: "solo" | "nonSolo" | "unknown", key: "dropNl" | "dropAppearance", value: string) => {
    setPreflight(null);
    setDraft((current) => patchDraftSection(current, "dropout", {
      appearanceNl: {
        ...current.dropout.appearanceNl,
        [group]: { ...current.dropout.appearanceNl[group], [key]: Number(value) },
      },
    }));
  };
  const invalidatePreflight = () => setPreflight(null);

  const refreshProfiles = async (preferredProfileId = draft.nl.apiProfileId): Promise<NlProfile[] | undefined> => {
    try {
      const nextProfiles = (await listNlProfiles()).profiles;
      setProfiles(nextProfiles);
      const selected = nextProfiles.find((item) => item.profileId === preferredProfileId)
        ?? nextProfiles.find((item) => item.profileId === "default")
        ?? nextProfiles[0];
      if (selected) {
        setProfile(selected);
        setDraft((current) => ({ ...current, nl: { ...current.nl, apiProfileId: selected.profileId } }));
      }
      return nextProfiles;
    } catch (cause) {
      setActionError(cause instanceof Error ? cause.message : t("profileRequestFailed"));
      return undefined;
    }
  };

  useEffect(() => { void refreshProfiles(); }, []);
  useEffect(() => { saveUiLanguage(language); document.documentElement.lang = language; }, [language]);
  useEffect(() => {
    if (activeStep >= steps.length) setActiveStep(Math.max(0, steps.length - 1));
  }, [activeStep, steps.length]);
  const runAction = async <T,>(name: ActionName, operation: () => Promise<T>, failureMessage = t("controlRequestFailed")): Promise<T | undefined> => {
    if (pendingActionRef.current.has(name)) return undefined;
    pendingActionRef.current.add(name);
    setPendingActions((current) => new Set(current).add(name));
    setActionError(null);
    try {
      return await operation();
    } catch (cause) {
      setActionError(cause instanceof Error ? cause.message : failureMessage);
      return undefined;
    } finally {
      pendingActionRef.current.delete(name);
      setPendingActions((current) => {
        const next = new Set(current);
        next.delete(name);
        return next;
      });
    }
  };
  const control = <T,>(name: ActionName, operation: () => Promise<T>) => runAction(name, async () => {
    const result = await operation();
    await refreshSnapshot();
    return result;
  });
  const lifecycleControl = <T,>(name: ActionName, selectedJobId: string, operation: () => Promise<T>) => runAction(name, async () => {
    const result = await operation();
    await Promise.all([refreshJobs(), refreshSnapshot(selectedJobId, true)]);
    return result;
  });
  const runPreflight = async () => {
    const distribution = draft.nl.lengthDistribution;
    const distributionValues = Object.values(distribution);
    if (!distributionValues.every((value) => Number.isInteger(value) && value >= 0 && value <= 100)
      || distributionValues.reduce((total, value) => total + value, 0) !== 100) {
      setActionError(t("lengthDistributionInvalid"));
      return;
    }
    if (draft.tokenBudget.enabled) {
      const tokenizer = resourceCatalog?.resources.find((item) => item.kind === "tokenizer" && item.resourceId === draft.tokenBudget.resourceId);
      if (!tokenizer?.available || !Number.isInteger(tokenizer.contextLimit)
        || tokenizer.contextLimit! < 1 || !Number.isInteger(draft.tokenBudget.maxTokens)
        || draft.tokenBudget.maxTokens < 1 || draft.tokenBudget.maxTokens > tokenizer.contextLimit!) {
        setActionError(t("tokenizerPreflightRequired"));
        return;
      }
    }
    const result = await runAction("preflight", () => preflightJob({
      config: draft,
      ...(draft.ocr.enabled ? { ocrExecution } : {}),
    }), t("preflightFailed"));
    if (!result) return;
    setPreflight(result);
    selectJob(result.jobId);
    void refreshJobs();
  };
  const prepareWorkspace = async () => {
    if (!preflight) return;
    const rebuild = draft.overwriteMode === "rebuild";
    if (!window.confirm(rebuild ? t("confirmRebuild") : t("confirmWorkspacePrompt"))) return;
    await control("confirm_workspace", () => confirmWorkspace(preflight.jobId, rebuild));
  };
  const startRepair = async () => {
    const result = await runAction("repair", () => repairJob(jobId), t("repairStartFailed"));
    if (!result) return;
    selectJob(result.jobId);
    setPreflight(null);
    void refreshJobs();
  };
  const startManualNlRetry = (issue: { issueId: string; sampleId: number }) => {
    if (!window.confirm(t("confirmNlManualRetry"))) return;
    void control("nl_manual_retry", async () => {
      const result = await manualNlRetry(jobId, { issueId: issue.issueId });
      selectJob(result.jobId);
      await refreshJobs();
      return result;
    });
  };
  const startManualNlWrite = (issue: { issueId: string; sampleId: number }, nl: string) => {
    if (!window.confirm(t("confirmNlManualWrite"))) return;
    void control("nl_manual_write", async () => {
      const result = await manualNlWrite(jobId, { issueId: issue.issueId, nl });
      selectJob(result.jobId);
      await refreshJobs();
      return result;
    });
  };
  const submitProfile = async () => {
    // F25: the prompt the runtime sends is the task's, so the profile only mirrors it.
    const saved = await runAction("profile_save", async () => {
      const result = await saveNlProfile({ ...profile, apiCredentialRef: effectiveCredentialReference(profile), systemPrompt: draft.nl.systemPrompt });
      if (secret) await saveNlSecret(result.apiCredentialRef, secret);
      await refreshProfiles(result.profileId);
      return secret ? { ...result, hasCredential: true } : result;
    }, t("profileSaveFailed"));
    if (!saved) return;
    setProfile(saved);
    setDraft((current) => ({ ...current, nl: { ...current.nl, apiProfileId: saved.profileId } }));
    setDiagnosticResetToken((value) => value + 1);
    if (secret) setSecret("");
  };

  const rebuild = draft.overwriteMode === "rebuild";
  const annotationProfile = draft.profile as AnnotationProfile;
  const profileLabel = (value: AnnotationProfile) => value === "danbooru" ? copy.profileDanbooru : copy.profileE621;
  const profileDisplay = profileLabel(annotationProfile);
  const classifyEnabled = Boolean(draft.classify.enabled);
  const classificationResourceActive = classifyEnabled || Boolean(draft.caption.enabled);
  const enabledModules = draftModuleOrder
    .filter((id) => id === "export" || (id === "count_review" ? Boolean(draft.countReview.enabled)
      : id === "token_budget" ? draft.tokenBudget.enabled : Boolean(draft[id].enabled)))
    .map((id) => moduleLabel(language, id)).join(" / ");
  const workspaceReady = snapshot?.job.status === "preparing_workspace";
  // F28: the backend accepts the confirmation while paused or interrupted (scheduler.py:335).
  const pendingApiDecisions = snapshot?.nlPendingApiDecisions ?? 0;
  const rawE621Converted = snapshot?.captionDiagnostics.find((item) => item.code === "e621_raw_json_converted")?.count ?? 0;
  const nlAwaitsDecision = snapshot?.job.currentModuleId === "nl" && ["paused", "interrupted"].includes(snapshot.job.status) && pendingApiDecisions > 0;
  const retriableCount = snapshot?.repairPreview?.eligibleTargetCount ?? 0;
  const latestEvent = snapshot?.events.length ? snapshot.events[snapshot.events.length - 1] : null;
  const currentStep = steps[activeStep] ?? steps[steps.length - 1];
  const visibleStepIndex = steps.indexOf(currentStep);
  const currentModuleNumber = currentStep.id === "setup" ? null : moduleOrder.indexOf(currentStep.id) + 1;
  const workflowRailSteps = steps.map((step, index) => {
    const summary = step.id === "setup" ? null : snapshot?.modules.find((module) => module.module_id === step.id);
    const profileSkipped = step.id === "replace" && annotationProfile === "danbooru";
    const complete = step.id === "setup"
      ? Boolean(preflight)
      : profileSkipped || Boolean(summary && ["completed", "completed_with_issues", "skipped", "skipped_not_available"].includes(summary.status));
    const label = profileSkipped
      ? statusLabel(language, "skipped")
      : index === visibleStepIndex ? copy.current : complete ? copy.completed : index < visibleStepIndex ? copy.visited : copy.pending;
    return {
      id: step.id,
      title: step.title,
      label,
      moduleNumber: step.id === "setup" ? null : moduleOrder.indexOf(step.id) + 1,
      isSetup: step.id === "setup",
    };
  });
  const stepReady = visibleStepIndex !== 0 || Boolean(preflight);
  const taskLocked = Boolean(workspaceReady || (snapshot && (isActiveJobStatus(snapshot.job.status) || snapshot.job.status === "reviewing")));
  const orderedModules = snapshot?.moduleOrder.flatMap((id) => {
    const summary = snapshot.modules.find((module) => module.module_id === id);
    return summary ? [summary] : [];
  }) ?? [];
  const numberedModuleLabel = (moduleId: string) => {
    const index = snapshot?.moduleOrder.indexOf(moduleId as PipelineModuleId) ?? -1;
    const label = moduleLabel(language, moduleId);
    return index >= 0 ? `${t("moduleNumber", { number: index + 1 })} ${label}` : label;
  };
  const resourcesOf = (kind: ResourceKind) => resourceCatalog?.resources.filter((item) =>
    item.kind === kind && (kind === "dropout-model" || kind === "ocr-model" || kind === "tokenizer" || item.profile === annotationProfile),
  ) ?? [];
  const resourceFor = (kind: ResourceKind, resourceId: unknown) => resourceCatalog?.resources.find((item) =>
    item.kind === kind && item.resourceId === resourceId
    && (kind === "dropout-model" || kind === "ocr-model" || kind === "tokenizer" || item.profile === annotationProfile),
  );
  const ocrResource = resourceFor("ocr-model", draft.ocr.resourceId);
  const tokenizerResource = resourceFor("tokenizer", draft.tokenBudget.resourceId);
  const requiredResources = [
    resourceFor("tagging-model", draft.caption.resourceId),
    resourceFor("classification-index", draft.classify.resourceId),
    resourceFor("dropout-model", draft.dropout.quality.resourceId),
    ...(annotationProfile === "e621" && draft.replace.indexMode === "bundled"
      ? [resourceFor("replacement-index", draft.replace.resourceId)]
      : []),
    ...(draft.tokenBudget.enabled ? [tokenizerResource] : []),
  ];
  const resourceProblems = requiredResources.filter((item) => !(
    item?.available && !["incompatible", "unavailable"].includes(item.compatibility.status)
  ));
  const resourceProblemNames = resourceProblems.map((item) => item?.displayName[language] || item?.officialModelId || item?.resourceId || copy.resourceUnavailable);
  const profileResourcesReady = Boolean(resourceCatalog) && resourceProblems.length === 0;
  const resourcePickerCopy = {
    loading: copy.resourceLoading,
    unavailable: copy.resourceUnavailable,
    refresh: copy.refreshResources,
    retry: t("retryResources"),
    details: copy.resourceDetails,
    version: copy.resourceVersion,
    id: copy.resourceId,
    status: copy.resourceStatus,
    distribution: copy.resourceDistribution,
    ready: copy.resourceReady,
    notInstalled: copy.resourceNotInstalled,
    incompatible: copy.resourceIncompatible,
    localOnly: copy.resourceLocalOnly,
    bundled: copy.resourceBundled,
    source: copy.resourceSource,
    license: copy.resourceLicense,
    adjustable: copy.resourceAdjustable,
    excluded: copy.resourceExcluded,
    manualInstall: copy.resourceManualInstall,
    downloadUrl: copy.resourceDownloadUrl,
    installDirectory: copy.resourceInstallDirectory,
    requiredFiles: copy.resourceRequiredFiles,
    installHint: copy.resourceInstallHint,
    defaultSuffix: copy.defaultResource,
    invalid: copy.invalidResources,
  };
  const pathPickerCopy = {
    selectLabel: t("selectPath"),
    selectingLabel: t("selectingPath"),
    busyMessage: t("pathPickerBusy"),
    unavailableMessage: t("pathPickerUnavailable"),
    failedMessage: t("pathPickerFailed"),
  };
  const invalidResourceCount = resourceCatalog?.invalidResources.length ?? 0;
  const taskMonitorLabels = {
    taskOverview: copy.taskOverview,
    taskProgress: t("taskProgress"),
    annotationProfile: copy.annotationProfile,
    currentModule: t("currentModule"),
    currentBatch: t("currentBatch"),
    taskActions: copy.taskActions,
    pauseTask: t("pauseTask"),
    resumeTask: t("resumeTask"),
    terminateTask: t("terminateTask"),
    recoverTask: t("recoverTask"),
    pinTask: t("pinTask"),
    unpinTask: t("unpinTask"),
    discardTask: t("discardTask"),
    additionalAttempts: t("additionalAttempts"),
    addBudget: copy.addBudget,
    pendingApiDecisions: t("pendingApiDecisions"),
    confirmUnknown: t("confirmUnknown"),
    issues: t("issues"),
    noTask: copy.noTask,
    loadingTask: t("loadingTask"),
    retryTask: t("retryTask"),
    ocrRuntime: copy.ocrRuntime,
    ocrAvailability: copy.ocrAvailability,
    ocrGpu: copy.ocrGpu,
    ocrRequestedDevice: copy.ocrRequestedDevice,
    ocrObservedDevice: copy.ocrObservedDevice,
    ocrRecommended: copy.ocrRecommended,
    ocrEffective: copy.ocrEffective,
    ocrStartupReason: copy.ocrStartupReason,
  };
  const issuePanelLabels = {
    issues: t("issues"),
    shownRetriable: (shown: number, retriable: number) => t("shownRetriable", { shown, retriable }),
    attempt: (attempt: number) => t("attempt", { attempt }),
    retryFrom: (module: string) => t("retryFrom", { module }),
    notRetriable: t("notRetriable"),
    firstPage: t("firstPage"),
    nextPage: t("nextPage"),
    restoreOriginal: t("restoreOriginal"),
    reprocess: t("reprocess"),
    nlRetry: t("nlManualRetry"),
    nlWrite: t("nlManualWrite"),
    nlWritePlaceholder: t("nlManualWritePlaceholder"),
  };
  const taskMonitorModules = orderedModules.map((module) => ({
    moduleId: module.module_id,
    label: numberedModuleLabel(module.module_id),
    status: module.status,
    statusLabel: statusLabel(language, module.status),
    completed: module.completed,
    failed: module.failed,
    skipped: module.skipped,
    total: module.total,
    issueCount: module.issue_count,
    isCurrent: module.module_id === snapshot?.job.currentModuleId,
  }));

  const guide = <details className="module-guide" open={openGuide} onToggle={(event) => setOpenGuide((event.target as HTMLDetailsElement).open)}>
    <summary>{copy.details}</summary>
    <p>{currentStep.guide[0]}</p>
    <dl>
      <div><dt>{copy.input}</dt><dd>{currentStep.guide[1]}</dd></div>
      <div><dt>{copy.output}</dt><dd>{currentStep.guide[2]}</dd></div>
      <div><dt>{copy.effect}</dt><dd>{currentStep.guide[3]}</dd></div>
    </dl>
  </details>;

  const profileSelector = <section className="profile-selector top-profile-selector">
    <span>{copy.annotationProfile}</span>
    <div className="profile-switch" role="group" aria-label={copy.annotationProfile}>
      <button type="button" className={annotationProfile === "e621" ? "selected" : ""} disabled={taskLocked} onClick={() => selectAnnotationProfile("e621")}>{copy.profileE621}</button>
      <button type="button" className={annotationProfile === "danbooru" ? "selected" : ""} disabled={taskLocked} onClick={() => selectAnnotationProfile("danbooru")}>{copy.profileDanbooru}</button>
    </div>
    {!resourcesLoading && !profileResourcesReady && <small className="resource-warning" role="status">{copy.profileMissing(resourceProblemNames.join(" / "))}</small>}
  </section>;

  const countReviewContent = <>
    <div className="option-stack count-review-config">
      <ToggleField id="count-review-enabled" label={t("enableCountReview")} checked={draft.countReview.enabled} disabled={taskLocked} onChange={(enabled) => updateSection("countReview", { enabled })} copy={guidanceCopy} guidance={{ description: t("fieldHelp_countReviewEnabled"), defaultValue: draftDefaults.countReview.enabled ? t("fieldEnabled") : t("fieldDisabled") }} />
      <p className="hint">{t("countReviewConfigHelp")}</p>
    </div>
    {snapshot?.moduleOrder.includes("count_review")
      ? <CountReviewPanel jobId={jobId} jobStatus={snapshot.job.status} currentModuleId={snapshot.job.currentModuleId} language={language} onConfirmed={refreshSnapshot} />
      : <p className="review-empty">{t("countReviewUnavailableUntilTask")}</p>}
  </>;
  const tokenBudgetContent = <>
    <TokenBudgetStep
      draft={draft}
      defaults={draftDefaults}
      taskLocked={taskLocked}
      language={language}
      resources={resourcesOf("tokenizer")}
      resourcesLoading={resourcesLoading}
      resourceError={resourceError}
      invalidResourceCount={invalidResourceCount}
      resourcePickerCopy={resourcePickerCopy}
      t={t}
      guidanceCopy={guidanceCopy}
      onTokenBudgetChange={(patch) => updateSection("tokenBudget", patch)}
      onRefreshResources={() => void refreshResources()}
    />
    {snapshot?.moduleOrder.includes("token_budget") && <TokenBudgetReviewPanel
      jobId={jobId}
      jobStatus={snapshot.job.status}
      currentModuleId={snapshot.job.currentModuleId}
      language={language}
      onApplied={refreshSnapshot}
    />}
  </>;
  const stepContent = currentStep.id === "setup" ? <SetupStep
    draft={draft}
    defaults={draftDefaults}
    taskLocked={taskLocked}
    resourcesLoading={resourcesLoading}
    profileResourcesReady={profileResourcesReady}
    workspaceReady={workspaceReady}
    preflightPending={isActionPending("preflight")}
    workspacePending={isActionPending("confirm_workspace")}
    preflight={preflight}
    enabledModules={enabledModules}
    language={language}
    t={t}
    guidanceCopy={guidanceCopy}
    pathPickerCopy={pathPickerCopy}
    copy={{
      preflightHint: copy.preflightHint,
      profileReady: copy.profileReady,
      profileUnavailable: copy.profileUnavailable,
      workspaceHint: copy.workspaceHint,
    }}
    onSourceRootChange={(value) => { setDraft((current) => replaceDraftValue(current, "sourceRoot", value)); invalidatePreflight(); }}
    onWorkModeChange={(workMode) => { setDraft((current) => ({ ...current, workMode, outputRoot: workMode === "in_place" ? undefined : current.outputRoot })); invalidatePreflight(); }}
    onOutputRootChange={(value) => { setDraft((current) => replaceDraftValue(current, "outputRoot", value)); invalidatePreflight(); }}
    onOverwriteModeChange={(overwriteMode) => { setDraft((current) => replaceDraftValue(current, "overwriteMode", overwriteMode)); invalidatePreflight(); }}
    onInvalidImageActionChange={(invalidImageAction) => updateSection("imageDecode", { invalidImageAction })}
    onRecursiveChange={(recursive) => { setDraft((current) => replaceDraftValue(current, "recursive", recursive)); invalidatePreflight(); }}
    onRunPreflight={() => void runPreflight()}
    onPrepareWorkspace={() => void prepareWorkspace()}
  /> : currentStep.id === "caption" ? <CaptionStep
    draft={draft}
    defaults={draftDefaults}
    taskLocked={taskLocked}
    rebuild={rebuild}
    language={language}
    annotationProfile={annotationProfile}
    resources={resourcesOf("tagging-model")}
    resourcesLoading={resourcesLoading}
    resourceError={resourceError}
    invalidResourceCount={invalidResourceCount}
    resourcePickerCopy={resourcePickerCopy}
    selectedTagger={selectedTagger}
    triggerInput={triggerInput}
    t={t}
    guidanceCopy={guidanceCopy}
    copy={{ captionModel: copy.captionModel, captionModelHelp: copy.captionModelHelp }}
    onCaptionChange={(patch) => updateSection("caption", patch)}
    onInputTxtModeChange={updateInputTxtMode}
    onCaptionFormatChange={(patch) => updateSection("captionFormat", patch)}
    onSelectTagger={selectTagger}
    onRefreshResources={() => void refreshResources()}
    onThresholdModeChange={updateThresholdMode}
    onCategoryThresholdChange={updateCategoryThreshold}
    onTriggerInputChange={updateTriggerTerms}
  /> : currentStep.id === "classify" ? <ClassifyStep
    draft={draft}
    taskLocked={taskLocked}
    rebuild={rebuild}
    classifyEnabled={classifyEnabled}
    classificationResourceActive={classificationResourceActive}
    language={language}
    annotationProfile={annotationProfile}
    resources={resourcesOf("classification-index")}
    resourcesLoading={resourcesLoading}
    resourceError={resourceError}
    invalidResourceCount={invalidResourceCount}
    resourcePickerCopy={resourcePickerCopy}
    t={t}
    guidanceCopy={guidanceCopy}
    copy={{ classificationIndex: copy.classificationIndex, classificationIndexHelp: copy.classificationIndexHelp, anthroReplacementNote: copy.anthroReplacementNote }}
    onClassifyChange={(patch) => updateSection("classify", patch)}
    onRefreshResources={() => void refreshResources()}
  /> : currentStep.id === "replace" ? <ReplaceStep
    draft={draft}
    annotationProfile={annotationProfile}
    taskLocked={taskLocked}
    language={language}
    resources={resourcesOf("replacement-index")}
    resourcesLoading={resourcesLoading}
    resourceError={resourceError}
    invalidResourceCount={invalidResourceCount}
    resourcePickerCopy={resourcePickerCopy}
    pathPickerCopy={pathPickerCopy}
    replaceIndex={preflight?.replaceIndex ?? null}
    t={t}
    guidanceCopy={guidanceCopy}
    copy={{
      replaceSkipped: copy.replaceSkipped,
      replaceMode: copy.replaceMode,
      bundledIndex: copy.bundledIndex,
      customIndex: copy.customIndex,
      customIndexPath: copy.customIndexPath,
      customIndexHelp: copy.customIndexHelp,
      replacementIndex: copy.replacementIndex,
      replacementIndexHelp: copy.replacementIndexHelp,
      indexRules: copy.indexRules,
    }}
    onReplaceChange={(patch) => updateSection("replace", patch)}
    onIndexModeChange={(indexMode) => {
      setPreflight(null);
      setDraft((current) => replaceDraftValue(current, "replace", indexMode === "bundled"
        ? { enabled: current.replace.enabled, indexMode, resourceId: resourceCatalog?.defaults.e621.replacementIndex ?? "replace-e621-20260726-v2" }
        : { enabled: current.replace.enabled, indexMode, customIndexPath: current.replace.customIndexPath ?? "" }));
    }}
    onRefreshResources={() => void refreshResources()}
  /> : currentStep.id === "ocr" ? <OcrStep
    draft={draft}
    defaults={draftDefaults}
    taskLocked={taskLocked || Boolean(preflight)}
    ocrExecution={ocrExecution}
    runtime={snapshot?.ocrRuntime ?? null}
    resource={ocrResource}
    resourcesLoading={resourcesLoading}
    resourceError={resourceError}
    diagnostics={snapshot?.ocrDiagnostics ?? []}
    guidanceCopy={guidanceCopy}
    t={t}
    copy={copy}
    onOcrChange={(patch) => updateSection("ocr", patch)}
    onOcrExecutionChange={(next) => { setPreflight(null); setOcrExecution(next); }}
    onRefreshResources={() => void refreshResources()}
  /> : currentStep.id === "nl" ? <NlStep
    draft={draft}
    defaults={draftDefaults}
    taskLocked={taskLocked}
    attemptBudget={attemptBudget}
    profile={profile}
    profiles={profiles}
    secret={secret}
    profileSavePending={isActionPending("profile_save")}
    credentialDeletePending={isActionPending("credential_delete")}
    diagnosticResetToken={diagnosticResetToken}
    t={t}
    guidanceCopy={guidanceCopy}
    profileHelp={copy.profileHelp}
    onNlChange={(patch) => updateSection("nl", patch)}
    onApiPolicyChange={updateApiPolicy}
    onConcurrencyChange={(value) => updateApiPolicy({ concurrency: clamp(value, 1, 16, 3) })}
    onRequestsPerMinuteChange={(value) => updateApiPolicy({ maxRequestsPerMinute: clamp(value, 1, 100_000, 60) })}
    onUnlimitedRpmChange={(enabled) => {
      if (!enabled) { updateApiPolicy({ maxRequestsPerMinute: 60 }); return; }
      if (window.confirm(t("confirmUnlimitedRpm"))) updateApiPolicy({ maxRequestsPerMinute: "unlimited" });
    }}
    onAttemptBudgetChange={updateAttemptBudget}
    onProfileSelect={(profileId) => {
      const selected = profiles.find((item) => item.profileId === profileId) ?? { ...emptyProfile, profileId };
      setProfile(selected);
      setSecret("");
      setDiagnosticResetToken((value) => value + 1);
      updateSection("nl", { apiProfileId: selected.profileId });
    }}
    onProfileChange={(nextProfile) => setProfile((current) => ({ ...current, ...nextProfile }))}
    onSecretChange={setSecret}
    onSaveProfile={() => void submitProfile()}
    onClearSecret={() => void control("credential_delete", async () => {
      const reference = effectiveCredentialReference(profile);
      await deleteNlSecret(reference);
      await refreshProfiles(profile.profileId);
      setProfile((current) => ({ ...current, apiCredentialRef: reference, hasCredential: false }));
      setSecret("");
      setDiagnosticResetToken((value) => value + 1);
    })}
  /> : currentStep.id === "count_review" ? countReviewContent : currentStep.id === "dropout" ? <PolicyStep
    draft={draft}
    defaults={draftDefaults}
    taskLocked={taskLocked}
    annotationProfile={annotationProfile}
    language={language}
    resources={resourcesOf("dropout-model")}
    resourcesLoading={resourcesLoading}
    resourceError={resourceError}
    invalidResourceCount={invalidResourceCount}
    resourcePickerCopy={resourcePickerCopy}
    t={t}
    guidanceCopy={guidanceCopy}
    copy={{ qualityModel: copy.qualityModel, qualityModelHelp: copy.qualityModelHelp }}
    onDropoutChange={(patch) => updateSection("dropout", patch)}
    onArtistChange={(patch) => updateDropoutNested("artist", patch)}
    onQualityChange={(patch) => updateDropoutNested("quality", patch)}
    onAppearanceNlChange={(patch) => updateDropoutNested("appearanceNl", patch)}
    onAppearanceProbabilityChange={updateAppearanceProbability}
    onRefreshResources={() => void refreshResources()}
  /> : currentStep.id === "token_budget" ? tokenBudgetContent : <ExportStep
    draft={draft}
    defaults={draftDefaults}
    taskLocked={taskLocked}
    workspaceReady={workspaceReady}
    startPending={isActionPending("start")}
    exportSummary={snapshot?.exportSummary ?? null}
    t={t}
    automatic={copy.automatic}
    guidanceCopy={guidanceCopy}
    onExportChange={(patch) => updateSection("export", patch)}
    onStartPipeline={() => void control("start", () => startPipeline(jobId))}
  />;

  return <main className="app-shell">
    <header className="topbar"><div><p className="eyebrow">ANIMA</p><h1>Anima Dataset Tool</h1><p>{t("pipelineSubtitle", { profile: profileDisplay })}</p></div><div className="topbar-controls"><label>{t("taskId")}<input value={jobId} onChange={(event) => selectJob(event.target.value.trim())} /></label><div className="recent-task-control"><label>{t("recentTasks")}<select disabled={jobsLoading} value={jobs.some((item) => item.jobId === jobId) ? jobId : ""} onChange={(event) => { if (event.target.value === "__more__") { void loadMoreJobs(); return; } if (event.target.value) selectJob(event.target.value); }}><option value="">{jobs.length ? "-" : t("noRecentTasks")}</option>{jobs.map((item) => <option key={item.jobId} value={item.jobId}>{item.createdAt} {profileLabel(item.profile)} {statusLabel(language, item.status)} {item.jobId.slice(0, 8)}</option>)}{jobsCursor && <option value="__more__">{t("loadMore")}</option>}</select></label><div className="recent-task-state" aria-live="polite" aria-atomic="true">{jobsLoading && <small role="status">{t("loadingTasks")}</small>}{jobsError && <><small role="alert">{jobsError}</small><button className="secondary" type="button" onClick={() => void refreshJobs()}>{t("retryTasks")}</button></>}</div></div><div className="language-switch" role="group" aria-label={t("language")}><button type="button" className={language === "zh-CN" ? "selected" : ""} onClick={() => setLanguage("zh-CN")}>中文</button><button type="button" className={language === "en" ? "selected" : ""} onClick={() => setLanguage("en")}>EN</button></div></div></header>
    {profileSelector}
    <div className="workflow-layout">
      <WorkflowRail flow={copy.flow} visibleStepIndex={visibleStepIndex} steps={workflowRailSteps} onSelect={(index) => { setActiveStep(index); setOpenGuide(true); }} />
      <section className="step-panel"><div className="step-heading"><div><p className="eyebrow">{copy.flow} / {currentModuleNumber === null ? copy.setup : t("moduleNumber", { number: currentModuleNumber })}</p><h2>{currentStep.title}</h2></div><span className={`status ${currentStep.id === "replace" ? annotationProfile === "danbooru" ? "skipped" : "available" : ""}`}>{currentStep.id === "replace" ? annotationProfile === "danbooru" ? statusLabel(language, "skipped") : t("e621Only") : currentStep.id === "setup" ? t("newTask") : copy.current}</span></div>{actionError && <div className="action-feedback" aria-live="assertive"><p role="alert">{actionError}</p></div>}{guide}<div className="step-content">{stepContent}</div><div className="wizard-controls"><button className="secondary" type="button" disabled={visibleStepIndex === 0} onClick={() => { setActiveStep(visibleStepIndex - 1); setOpenGuide(true); }}>{copy.back}</button><button type="button" disabled={visibleStepIndex === steps.length - 1 || !stepReady} onClick={() => { setActiveStep(visibleStepIndex + 1); setOpenGuide(true); }}>{copy.next}</button></div></section>
      <TaskMonitor
        snapshot={snapshot ? {
          status: snapshot.job.status,
          profile: snapshot.job.profile,
          currentModuleId: snapshot.job.currentModuleId ?? null,
          pinned: snapshot.job.pinned,
          ocrRuntime: snapshot.ocrRuntime,
        } : null}
        loading={snapshotLoading}
        error={snapshotError}
        statusLabel={statusLabel(language, snapshot?.job.status ?? "idle")}
        profileLabel={snapshot ? profileLabel(snapshot.job.profile) : ""}
        currentModuleLabel={snapshot?.job.currentModuleId ? numberedModuleLabel(snapshot.job.currentModuleId) : "-"}
        currentBatchLabel={latestEvent ? `${latestEvent.completed} / ${latestEvent.total}` : "-"}
        rawE621ConvertedMessage={rawE621Converted > 0 ? t("rawE621Converted", { count: rawE621Converted }) : null}
        modules={taskMonitorModules}
        labels={taskMonitorLabels}
        canDiscard={Boolean(snapshot && ["ready", "interrupted", "reviewing", "cancelled_recoverable", "failed"].includes(snapshot.job.status))}
        budget={budget}
        pendingApiDecisions={pendingApiDecisions}
        nlAwaitsDecision={nlAwaitsDecision}
        pendingActions={pendingActions}
        onPause={() => void lifecycleControl("pause", jobId, () => pauseJob(jobId))}
        onResume={() => void lifecycleControl("resume", jobId, () => resumeJob(jobId))}
        onTerminate={() => { if (window.confirm(t("confirmTerminate"))) void lifecycleControl("terminate", jobId, () => cancelJob(jobId)); }}
        onRecover={() => { if (window.confirm(t("confirmRecover"))) void lifecycleControl("recover", jobId, () => recoverJob(jobId)); }}
        onPin={() => { if (snapshot) void control("pin", () => setJobPin(jobId, !snapshot.job.pinned)); }}
        onDiscard={() => { if (window.confirm(t("confirmDiscard"))) void control("discard", () => discardJob(jobId)); }}
        onBudgetChange={setBudget}
        onAddBudget={() => void control("nl_budget", () => addNlBudget(jobId, Number(budget)))}
        onConfirmUnknown={() => { if (window.confirm(t("confirmUnknownPrompt", { count: pendingApiDecisions }))) void control("nl_confirm_outcomes", () => confirmNlOutcomes(jobId)); }}
        onRetry={() => void refreshSnapshot()}
      />
    </div>
    {snapshot && <IssuePanel
      issues={snapshot.issues.map((item) => ({
        issueId: item.issue_id,
        sampleId: item.sample_id,
        moduleId: item.module_id,
        code: item.code,
        message: item.message,
        retriable: Boolean(item.retriable),
        attempt: item.attempt,
        repairStartModule: item.repair_start_module ?? null,
      }))}
      retriableCount={retriableCount}
      cursor={issueCursor}
      nextCursor={{ sampleId: snapshot.nextIssueAfterSampleId, issueId: snapshot.nextIssueAfterIssueId }}
      labels={issuePanelLabels}
       canRestore={snapshot.job.status === "succeeded"}
      canReprocess={retriableCount > 0 && ["reviewing", "failed"].includes(snapshot.job.status)}
      canManualNl={Boolean(snapshot.job.status === "reviewing" || snapshot.job.status === "failed")}
      pendingActions={pendingActions}
      onFirstPage={firstIssuePage}
      onNextPage={nextIssuePage}
       onRestore={() => { if (window.confirm(t("confirmRestore"))) void control("restore", () => restoreOriginalAnnotations(jobId)); }}
      onReprocess={() => void startRepair()}
      onManualNlRetry={startManualNlRetry}
      onManualNlWrite={startManualNlWrite}
    />}
  </main>;
}
