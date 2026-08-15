import { useState } from "react";

export type IssuePanelIssue = {
  issueId: string;
  sampleId: number;
  moduleId: string;
  code: string;
  message: string;
  retriable: boolean;
  attempt: number;
  repairStartModule: string | null;
};

export type IssuePanelProps = {
  issues: IssuePanelIssue[];
  retriableCount: number;
  cursor: { sampleId: number; issueId: string | null };
  nextCursor: { sampleId: number; issueId: string | null };
  labels: {
    issues: string;
    shownRetriable: (shown: number, retriable: number) => string;
    attempt: (attempt: number) => string;
    retryFrom: (module: string) => string;
    notRetriable: string;
    firstPage: string;
    nextPage: string;
    restoreOriginal: string;
    reprocess: string;
    nlRetry: string;
    nlWrite: string;
    nlWritePlaceholder: string;
  };
  canRestore: boolean;
  canReprocess: boolean;
  pendingActions: ReadonlySet<string>;
  onFirstPage: () => void;
  onNextPage: () => void;
  onRestore: () => void;
  onReprocess: () => void;
  canManualNl: boolean;
  onManualNlRetry: (issue: IssuePanelIssue) => void;
  onManualNlWrite: (issue: IssuePanelIssue, nl: string) => void;
};

export function IssuePanel({
  issues, retriableCount, cursor, nextCursor, labels, canRestore, canReprocess, pendingActions, onFirstPage, onNextPage, onRestore, onReprocess,
  canManualNl, onManualNlRetry, onManualNlWrite,
}: IssuePanelProps) {
  const atFirstPage = cursor.sampleId === 0 && cursor.issueId === null;
  const atLastPage = issues.length < 1 || (nextCursor.sampleId === cursor.sampleId && nextCursor.issueId === cursor.issueId);
  const restoring = pendingActions.has("restore");
  const repairing = pendingActions.has("repair");
  const manualNlRetrying = pendingActions.has("nl_manual_retry");
  const manualNlWriting = pendingActions.has("nl_manual_write");
  const [nlDrafts, setNlDrafts] = useState<Record<string, string>>({});

  return <section className="issues-panel" aria-busy={restoring || repairing}>
    <div className="panel-heading">
      <div><p className="eyebrow">{labels.issues}</p><h2>{labels.issues}</h2></div>
      <span>{labels.shownRetriable(issues.length, retriableCount)}</span>
    </div>
    <ul className="issue-list">{issues.map((item) => {
      const isNlManual = canManualNl && item.moduleId === "nl" && !item.retriable;
      const draft = nlDrafts[item.issueId] ?? "";
      return <li key={item.issueId}>
        <div><code>{item.moduleId}:{item.code}</code><span>{item.message}</span></div>
        <small>{labels.attempt(item.attempt)}; {item.retriable ? labels.retryFrom(item.repairStartModule ?? "-") : labels.notRetriable}</small>
        {isNlManual && <div className="issue-manual-nl">
          <button className="secondary" type="button" disabled={manualNlRetrying || manualNlWriting} onClick={() => onManualNlRetry(item)}>{labels.nlRetry}</button>
          <input value={draft} placeholder={labels.nlWritePlaceholder} disabled={manualNlRetrying || manualNlWriting} onChange={(event) => setNlDrafts((current) => ({ ...current, [item.issueId]: event.target.value }))} />
          <button className="secondary" type="button" disabled={!draft.trim() || manualNlRetrying || manualNlWriting} onClick={() => onManualNlWrite(item, draft)}>{labels.nlWrite}</button>
        </div>}
      </li>;
    })}</ul>
    <div className="step-actions">
      <button className="secondary" type="button" disabled={atFirstPage} onClick={onFirstPage}>{labels.firstPage}</button>
      <button className="secondary" type="button" disabled={atLastPage} onClick={onNextPage}>{labels.nextPage}</button>
      <button className="warning-action" type="button" disabled={!canRestore || restoring} aria-busy={restoring} onClick={onRestore}>{labels.restoreOriginal}</button>
      <button className="secondary" type="button" disabled={!canReprocess || repairing} aria-busy={repairing} onClick={onReprocess}>{labels.reprocess}</button>
    </div>
  </section>;
}
