# Reliability And 100k Stability Design

## Goal

Fix the currently reproducible control-plane and worker-transport bugs without broad architectural decomposition, while preserving the existing 100,000-image bounded-memory behavior.

## Confirmed Scope

1. A failed initial pipeline thread start leaves a ghost entry in `PipelineService._threads`.
2. Resume can persist `running` before noticing that the previous thread is still settling, leaving no successor thread.
3. Recovery can persist a resumed status and then fail to start its thread, leaving `running` with no registered thread.
4. Formal discard removes the SQLite dataset claim but leaves the live Windows file handle in `JobPreparationService._locks`.
5. Worker stderr is piped but not drained during request/response exchange, so a full pipe can block the worker before it writes stdout.

Resource-default handling is not in scope because the current source accepts a non-empty known profile subset and its regression coverage already passes. Broad splitting of `StateDatabase`, `PipelineService`, or `frontend/src/App.tsx` is also out of scope.

## Architecture

Keep the existing Core/worker process boundary, SQLite schema, JSONL protocol, queue sizes, indexes, and frontend contracts. Each fix stays with its current owner:

- `PipelineService` and `PipelineRecoveryMixin` preserve the invariant that a running persisted state has a successfully started registered thread.
- `JobPreparationService` remains the owner of live `DatasetLock` objects; the discard API coordinates persisted lifecycle completion with release of the live handle.
- `StdioJsonlTransport` owns bounded stderr draining because it owns the child process pipes.

No new runtime dependency or JSON schema is introduced.

## Thread State Behavior

- Initial start failure unregisters the thread; the job remains `preparing_workspace` and can be retried.
- Resume refuses with a bounded, user-visible conflict while the old thread is still registered. It does not mutate job or module status before that check.
- Resume thread-start failure restores the prior paused/reviewing state, retaining the existing behavior.
- Recovery thread-start failure restores `interrupted`, the original `resume_status`, and the current module. Already completed recovery work remains idempotent for the next attempt.
- Count Review continuation retains its existing rollback behavior.

## Dataset Lock Behavior

After overlay discard and persisted transition to `discarded`, the API asks `JobPreparationService` to release the matching live lock. Release closes the Windows handle, closes the lock-owned database connection, and removes the in-memory registry entry. The operation is idempotent when the process was restarted and no live lock exists.

If live-lock release fails after the durable transition, a repeated confirmed discard request recognizes the already-discarded job and retries only the owner release. It does not repeat overlay deletion or relax initial journal eligibility.

The live handle is never released before journal safety and discard eligibility have been validated.

## Worker Stderr Behavior

`StdioJsonlTransport` starts one daemon drain thread when the process has a stderr pipe. The drainer continuously reads fixed-size chunks and retains at most the last 64 KiB for diagnostics. Transport close waits for process termination, then joins the drainer and closes streams. This prevents both pipe backpressure deadlock and unbounded resident memory.

If the drain thread itself cannot start, transport construction closes stdin, reaps the worker with the existing bounded wait/terminate/kill sequence, closes all streams, and raises a transport-level error.

## 100k Acceptance

The existing stress suite remains the capacity contract:

- 100,000 samples and 5,000 issues use keyset pagination.
- peak traced Python memory remains below 32 MiB;
- the control-plane database remains below 256 MiB;
- WAL remains within the 64 MiB configured limit and truncates cleanly;
- OCR retains one resident lease.

The current reference run completed both tests in 79.163 seconds, with maximum traced memory 2,195,229 bytes, maximum database size 146,776,064 bytes, and zero-byte WAL after truncate. Runtime is recorded diagnostically, not used as a machine-dependent hard gate.

## Verification

Every production change follows red-green TDD. Completion requires the focused regression tests, Core module-boundary tests, the 100k stress suite, frontend typecheck/build where the project toolchain is available, and `git diff --check`. The full non-stress suite must also be run; pre-existing environment or frozen-scope conflicts are recorded rather than hidden by changing unrelated product behavior or test prerequisites. Generated test artifacts and processes must be cleaned before commit.
