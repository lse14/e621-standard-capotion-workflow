import { useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  applyTokenBudgetProposal,
  listTokenBudgetReviews,
  recountTokenBudget,
  rewriteTokenBudgetShort,
  type TokenBudgetAnnotation,
  type TokenBudgetProposal,
  type TokenBudgetReviewItem,
  type TokenBudgetReviewPage,
} from "./api";
import { translate, type UiLanguage } from "./i18n";

const PAGE_SIZE = 50;
const EDITABLE_ARRAYS = ["quality", "environment", "tags", "appearance"] as const;

type EditField = "nl" | (typeof EDITABLE_ARRAYS)[number];
type RecountRequest = { sampleId: number; expectedVersion: number; annotation: TokenBudgetAnnotation };

export type TokenBudgetReviewPanelProps = {
  jobId: string;
  jobStatus: string;
  currentModuleId?: string;
  language: UiLanguage;
  onApplied: () => Promise<void>;
};

function annotationText(annotation: TokenBudgetAnnotation, field: EditField): string {
  return field === "nl" ? annotation.nl : annotation[field].join(", ");
}

function updateAnnotation(annotation: TokenBudgetAnnotation, field: EditField, value: string): TokenBudgetAnnotation {
  if (field === "nl") return { ...annotation, nl: value };
  return { ...annotation, [field]: value.split(",").map((item) => item.trim()).filter(Boolean) };
}

function proposalFor(item: TokenBudgetReviewItem, proposal: TokenBudgetProposal): TokenBudgetReviewItem {
  return {
    ...item,
    review: { ...item.review, version: proposal.version },
    proposal,
  };
}

export function TokenBudgetReviewPanel({ jobId, jobStatus, currentModuleId, language, onApplied }: TokenBudgetReviewPanelProps) {
  const t = (key: string, values?: Record<string, string | number>) => translate(language, key, values);
  const editable = jobStatus === "reviewing" && currentModuleId === "token_budget";
  const [page, setPage] = useState<TokenBudgetReviewPage | null>(null);
  const [pageCursors, setPageCursors] = useState<Array<number | null>>([null]);
  const [pageIndex, setPageIndex] = useState(0);
  const [edits, setEdits] = useState<Record<number, TokenBudgetAnnotation>>({});
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [recountRequest, setRecountRequest] = useState<RecountRequest | null>(null);
  const [loading, setLoading] = useState(false);
  const [recounting, setRecounting] = useState<Set<number>>(() => new Set());
  const [applying, setApplying] = useState<Set<number>>(() => new Set());
  const [rewriting, setRewriting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [reloadRevision, setReloadRevision] = useState(0);
  const requestSequence = useRef(0);
  const afterSampleId = pageCursors[pageIndex] ?? null;

  const refreshCurrentPage = () => setReloadRevision((value) => value + 1);
  const handleConflict = () => {
    setNotice(t("tokenBudgetConflict"));
    refreshCurrentPage();
  };

  useEffect(() => {
    setPageCursors([null]);
    setPageIndex(0);
    setEdits({});
    setSelected(new Set());
    setRecountRequest(null);
  }, [jobId]);

  useEffect(() => {
    if (!editable) {
      setPage(null);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    let active = true;
    setLoading(true);
    setError(null);
    setSelected(new Set());
    void listTokenBudgetReviews(jobId, afterSampleId, PAGE_SIZE, controller.signal)
      .then((result) => { if (active) { setPage(result); setEdits({}); } })
      .catch((cause) => {
        if (active && cause instanceof Error && cause.name !== "AbortError") {
          setPage(null);
          setError(cause instanceof Error ? cause.message : t("tokenBudgetReviewLoadFailed"));
        }
      })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; controller.abort(); };
  }, [afterSampleId, editable, jobId, language, pageIndex, reloadRevision]);

  useEffect(() => {
    if (!recountRequest || !editable) return;
    const controller = new AbortController();
    const sequence = ++requestSequence.current;
    const target = recountRequest;
    const timer = window.setTimeout(() => {
      setRecounting((current) => new Set(current).add(target.sampleId));
      void recountTokenBudget(jobId, target, controller.signal)
        .then((proposal) => {
          if (controller.signal.aborted || sequence !== requestSequence.current) return;
          setPage((current) => current ? { ...current, items: current.items.map((item) => item.sampleId === target.sampleId ? proposalFor(item, proposal) : item) } : current);
          setEdits((current) => ({ ...current, [target.sampleId]: proposal.annotation }));
        })
        .catch((cause) => {
          if (controller.signal.aborted || sequence !== requestSequence.current) return;
          if (cause instanceof ApiError && cause.status === 409) { handleConflict(); return; }
          setError(cause instanceof Error ? cause.message : t("tokenBudgetRecountFailed"));
        })
        .finally(() => {
          if (!controller.signal.aborted && sequence === requestSequence.current) {
            setRecounting((current) => { const next = new Set(current); next.delete(target.sampleId); return next; });
            setRecountRequest((current) => current?.sampleId === target.sampleId ? null : current);
          }
        });
    }, 400);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [editable, jobId, recountRequest]);

  const selectedItems = useMemo(() => page?.items.filter((item) => selected.has(item.sampleId)) ?? [], [page, selected]);
  const allPageSelected = Boolean(page?.items.length) && page!.items.every((item) => selected.has(item.sampleId));
  const itemAnnotation = (item: TokenBudgetReviewItem) => edits[item.sampleId] ?? item.annotation;
  const updateEdit = (item: TokenBudgetReviewItem, field: EditField, value: string) => {
    const annotation = updateAnnotation(itemAnnotation(item), field, value);
    setEdits((current) => ({ ...current, [item.sampleId]: annotation }));
    setRecountRequest({ sampleId: item.sampleId, expectedVersion: item.review.version, annotation });
    setError(null);
  };
  const toggleSelected = (sampleId: number, checked: boolean) => setSelected((current) => {
    const next = new Set(current);
    if (checked) next.add(sampleId); else next.delete(sampleId);
    return next;
  });
  const rewriteShort = async () => {
    if (!editable || rewriting || selectedItems.length < 1 || selectedItems.length > 500) return;
    setRewriting(true);
    setError(null);
    try {
      const result = await rewriteTokenBudgetShort(jobId, {
        sampleIds: selectedItems.map((item) => item.sampleId),
        expectedVersions: Object.fromEntries(selectedItems.map((item) => [String(item.sampleId), item.review.version])),
      });
      const proposals = new Map(result.proposals.map((proposal) => [proposal.sampleId, proposal]));
      setPage((current) => current ? { ...current, items: current.items.map((item) => {
        const proposal = proposals.get(item.sampleId);
        return proposal ? proposalFor(item, proposal) : item;
      }) } : current);
      setEdits((current) => ({ ...current, ...Object.fromEntries(result.proposals.map((proposal) => [proposal.sampleId, proposal.annotation])) }));
      setSelected(new Set());
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) { handleConflict(); return; }
      setError(cause instanceof Error ? cause.message : t("tokenBudgetRewriteFailed"));
    } finally {
      setRewriting(false);
    }
  };
  const applyOne = async (item: TokenBudgetReviewItem) => {
    if (!editable || applying.has(item.sampleId)) return;
    setApplying((current) => new Set(current).add(item.sampleId));
    setError(null);
    try {
      const result = await applyTokenBudgetProposal(jobId, { sampleId: item.sampleId, expectedVersion: item.review.version });
      setPage((current) => current ? {
        ...current,
        items: current.items.filter((candidate) => candidate.sampleId !== item.sampleId),
        targetCount: Math.max(0, current.targetCount - 1),
      } : current);
      setEdits((current) => { const next = { ...current }; delete next[item.sampleId]; return next; });
      setNotice(result.exportStarted ? null : t("tokenBudgetExportContinues"));
      await onApplied();
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) { handleConflict(); return; }
      setError(cause instanceof Error ? cause.message : t("tokenBudgetApplyFailed"));
      refreshCurrentPage();
    } finally {
      setApplying((current) => { const next = new Set(current); next.delete(item.sampleId); return next; });
    }
  };

  if (!editable) return <p className="review-empty token-budget-readonly">{t("tokenBudgetReviewReadOnly")}</p>;
  return <section className="token-budget-review-panel count-review-panel" aria-busy={loading || rewriting || recounting.size > 0 || applying.size > 0}>
    <div className="review-summary">
      <dl>
        <div><dt>{t("tokenBudgetReview")}</dt><dd>{page?.targetCount ?? 0}</dd></div>
        <div><dt>{t("tokenBudgetSelected")}</dt><dd>{selectedItems.length}</dd></div>
      </dl>
      <span className="status reviewing">{t("tokenBudgetReviewOpen")}</span>
    </div>
    {notice && <p className="review-notice" role="status">{notice}</p>}
    {error && <div className="review-error"><p role="alert">{error}</p><button className="secondary" type="button" onClick={refreshCurrentPage}>{t("retryCountReview")}</button></div>}
    <div className="review-batch-bar">
      <label className="checkbox"><input type="checkbox" disabled={loading || !page?.items.length || rewriting} checked={allPageSelected} onChange={(event) => setSelected(event.target.checked ? new Set(page?.items.map((item) => item.sampleId)) : new Set())} />{t("countReviewSelectPage")}</label>
      <button type="button" disabled={selectedItems.length < 1 || rewriting || applying.size > 0} aria-busy={rewriting} onClick={() => void rewriteShort()}>{rewriting ? t("tokenBudgetRewriting") : t("tokenBudgetRewriteShort")}</button>
    </div>
    {loading && <p className="review-empty">{t("tokenBudgetLoading")}</p>}
    {!loading && !error && page?.items.length === 0 && <p className="review-empty">{t("tokenBudgetEmpty")}</p>}
    {!loading && page && <div className="review-list token-budget-review-list">{page.items.map((item) => {
      const annotation = itemAnnotation(item);
      const recountingItem = recounting.has(item.sampleId);
      const applyingItem = applying.has(item.sampleId);
      const busy = recountingItem || applyingItem;
      const editingDisabled = applyingItem || rewriting;
      const proposal = item.proposal;
      return <article className="review-item token-budget-review-item" key={item.sampleId}>
        <div className="review-item-body">
          <header>
            <label className="checkbox"><input type="checkbox" disabled={editingDisabled} checked={selected.has(item.sampleId)} onChange={(event) => toggleSelected(item.sampleId, event.target.checked)} /><span>#{item.sampleId}</span></label>
            <code title={item.relativeImagePath}>{item.relativeImagePath}</code>
            <span className="status reviewing">{proposal?.status ?? item.review.status}</span>
          </header>
          <div className="review-evidence token-budget-evidence">
            <section><span>{t("tokenBudgetOriginal")}</span><strong>{item.review.originalTokens}</strong></section>
            <section><span>{t("tokenBudgetFinal")}</span><strong>{proposal?.finalTokens ?? item.review.finalTokens}</strong></section>
            <section><span>{t("tokenBudgetMaximum")}</span><strong>{item.review.maxTokens}</strong></section>
            <section><span>{t("tokenBudgetRemoved")}</span><small>{Object.entries(proposal?.removed ?? item.review.removed).filter(([, values]) => values.length).map(([field, values]) => `${field}: ${values.join(", ")}`).join(" / ") || "-"}</small></section>
          </div>
          <div className="token-budget-edit-grid">
            <label className="wide">NL<textarea rows={3} disabled={editingDisabled} value={annotationText(annotation, "nl")} onChange={(event) => updateEdit(item, "nl", event.target.value)} /></label>
            {EDITABLE_ARRAYS.map((field) => <label key={field}>{field}<input disabled={editingDisabled} value={annotationText(annotation, field)} onChange={(event) => updateEdit(item, field, event.target.value)} /></label>)}
          </div>
          <dl className="token-budget-protected"><div><dt>count</dt><dd>{annotation.count}</dd></div><div><dt>character</dt><dd>{annotation.character}</dd></div><div><dt>series</dt><dd>{annotation.series}</dd></div><div><dt>artist</dt><dd>{annotation.artist}</dd></div></dl>
          <div className="review-actions">
            <button type="button" className="secondary" disabled={editingDisabled} aria-busy={recountingItem} onClick={() => setRecountRequest({ sampleId: item.sampleId, expectedVersion: item.review.version, annotation })}>{recountingItem ? t("tokenBudgetRecounting") : t("tokenBudgetRecount")}</button>
            <button type="button" disabled={!proposal || busy || rewriting} aria-busy={applyingItem} onClick={() => void applyOne(item)}>{applyingItem ? t("tokenBudgetApplying") : t("tokenBudgetApply")}</button>
          </div>
        </div>
      </article>;
    })}</div>}
    <div className="review-footer"><div className="review-pagination">
      <button type="button" className="secondary" disabled={loading || pageIndex === 0} onClick={() => setPageIndex(0)}>{t("firstPage")}</button>
      <button type="button" className="secondary" disabled={loading || pageIndex === 0} onClick={() => setPageIndex((value) => Math.max(0, value - 1))}>{t("previousPage")}</button>
      <span>{t("pageNumber", { page: pageIndex + 1 })}</span>
      <button type="button" className="secondary" disabled={loading || page?.nextAfterSampleId === null || page?.nextAfterSampleId === undefined} onClick={() => {
        if (page?.nextAfterSampleId === null || page?.nextAfterSampleId === undefined) return;
        setPageCursors((current) => [...current.slice(0, pageIndex + 1), page.nextAfterSampleId]);
        setPageIndex((value) => value + 1);
      }}>{t("nextPage")}</button>
    </div></div>
  </section>;
}
