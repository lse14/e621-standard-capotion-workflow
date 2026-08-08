export type IssuePanelIssue = {
  issueId: string;
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
  };
  canRestore: boolean;
  canReprocess: boolean;
  pendingActions: ReadonlySet<string>;
  onFirstPage: () => void;
  onNextPage: () => void;
  onRestore: () => void;
  onReprocess: () => void;
};

export function IssuePanel({
  issues, retriableCount, cursor, nextCursor, labels, canRestore, canReprocess, pendingActions, onFirstPage, onNextPage, onRestore, onReprocess,
}: IssuePanelProps) {
  const atFirstPage = cursor.sampleId === 0 && cursor.issueId === null;
  const atLastPage = issues.length < 1 || (nextCursor.sampleId === cursor.sampleId && nextCursor.issueId === cursor.issueId);
  const restoring = pendingActions.has("restore");
  const repairing = pendingActions.has("repair");

  return <section className="issues-panel" aria-busy={restoring || repairing}>
    <div className="panel-heading">
      <div><p className="eyebrow">{labels.issues}</p><h2>{labels.issues}</h2></div>
      <span>{labels.shownRetriable(issues.length, retriableCount)}</span>
    </div>
    <ul className="issue-list">{issues.map((item) => <li key={item.issueId}>
      <div><code>{item.moduleId}:{item.code}</code><span>{item.message}</span></div>
      <small>{labels.attempt(item.attempt)}; {item.retriable ? labels.retryFrom(item.repairStartModule ?? "-") : labels.notRetriable}</small>
    </li>)}</ul>
    <div className="step-actions">
      <button className="secondary" type="button" disabled={atFirstPage} onClick={onFirstPage}>{labels.firstPage}</button>
      <button className="secondary" type="button" disabled={atLastPage} onClick={onNextPage}>{labels.nextPage}</button>
      <button className="warning-action" type="button" disabled={!canRestore || restoring} aria-busy={restoring} onClick={onRestore}>{labels.restoreOriginal}</button>
      <button className="secondary" type="button" disabled={!canReprocess || repairing} aria-busy={repairing} onClick={onReprocess}>{labels.reprocess}</button>
    </div>
  </section>;
}
