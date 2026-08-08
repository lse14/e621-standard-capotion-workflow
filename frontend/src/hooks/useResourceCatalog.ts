import { useCallback, useEffect, useRef, useState } from "react";
import { listResources, type ResourceCatalogResponse } from "../api";

export type ResourceCatalogState = {
  catalog: ResourceCatalogResponse | null;
  loading: boolean;
  error: string | null;
  refresh: (invalidate?: boolean) => Promise<void>;
};

type UseResourceCatalogOptions = {
  failureMessage: string;
  onCatalog: (catalog: ResourceCatalogResponse, invalidate: boolean) => void;
};

export function useResourceCatalog({ failureMessage, onCatalog }: UseResourceCatalogOptions): ResourceCatalogState {
  const [catalog, setCatalog] = useState<ResourceCatalogResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const mountedRef = useRef(true);
  const requestIdRef = useRef(0);
  const failureMessageRef = useRef(failureMessage);
  const onCatalogRef = useRef(onCatalog);

  failureMessageRef.current = failureMessage;
  onCatalogRef.current = onCatalog;

  const refresh = useCallback(async (invalidate = true) => {
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    if (mountedRef.current) setLoading(true);
    try {
      const next = await listResources();
      if (!mountedRef.current || requestId !== requestIdRef.current) return;
      setCatalog(next);
      onCatalogRef.current(next, invalidate);
      setError(null);
    } catch (cause) {
      if (!mountedRef.current || requestId !== requestIdRef.current) return;
      setError(cause instanceof Error ? cause.message : failureMessageRef.current);
    } finally {
      if (mountedRef.current && requestId === requestIdRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    void refresh(false);
    return () => {
      mountedRef.current = false;
      requestIdRef.current += 1;
    };
  }, [refresh]);

  return { catalog, loading, error, refresh };
}
