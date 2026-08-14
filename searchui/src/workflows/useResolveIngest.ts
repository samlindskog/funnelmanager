import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  resolveCompany,
  runSearch,
  runSimilaritySearch,
  type CompanyOption,
  type EmbeddingProgress,
  type SearchProgress,
} from '../api'
import { useProgress } from '../progress'
import type { CompanyRecord } from '../types'

/** The shared resolve+ingest pipeline behind the domain-driven workflows (Prospect,
 * Top-people-per-company): paste N company domains → resolve each to an Apollo org
 * → (optionally skip already-ingested) → people-ingest each. It owns the state
 * machine, the bounded-concurrency queue, the throttle/backpressure/partial display,
 * and a localStorage checkpoint so a refresh/crash resumes mid-run instead of
 * re-entering everything. It is UI-only orchestration over existing search endpoints
 * — no new backend surface. Each workflow passes its own `checkpointKey` so their
 * persisted runs stay independent. */

export type ResolveIngestPhase = 'idle' | 'resolving' | 'confirm' | 'ingesting' | 'done'

export type ResolveIngestStatus =
  | 'pending'
  | 'resolving'
  | 'not-found'
  | 'probing'
  | 'already-ingested'
  | 'ingesting'
  | 'done'
  | 'failed'

export interface ResolveIngestRow {
  /** Normalized bare domain (the row key). */
  domain: string
  status: ResolveIngestStatus
  companyName?: string | null
  /** Company record (Mongo) id — the similarity company filter + skip probe. */
  orgRecordId?: string | null
  /** Apollo organization id — the people-ingest `organization_id` param. */
  orgApolloId?: string | null
  orgDomain?: string | null
  /** Employee count from the resolved company record — the ingest estimate. */
  employeeCount?: number | null
  /** People stored/linked for this company (grows during ingest). */
  people?: number
  error?: string | null
  /** Apollo fetching is paused for this company waiting on embedding (backpressure).
   * Transient — set on a `throttled` event, cleared on the next progress/complete. */
  throttled?: boolean
  /** Ingest stopped short (Apollo page cap / max entries) — additive completion flag. */
  partial?: boolean
  partialReason?: string | null
  /** Embeddings that failed to index for this company (honest completion). */
  embedFailed?: number
  embedIndexed?: number
  /** Friendly reason for the embedding failures (e.g. "Milvus under write pressure"). */
  embedReason?: string
  /** Embedding was cancelled for this company but leads still collected — sticky info. */
  embeddingDetached?: boolean
}

/** Resolve requests are tiny; ingest walks Apollo pages so it stays sequential-ish
 * (the 2026-08-11 incident proved ~30 concurrent streams starve the single search
 * replica). Hard cap 3; default 2; ~2s between any two stream starts. */
const RESOLVE_CONCURRENCY = 3
const INGEST_CONCURRENCY = 2
const MAX_INGEST_CONCURRENCY = 3
const STAGGER_MS = 2000
/** Apollo ingest walks at most 100k entries per company — the estimate cap. */
const PER_COMPANY_WALK_CAP = 100_000

interface Checkpoint {
  phase: ResolveIngestPhase
  rows: ResolveIngestRow[]
  skipIfIngested: boolean
  savedAt: number
}

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback
}

/** Strip scheme, userinfo, path, query, fragment, port and a leading `www.`,
 * lowercased — so `HTTPS://www.Acme.com/careers` and `jane@acme.com` both yield
 * `acme.com`. Returns '' for input with no host. */
export function normalizeDomain(raw: string): string {
  let value = raw.trim().toLowerCase()
  if (!value) return ''
  value = value.replace(/^[a-z][a-z0-9+.-]*:\/\//, '')
  // A user@host or an email address — keep the host part.
  value = value.split('@').pop() ?? value
  value = value.split('/')[0].split('?')[0].split('#')[0]
  value = value.split(':')[0]
  value = value.replace(/^www\./, '')
  return value.trim()
}

/** Bounded-concurrency pool: at most `concurrency` workers run at once, each
 * pulling the next item until the list drains. Worker rejections must be handled
 * inside `worker` (a bogus domain marks its row and continues, never aborts). */
async function runPool<T>(
  items: T[],
  concurrency: number,
  worker: (item: T) => Promise<void>,
): Promise<void> {
  let index = 0
  const next = async (): Promise<void> => {
    const i = index++
    if (i >= items.length) return
    await worker(items[i])
    return next()
  }
  const lanes = Math.max(1, Math.min(concurrency, items.length))
  await Promise.all(Array.from({ length: lanes }, () => next()))
}

/** On resume, any status that reflects work interrupted mid-flight is downgraded
 * to a re-runnable state. People-ingest is an idempotent upsert, so re-running an
 * interrupted ingest is safe (and the skip probe catches a company that finished). */
function downgradeTransient(row: ResolveIngestRow): ResolveIngestRow {
  if (row.status === 'resolving') {
    return { ...row, status: 'pending', orgRecordId: null, orgApolloId: null, companyName: null }
  }
  if (row.status === 'probing' || row.status === 'ingesting') {
    return { ...row, status: 'pending', throttled: false, embeddingDetached: false }
  }
  return row
}

function loadCheckpoint(checkpointKey: string): Checkpoint | null {
  try {
    const raw = localStorage.getItem(checkpointKey)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Checkpoint
    if (!parsed || !Array.isArray(parsed.rows) || !parsed.rows.length) return null
    return parsed
  } catch {
    return null
  }
}

export interface ResolveIngestSummary {
  resolved: number
  ingested: number
  skipped: number
  notFound: number
  failed: number
  peopleLinked: number
}

export interface UseResolveIngest {
  phase: ResolveIngestPhase
  rows: ResolveIngestRow[]
  domainCount: number
  skipIfIngested: boolean
  setSkipIfIngested: (next: boolean) => void
  /** True when this run was restored from a checkpoint on mount. */
  resumed: boolean
  /** Replace the domain list (idle only) — normalizes + dedupes internally. */
  setDomains: (raw: string[]) => void
  /** Stage 1→2: resolve every domain (and probe skip-if-ingested), then confirm. */
  resolve: () => void
  /** Stage 2 gate → ingest: people-ingest every ready company. */
  startIngest: () => void
  /** Clear the run and the checkpoint, back to idle. */
  reset: () => void
  /** Ready-to-ingest companies (resolved, not skipped/not-found) for the confirm gate. */
  readyCount: number
  /** Σ employee_count over ready companies, capped per company at the walk limit. */
  estimatedPeople: number
  /** Whether any ready company lacked an employee_count (estimate is a floor). */
  estimateHasUnknowns: boolean
  /** Every resolved company as a named similarity chip (includes already-ingested). */
  resolvedCompanies: CompanyOption[]
  summary: ResolveIngestSummary
}

export interface ResolveIngestOptions {
  /** localStorage key for this workflow's checkpoint (distinct per workflow so a
   * Prospect run and a Top-people run persist independently). */
  checkpointKey: string
}

export function useResolveIngest({ checkpointKey }: ResolveIngestOptions): UseResolveIngest {
  const progress = useProgress()
  const [rows, setRows] = useState<ResolveIngestRow[]>([])
  const [phase, setPhase] = useState<ResolveIngestPhase>('idle')
  const [skipIfIngested, setSkipIfIngested] = useState(true)
  const [resumed, setResumed] = useState(false)

  // Refs mirror the latest values so async pool workers read fresh state without
  // re-created closures. Kept in sync on every render.
  const rowsRef = useRef<ResolveIngestRow[]>(rows)
  const skipRef = useRef(skipIfIngested)
  // Serializes runResolve/runIngest and defends against a StrictMode double-fire.
  const activeRef = useRef(false)
  // Enforces >=STAGGER_MS between any two ingest stream starts.
  const lastStartRef = useRef(0)
  // Latest checkpoint key so callbacks read the current workflow's key.
  const checkpointKeyRef = useRef(checkpointKey)
  useEffect(() => {
    rowsRef.current = rows
  }, [rows])
  useEffect(() => {
    skipRef.current = skipIfIngested
  }, [skipIfIngested])
  useEffect(() => {
    checkpointKeyRef.current = checkpointKey
  }, [checkpointKey])

  const patchRow = useCallback(
    (
      domain: string,
      patch: Partial<ResolveIngestRow> | ((row: ResolveIngestRow) => Partial<ResolveIngestRow>),
    ) => {
      setRows((prev) => {
        const next = prev.map((row) =>
          row.domain === domain
            ? { ...row, ...(typeof patch === 'function' ? patch(row) : patch) }
            : row,
        )
        rowsRef.current = next
        return next
      })
    },
    [],
  )

  const clearCheckpoint = useCallback(() => {
    try {
      localStorage.removeItem(checkpointKeyRef.current)
    } catch {
      /* storage may be unavailable (private mode); the run still works in-memory. */
    }
  }, [])

  // ---- stage 1: resolve a domain to an Apollo org, then probe skip-if-ingested.
  const resolveOne = useCallback(
    async (domain: string) => {
      patchRow(domain, { status: 'resolving', error: null })
      try {
        // Local-first: resolve from already-stored orgs (free) before spending
        // an Apollo export credit on a company search.
        let localHit: { mongo_id: string; apollo_id: string; name: string | null } | null = null
        try {
          const local = await resolveCompany(domain)
          if (local.mongo_id && local.apollo_id) {
            localHit = { mongo_id: local.mongo_id, apollo_id: local.apollo_id, name: local.name }
          }
        } catch {
          /* not stored locally — fall through to the Apollo search */
        }
        const response = localHit
          ? null
          : await runSearch({
          query: '',
          entity_type: 'companies',
          page: 1,
          per_page: 1,
          company_domain: domain,
        })
        const company = localHit
          ? null
          : (response!.history.results.find(
              (record): record is CompanyRecord => record.entity_type === 'company',
            ) ?? null)
        if (!localHit && !company) {
          patchRow(domain, { status: 'not-found' })
          return
        }
        const orgRecordId = localHit
          ? localHit.mongo_id
          : (company!.mongo_id || '').trim() || null
        const orgApolloId = localHit
          ? localHit.apollo_id
          : String(company!.organization_id || company!.id || '').trim() || null
        patchRow(domain, {
          companyName: (localHit ? localHit.name : company!.name) || domain,
          orgRecordId,
          orgApolloId,
          orgDomain: (company && company.domain) || domain,
          employeeCount:
            company && typeof company.employee_count === 'number' ? company.employee_count : null,
          status: skipRef.current && orgRecordId ? 'probing' : 'pending',
        })
        if (skipRef.current && orgRecordId) {
          try {
            // Mongo-only probe (no embeds): >0 results ⇒ people already linked.
            const probe = await runSimilaritySearch({
              query: '',
              embeds: [],
              companyIds: [orgRecordId],
              limit: 1,
            })
            if (probe.results.length > 0) {
              patchRow(domain, { status: 'already-ingested' })
              return
            }
          } catch {
            // Probe is a best-effort cost saver; fall through to ingest on failure.
          }
          patchRow(domain, { status: 'pending' })
        }
      } catch (err) {
        patchRow(domain, { status: 'failed', error: errorMessage(err, 'resolve failed') })
      }
    },
    [patchRow],
  )

  const runResolve = useCallback(async () => {
    if (activeRef.current) return
    activeRef.current = true
    setPhase('resolving')
    try {
      const worklist = rowsRef.current
        .filter((row) => row.status === 'pending' && !row.orgRecordId)
        .map((row) => row.domain)
      await runPool(worklist, RESOLVE_CONCURRENCY, resolveOne)
    } finally {
      activeRef.current = false
      // Auto-advance past the confirm gate when nothing is ingestable — every
      // company already-ingested / not-found / failed leaves zero pending rows, so
      // there is nothing to confirm and no ingest to run. Go straight to `done` so
      // the final step unlocks (this is the zero-Apollo-spend rerank path). Only the
      // has-pending case still gates on the confirm click.
      const anyReady = rowsRef.current.some(
        (row) => Boolean(row.orgRecordId) && row.status === 'pending',
      )
      setPhase(anyReady ? 'confirm' : 'done')
    }
  }, [resolveOne])

  // ---- stage 2: people-ingest one resolved company (drives the global rings).
  const ingestOne = useCallback(
    async (row: ResolveIngestRow) => {
      // Stagger starts so first_page bursts don't align across lanes.
      const now = Date.now()
      const wait = Math.max(0, lastStartRef.current + STAGGER_MS - now)
      lastStartRef.current = now + wait
      if (wait > 0) await new Promise((resolve) => setTimeout(resolve, wait))

      const { domain } = row
      patchRow(domain, { status: 'ingesting', error: null, people: row.people ?? 0 })
      const run = progress.beginRun()
      try {
        await runSearch(
          {
            query: '',
            entity_type: 'people',
            page: 1,
            per_page: 100,
            ...(row.orgApolloId ? { organization_id: row.orgApolloId } : {}),
            ...(row.companyName ? { organization_display_name: row.companyName } : {}),
            ...(row.orgDomain ? { organization_domain: row.orgDomain } : {}),
          },
          {
            onProgress: (event: SearchProgress) => {
              if (event.total_pages > 0) {
                run.reportIngest({
                  done: event.page,
                  total: event.total_pages,
                  streamId: event.ingest_stream_id,
                })
              }
              // Any progress event clears the paused/throttled indicator.
              patchRow(domain, { people: event.stored, throttled: false })
            },
            onThrottled: () => {
              // Server-side backpressure paused fetching — expected, not a stall.
              run.reportIngest({ throttled: true })
              patchRow(domain, { throttled: true })
            },
            onEmbeddingDetached: () => {
              // Embedding cancelled but leads keep collecting — informational.
              run.reportIngest({ embeddingDetached: true })
              patchRow(domain, { embeddingDetached: true })
            },
            onEmbeddingProgress: (event: EmbeddingProgress) => {
              if ((event.failed ?? 0) > 0) {
                patchRow(domain, {
                  embedFailed: event.failed,
                  embedIndexed: event.indexed,
                  ...(event.reason ? { embedReason: event.reason } : {}),
                })
              }
              // complete:true / error is terminal even when done < total (failed>0).
              if (event.complete || event.error) {
                run.reportEmbed({
                  complete: true,
                  streamId: event.embedding_stream_id,
                  failed: event.failed,
                  indexed: event.indexed,
                })
                return
              }
              run.reportEmbed({
                done: event.done,
                total: event.total,
                streamId: event.embedding_stream_id,
                failed: event.failed,
                indexed: event.indexed,
              })
            },
            onFirstPage: (page) => patchRow(domain, { people: page.history.total_results }),
            onComplete: (done) => {
              run.reportIngest({ complete: true })
              patchRow(domain, {
                people: done.history.total_results,
                throttled: false,
                ...(done.partial ? { partial: true, partialReason: done.reason ?? null } : {}),
              })
            },
          },
        )
        patchRow(domain, (current) => ({ status: 'done', people: current.people ?? 0 }))
      } catch (err) {
        patchRow(domain, { status: 'failed', error: errorMessage(err, 'ingest failed') })
      } finally {
        run.end()
      }
    },
    [patchRow, progress],
  )

  const runIngest = useCallback(async () => {
    if (activeRef.current) return
    activeRef.current = true
    setPhase('ingesting')
    lastStartRef.current = 0
    try {
      const worklist = rowsRef.current.filter(
        (row) => Boolean(row.orgRecordId) && row.status === 'pending',
      )
      await runPool(worklist, Math.min(INGEST_CONCURRENCY, MAX_INGEST_CONCURRENCY), ingestOne)
    } finally {
      activeRef.current = false
      setPhase('done')
    }
  }, [ingestOne])

  // ---- resume-on-mount: restore a checkpoint and continue in-flight work once.
  const resumeStartedRef = useRef(false)
  useEffect(() => {
    if (resumeStartedRef.current) return
    resumeStartedRef.current = true
    const saved = loadCheckpoint(checkpointKeyRef.current)
    if (!saved) return
    const restored = saved.rows.map(downgradeTransient)
    rowsRef.current = restored
    setRows(restored)
    setSkipIfIngested(saved.skipIfIngested)
    skipRef.current = saved.skipIfIngested
    setPhase(saved.phase)
    setResumed(true)
    if (saved.phase === 'resolving') void runResolve()
    else if (saved.phase === 'ingesting') void runIngest()
    // 'idle' | 'confirm' | 'done' restore state only and wait for the user.
  }, [runResolve, runIngest])

  // ---- persist a checkpoint on every change; drop it once the run completes.
  useEffect(() => {
    if (phase === 'idle' && rows.length === 0) return
    if (phase === 'done') {
      clearCheckpoint()
      return
    }
    const snapshot: Checkpoint = { phase, rows, skipIfIngested, savedAt: Date.now() }
    try {
      localStorage.setItem(checkpointKey, JSON.stringify(snapshot))
    } catch {
      /* best-effort; the run continues in-memory without a checkpoint. */
    }
  }, [phase, rows, skipIfIngested, checkpointKey, clearCheckpoint])

  const setDomains = useCallback((raw: string[]) => {
    const seen = new Set<string>()
    const next: ResolveIngestRow[] = []
    for (const item of raw) {
      const domain = normalizeDomain(item)
      if (!domain || seen.has(domain)) continue
      seen.add(domain)
      next.push({ domain, status: 'pending' })
    }
    rowsRef.current = next
    setRows(next)
    setPhase('idle')
    setResumed(false)
  }, [])

  const reset = useCallback(() => {
    activeRef.current = false
    lastStartRef.current = 0
    rowsRef.current = []
    setRows([])
    setPhase('idle')
    setResumed(false)
    clearCheckpoint()
  }, [clearCheckpoint])

  const readyRows = useMemo(
    () => rows.filter((row) => Boolean(row.orgRecordId) && row.status === 'pending'),
    [rows],
  )
  const estimatedPeople = useMemo(
    () =>
      readyRows.reduce(
        (sum, row) => sum + Math.min(row.employeeCount ?? 0, PER_COMPANY_WALK_CAP),
        0,
      ),
    [readyRows],
  )
  const estimateHasUnknowns = useMemo(
    () => readyRows.some((row) => row.employeeCount == null),
    [readyRows],
  )
  const resolvedCompanies = useMemo<CompanyOption[]>(
    () =>
      rows
        .filter((row) => Boolean(row.orgRecordId))
        .map((row) => ({
          mongo_id: row.orgRecordId ?? null,
          apollo_id: row.orgApolloId ?? null,
          name: row.companyName ?? null,
        })),
    [rows],
  )
  const summary = useMemo<ResolveIngestSummary>(() => {
    let resolved = 0
    let ingested = 0
    let skipped = 0
    let notFound = 0
    let failed = 0
    let peopleLinked = 0
    for (const row of rows) {
      if (row.orgRecordId) resolved += 1
      if (row.status === 'done') {
        ingested += 1
        peopleLinked += row.people ?? 0
      } else if (row.status === 'already-ingested') skipped += 1
      else if (row.status === 'not-found') notFound += 1
      else if (row.status === 'failed') failed += 1
    }
    return { resolved, ingested, skipped, notFound, failed, peopleLinked }
  }, [rows])

  return {
    phase,
    rows,
    domainCount: rows.length,
    skipIfIngested,
    setSkipIfIngested,
    resumed,
    setDomains,
    resolve: runResolve,
    startIngest: runIngest,
    reset,
    readyCount: readyRows.length,
    estimatedPeople,
    estimateHasUnknowns,
    resolvedCompanies,
    summary,
  }
}
