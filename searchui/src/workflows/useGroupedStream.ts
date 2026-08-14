import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError,
  runSimilarityGroupedStream,
  type CompanyOption,
  type SimilarityGroupHit,
  type SimilarityGroupedParams,
} from '../api'
import type { ApolloRecord } from '../types'

/** One company's slot in the incremental grouped-ranking view. Slots are created
 * up front from the request companies (request order) as `pending`, then filled
 * as `group` / `item_error` stream events arrive (placed by their `index`). */
export interface GroupedSlot {
  index: number
  companyId: string
  /** Fallback label from the request company chip (used until the record arrives). */
  companyName: string | null
  /** Authoritative company record from the `group` event (name/domain), if any. */
  company: ApolloRecord | null
  status: 'pending' | 'done' | 'error'
  hits: SimilarityGroupHit[]
  /** Friendly per-company failure reason (item_error). */
  errorReason?: string
}

export interface GroupedRunView {
  /** A run has been started (results area should render). */
  active: boolean
  /** Currently streaming (no terminal complete/error/abort yet). */
  streaming: boolean
  /** Terminal complete received — export is now allowed. */
  complete: boolean
  slots: GroupedSlot[]
  /** Companies in the run (= slots.length). */
  total: number
  /** Companies resolved so far (done or errored). */
  ranked: number
  /** Total hits rendered across done slots. */
  totalHits: number
  /** Companies that failed to rank (from the terminal complete). */
  failedTotal: number
  searchId: number | null
}

export interface UseGroupedStream {
  view: GroupedRunView
  /** Terminal error message (stream-level), if any. */
  error: string | null
  /** Start a streamed grouped ranking. Resolves when the stream ends (complete,
   * error, or abort); returns whether it completed + the persisted search id. */
  run: (
    params: SimilarityGroupedParams,
    companies: CompanyOption[],
  ) => Promise<{ completed: boolean; searchId: number | null }>
  /** Abort the in-flight stream (cancels backend work); keeps partial results. */
  cancel: () => void
  /** Abort + clear all state back to idle. */
  reset: () => void
}

function initialSlots(companies: CompanyOption[]): GroupedSlot[] {
  return companies.map((c, index) => ({
    index,
    companyId: (c.mongo_id || c.apollo_id || '').trim(),
    companyName: c.name ?? null,
    company: null,
    status: 'pending',
    hits: [],
  }))
}

export function useGroupedStream(): UseGroupedStream {
  const [slots, setSlots] = useState<GroupedSlot[]>([])
  const [streaming, setStreaming] = useState(false)
  const [complete, setComplete] = useState(false)
  const [searchId, setSearchId] = useState<number | null>(null)
  const [failedTotal, setFailedTotal] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  // Abort any in-flight stream when the component unmounts (leaving the page
  // cancels the backend ranking work).
  useEffect(
    () => () => {
      abortRef.current?.abort()
      abortRef.current = null
    },
    [],
  )

  const run = useCallback(
    async (params: SimilarityGroupedParams, companies: CompanyOption[]) => {
      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller

      setSlots(initialSlots(companies))
      setStreaming(true)
      setComplete(false)
      setSearchId(null)
      setFailedTotal(0)
      setError(null)

      let completed = false
      let resultSearchId: number | null = null
      try {
        await runSimilarityGroupedStream(params, {
          signal: controller.signal,
          onGroup: (group) =>
            setSlots((prev) =>
              prev.map((slot) =>
                slot.index === group.index
                  ? { ...slot, status: 'done', hits: group.hits, company: group.company }
                  : slot,
              ),
            ),
          onItemError: (itemError) =>
            setSlots((prev) =>
              prev.map((slot) =>
                slot.index === itemError.index
                  ? { ...slot, status: 'error', errorReason: itemError.reason }
                  : slot,
              ),
            ),
          onProgress: () => {
            // Heartbeat only — the "n of N" count derives from resolved slots.
          },
          onComplete: (c) => {
            completed = true
            resultSearchId = c.search_id
            setComplete(true)
            setSearchId(c.search_id)
            setFailedTotal(c.failed)
          },
        })
      } catch (err) {
        if (!controller.signal.aborted) {
          setError(err instanceof ApiError || err instanceof Error ? err.message : 'Grouped ranking failed')
        }
      } finally {
        if (abortRef.current === controller) abortRef.current = null
        setStreaming(false)
      }
      return { completed, searchId: resultSearchId }
    },
    [],
  )

  const cancel = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
    setStreaming(false)
  }, [])

  const reset = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
    setSlots([])
    setStreaming(false)
    setComplete(false)
    setSearchId(null)
    setFailedTotal(0)
    setError(null)
  }, [])

  const ranked = slots.reduce((n, s) => n + (s.status === 'pending' ? 0 : 1), 0)
  const totalHits = slots.reduce((n, s) => n + (s.status === 'done' ? s.hits.length : 0), 0)
  const view: GroupedRunView = {
    active: streaming || complete || slots.length > 0,
    streaming,
    complete,
    slots,
    total: slots.length,
    ranked,
    totalHits,
    failedTotal,
    searchId,
  }

  return { view, error, run, cancel, reset }
}
