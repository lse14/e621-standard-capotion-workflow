export type TaskMonitorSnapshot = {
  status: string;
  profile: string;
  currentModuleId: string | null;
  pinned: boolean;
  ocrRuntime: {
    availability: "pending" | "available" | "unavailable";
    runtimeId: "ocr-paddle" | "ocr-paddle-gpu" | null;
    gpuName: string | null;
    totalVramBytes: number | null;
    requestedDevice: "auto" | "cuda" | "cpu" | null;
    observedDevice: "cpu" | "cuda" | null;
    recommended: { textDetLimitSideLen: number; textBatchSize: number } | null;
    effective: { textDetLimitSideLen: number; textBatchSize: number } | null;
    startupReason: "gpu_runtime_unavailable" | "binding_invalid" | null;
  } | null;
};

export type TaskMonitorModule = {
  moduleId: string;
  label: string;
  status: string;
  statusLabel: string;
  completed: number;
  failed: number;
  skipped: number;
  total: number;
  issueCount: number;
  isCurrent: boolean;
};

export type TaskMonitorProps = {
  snapshot: TaskMonitorSnapshot | null;
  loading: boolean;
  error: string | null;
  statusLabel: string;
  profileLabel: string;
  currentModuleLabel: string;
  currentBatchLabel: string;
  rawE621ConvertedMessage: string | null;
  modules: TaskMonitorModule[];
  labels: {
    taskOverview: string; taskProgress: string; annotationProfile: string; currentModule: string; currentBatch: string;
    taskActions: string; pauseNl: string; resumeNl: string; pausePolicy: string; resumePolicy: string;
    cancelTask: string; recoverTask: string; pinTask: string; unpinTask: string; discardTask: string;
    additionalAttempts: string; addBudget: string; pendingApiDecisions: string; confirmUnknown: string;
    issues: string; noTask: string; loadingTask: string; retryTask: string;
    ocrRuntime: string; ocrAvailability: string; ocrGpu: string; ocrRequestedDevice: string; ocrObservedDevice: string;
    ocrRecommended: string; ocrEffective: string; ocrStartupReason: string;
  };
  nlRunning: boolean;
  nlPaused: boolean;
  policyRunning: boolean;
  policyPaused: boolean;
  canCancel: boolean;
  canRecover: boolean;
  canDiscard: boolean;
  budget: string;
  pendingApiDecisions: number;
  nlAwaitsDecision: boolean;
  pendingActions: ReadonlySet<string>;
  onPauseNl: () => void;
  onResumeNl: () => void;
  onPausePolicy: () => void;
  onResumePolicy: () => void;
  onCancel: () => void;
  onRecover: () => void;
  onPin: () => void;
  onDiscard: () => void;
  onBudgetChange: (value: string) => void;
  onAddBudget: () => void;
  onConfirmUnknown: () => void;
  onRetry: () => void;
};

function formatGiB(bytes: number): string {
  return `${(bytes / (1024 ** 3)).toFixed(bytes % (1024 ** 3) ? 1 : 0)} GiB`;
}

export function TaskMonitor({
  snapshot, loading, error, statusLabel, profileLabel, currentModuleLabel, currentBatchLabel, rawE621ConvertedMessage, modules, labels,
  nlRunning, nlPaused, policyRunning, policyPaused, canCancel, canRecover, canDiscard, budget, pendingApiDecisions,
  nlAwaitsDecision, pendingActions, onPauseNl, onResumeNl, onPausePolicy, onResumePolicy, onCancel, onRecover, onPin, onDiscard,
  onBudgetChange, onAddBudget, onConfirmUnknown, onRetry,
}: TaskMonitorProps) {
  const isPending = (action: string) => pendingActions.has(action);

  return <aside className="task-monitor" aria-busy={loading || pendingActions.size > 0}>
    <div className="monitor-heading">
      <div><p className="eyebrow">{labels.taskOverview}</p><h2>{labels.taskProgress}</h2></div>
      <span className={`status ${snapshot?.status ?? "idle"}`} aria-live="polite" aria-atomic="true">{statusLabel}</span>
    </div>
    {loading && <p className="monitor-state" role="status">{labels.loadingTask}</p>}
    {error && <div className="monitor-error"><p role="alert">{error}</p><button className="secondary" type="button" onClick={onRetry}>{labels.retryTask}</button></div>}
    {snapshot ? <>
      <dl className="facts">
        <div><dt>{labels.annotationProfile}</dt><dd>{profileLabel}</dd></div>
        <div><dt>{labels.currentModule}</dt><dd>{currentModuleLabel}</dd></div>
        <div><dt>{labels.currentBatch}</dt><dd>{currentBatchLabel}</dd></div>
      </dl>
      {snapshot.ocrRuntime ? <section className="ocr-runtime" aria-label={labels.ocrRuntime}>
        <h3>{labels.ocrRuntime}</h3>
        <dl className="facts">
          <div><dt>{labels.ocrAvailability}</dt><dd>{snapshot.ocrRuntime.availability}{snapshot.ocrRuntime.runtimeId ? ` / ${snapshot.ocrRuntime.runtimeId}` : ""}</dd></div>
          {snapshot.ocrRuntime.gpuName ? <div><dt>{labels.ocrGpu}</dt><dd>{snapshot.ocrRuntime.gpuName}{snapshot.ocrRuntime.totalVramBytes === null ? "" : ` ${formatGiB(snapshot.ocrRuntime.totalVramBytes)}`}</dd></div> : null}
          <div><dt>{labels.ocrRequestedDevice}</dt><dd>{snapshot.ocrRuntime.requestedDevice ?? "-"}</dd></div>
          <div><dt>{labels.ocrObservedDevice}</dt><dd>{snapshot.ocrRuntime.observedDevice ?? "-"}</dd></div>
          {snapshot.ocrRuntime.recommended ? <div><dt>{labels.ocrRecommended}</dt><dd>{snapshot.ocrRuntime.recommended.textDetLimitSideLen} / {snapshot.ocrRuntime.recommended.textBatchSize}</dd></div> : null}
          {snapshot.ocrRuntime.effective ? <div><dt>{labels.ocrEffective}</dt><dd>{snapshot.ocrRuntime.effective.textDetLimitSideLen} / {snapshot.ocrRuntime.effective.textBatchSize}</dd></div> : null}
          {snapshot.ocrRuntime.startupReason ? <div><dt>{labels.ocrStartupReason}</dt><dd>{snapshot.ocrRuntime.startupReason}</dd></div> : null}
        </dl>
      </section> : null}
      {rawE621ConvertedMessage ? <p className="hint">{rawE621ConvertedMessage}</p> : null}
      <div className="module-progress">{modules.map((module) => {
        const settled = module.completed + module.failed + module.skipped;
        const percentage = module.total ? Math.min(100, Math.round(settled / module.total * 100)) : 0;
        return <div className={`module-row ${module.isCurrent ? "current" : ""}`} key={module.moduleId}>
          <div><strong>{module.label}</strong><span className={`status ${module.status}`}>{module.statusLabel}</span></div>
          <progress value={settled} max={Math.max(module.total, 1)} />
          <small>{settled} / {module.total} ({percentage}%) {module.issueCount ? `- ${module.issueCount} ${labels.issues}` : ""}</small>
        </div>;
      })}</div>
      <section className="task-actions">
        <h3>{labels.taskActions}</h3>
        <div className="action-grid">
          <button type="button" disabled={!nlRunning || isPending("nl_pause")} aria-busy={isPending("nl_pause")} onClick={onPauseNl}>{labels.pauseNl}</button>
          <button type="button" disabled={!nlPaused || isPending("nl_resume")} aria-busy={isPending("nl_resume")} onClick={onResumeNl}>{labels.resumeNl}</button>
          <button type="button" disabled={!policyRunning || isPending("policy_pause")} aria-busy={isPending("policy_pause")} onClick={onPausePolicy}>{labels.pausePolicy}</button>
          <button type="button" disabled={!policyPaused || isPending("policy_resume")} aria-busy={isPending("policy_resume")} onClick={onResumePolicy}>{labels.resumePolicy}</button>
        </div>
        <div className="action-grid">
          <button className="secondary" type="button" disabled={!canCancel || isPending("cancel")} aria-busy={isPending("cancel")} onClick={onCancel}>{labels.cancelTask}</button>
          <button className="secondary" type="button" disabled={!canRecover || isPending("recover")} aria-busy={isPending("recover")} onClick={onRecover}>{labels.recoverTask}</button>
          <button className="secondary" type="button" disabled={isPending("pin")} aria-busy={isPending("pin")} onClick={onPin}>{snapshot.pinned ? labels.unpinTask : labels.pinTask}</button>
          <button className="danger-action" type="button" disabled={!canDiscard || isPending("discard")} aria-busy={isPending("discard")} onClick={onDiscard}>{labels.discardTask}</button>
        </div>
      </section>
      <section className="task-actions">
        <div className="inline-control">
          <input inputMode="numeric" value={budget} onChange={(event) => onBudgetChange(event.target.value)} aria-label={labels.additionalAttempts} />
          <button type="button" disabled={!/^\d+$/.test(budget) || Number(budget) < 1 || isPending("nl_budget")} aria-busy={isPending("nl_budget")} onClick={onAddBudget}>{labels.addBudget}</button>
        </div>
        <div className="inline-control">
          <span>{labels.pendingApiDecisions}: {pendingApiDecisions}</span>
          <button className="warning-action" type="button" disabled={!nlAwaitsDecision || isPending("nl_confirm_outcomes")} aria-busy={isPending("nl_confirm_outcomes")} onClick={onConfirmUnknown}>{labels.confirmUnknown}</button>
        </div>
      </section>
    </> : !loading && !error && <p className="empty-state">{labels.noTask}</p>}
  </aside>;
}
