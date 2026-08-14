import { useResolveIngest, type UseResolveIngest } from './useResolveIngest'

/** The Prospect workflow: paste N company domains → resolve → (skip already-ingested)
 * → people-ingest → hand the resolved set to a saved-leads similarity search. This
 * is now a thin consumer of the shared `useResolveIngest` pipeline; only the
 * checkpoint key is Prospect-specific. Behavior is identical to before the refactor. */

const PROSPECT_CHECKPOINT_KEY = 'searchui.prospect.v1'

export function useProspectRun(): UseResolveIngest {
  return useResolveIngest({ checkpointKey: PROSPECT_CHECKPOINT_KEY })
}
