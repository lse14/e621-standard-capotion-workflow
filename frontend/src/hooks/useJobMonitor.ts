import { useCallback, useEffect, useRef, useState } from "react";
import { listJobs, pollJob, type JobListEntry, type JobSnapshot } from "../api";

export type JobListCursor = { createdAt: string; jobId: string } | null;
export type IssueCursor = { sampleId: number; issueId: string | null };

export type JobMonitorState = {
  jobId: string;
  jobs: JobListEntry[];
  jobsCursor: JobListCursor;
  jobsLoading: boolean;
  jobsError: string | null;
  snapshot: JobSnapshot | null;
  snapshotLoading: boolean;
  snapshotError: string | null;
  issueCursor: IssueCursor;
  selectJob: (jobId: string) => void;
  refreshJobs: () => Promise<void>;
  loadMoreJobs: () => Promise<void>;
  refreshSnapshot: () => Promise<void>;
  firstIssuePage: () => void;
  nextIssuePage: () => void;
};

type UseJobMonitorOptions = {
  jobListFailureMessage: string;
  jobRequestFailureMessage: string;
};

const activeJobStatuses = new Set(["running", "exporting", "committing", "cancelling"]);
const initialIssueCursor: IssueCursor = { sampleId: 0, issueId: null };
const jobStorageKey = "anima.ui.jobId.v1";

export function isActiveJobStatus(status: string): boolean {
  return activeJobStatuses.has(status);
}

function loadStoredJobId(): string {
  try {
    return window.localStorage.getItem(jobStorageKey) ?? "";
  } catch {
    return "";
  }
}

export function useJobMonitor({
  jobListFailureMessage,
  jobRequestFailureMessage,
}: UseJobMonitorOptions): JobMonitorState {
  const [jobId, setJobId] = useState(loadStoredJobId);
  const [jobs, setJobs] = useState<JobListEntry[]>([]);
  const [jobsCursor, setJobsCursor] = useState<JobListCursor>(null);
  const [jobsLoading, setJobsLoading] = useState(false);
  const [jobsError, setJobsError] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<JobSnapshot | null>(null);
  const [snapshotLoading, setSnapshotLoading] = useState(false);
  const [snapshotError, setSnapshotError] = useState<string | null>(null);
  const [issueCursor, setIssueCursor] = useState<IssueCursor>(initialIssueCursor);
  const mountedRef = useRef(true);
  const eventCursorRef = useRef(0);
  const snapshotInFlightRequestIdRef = useRef<number | null>(null);
  const snapshotAbortControllerRef = useRef<AbortController | null>(null);
  const jobsRequestIdRef = useRef(0);
  const snapshotRequestIdRef = useRef(0);
  const jobListFailureMessageRef = useRef(jobListFailureMessage);
  const jobRequestFailureMessageRef = useRef(jobRequestFailureMessage);

  jobListFailureMessageRef.current = jobListFailureMessage;
  jobRequestFailureMessageRef.current = jobRequestFailureMessage;

  const loadJobsPage = useCallback(async (cursor: JobListCursor, append: boolean) => {
    const requestId = jobsRequestIdRef.current + 1;
    jobsRequestIdRef.current = requestId;
    if (mountedRef.current) setJobsLoading(true);
    try {
      const page = await listJobs(cursor?.createdAt ?? null, cursor?.jobId ?? null);
      if (!mountedRef.current || requestId !== jobsRequestIdRef.current) return;
      setJobs((current) => append ? [...current, ...page.jobs] : page.jobs);
      setJobsCursor(page.nextAfterCreatedAt && page.nextAfterJobId
        ? { createdAt: page.nextAfterCreatedAt, jobId: page.nextAfterJobId }
        : null);
      setJobsError(null);
    } catch (cause) {
      if (!mountedRef.current || requestId !== jobsRequestIdRef.current) return;
      const message = cause instanceof Error ? cause.message : jobListFailureMessageRef.current;
      setJobsError(message);
    } finally {
      if (mountedRef.current && requestId === jobsRequestIdRef.current) setJobsLoading(false);
    }
  }, []);

  const refreshJobs = useCallback(() => loadJobsPage(null, false), [loadJobsPage]);
  const loadMoreJobs = useCallback(() => jobsCursor ? loadJobsPage(jobsCursor, true) : Promise.resolve(), [jobsCursor, loadJobsPage]);

  const refreshSnapshot = useCallback(async () => {
    if (!jobId || snapshotInFlightRequestIdRef.current !== null) return;
    const requestId = snapshotRequestIdRef.current + 1;
    snapshotRequestIdRef.current = requestId;
    snapshotInFlightRequestIdRef.current = requestId;
    const abortController = new AbortController();
    snapshotAbortControllerRef.current = abortController;
    if (mountedRef.current) setSnapshotLoading(true);
    try {
      const value = await pollJob(jobId, eventCursorRef.current, issueCursor.sampleId, issueCursor.issueId, abortController.signal);
      if (!mountedRef.current || requestId !== snapshotRequestIdRef.current) return;
      eventCursorRef.current = value.snapshotRequired ? value.job.lastEventId : value.nextAfterEventId;
      setSnapshot(value);
      setSnapshotError(null);
    } catch (cause) {
      if (!mountedRef.current || requestId !== snapshotRequestIdRef.current) return;
      const message = cause instanceof Error ? cause.message : jobRequestFailureMessageRef.current;
      setSnapshotError(message);
    } finally {
      if (snapshotInFlightRequestIdRef.current === requestId) snapshotInFlightRequestIdRef.current = null;
      if (snapshotAbortControllerRef.current === abortController) snapshotAbortControllerRef.current = null;
      if (mountedRef.current && requestId === snapshotRequestIdRef.current) setSnapshotLoading(false);
    }
  }, [issueCursor.issueId, issueCursor.sampleId, jobId]);

  const invalidateSnapshotRequest = useCallback(() => {
    snapshotRequestIdRef.current += 1;
    snapshotAbortControllerRef.current?.abort();
    snapshotAbortControllerRef.current = null;
    snapshotInFlightRequestIdRef.current = null;
  }, []);

  const selectJob = useCallback((nextJobId: string) => {
    invalidateSnapshotRequest();
    eventCursorRef.current = 0;
    setIssueCursor(initialIssueCursor);
    setJobId(nextJobId);
    setSnapshot(null);
    setSnapshotLoading(false);
    setSnapshotError(null);
  }, [invalidateSnapshotRequest]);

  const firstIssuePage = useCallback(() => {
    invalidateSnapshotRequest();
    setIssueCursor(initialIssueCursor);
  }, [invalidateSnapshotRequest]);
  const nextIssuePage = useCallback(() => {
    if (!snapshot) return;
    invalidateSnapshotRequest();
    setIssueCursor({ sampleId: snapshot.nextIssueAfterSampleId, issueId: snapshot.nextIssueAfterIssueId });
  }, [invalidateSnapshotRequest, snapshot]);

  useEffect(() => {
    mountedRef.current = true;
    void refreshJobs();
    return () => {
      mountedRef.current = false;
      jobsRequestIdRef.current += 1;
    };
  }, [refreshJobs]);

  useEffect(() => {
    if (!jobId) return;
    const tick = () => { void refreshSnapshot(); };
    tick();
    const timer = window.setInterval(tick, isActiveJobStatus(snapshot?.job.status ?? "") ? 1000 : 5000);
    return () => window.clearInterval(timer);
  }, [issueCursor.issueId, issueCursor.sampleId, jobId, refreshSnapshot, snapshot?.job.status]);

  useEffect(() => {
    try {
      window.localStorage.setItem(jobStorageKey, jobId);
    } catch {
      // Presentation preference is optional.
    }
  }, [jobId]);

  return {
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
  };
}
