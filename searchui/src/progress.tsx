import CloseIcon from '@mui/icons-material/Close'
import InfoOutlinedIcon from '@mui/icons-material/InfoOutlined'
import PauseCircleOutlineIcon from '@mui/icons-material/PauseCircleOutlined'
import {
  Box,
  CircularProgress,
  IconButton,
  Stack,
  Tooltip,
  Typography,
} from '@mui/material'
import { alpha } from '@mui/material/styles'
import {
  createContext,
  useCallback,
  useContext,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { cancelStream } from './api'

export type ProgressKind = 'ingest' | 'embedding'

type Track = {
  done: number
  total: number
  complete: boolean
  /** ingest: Apollo fetching is paused waiting for embedding to catch up. */
  throttled?: boolean
  /** ingest: embedding was cancelled but fetching continues (sticky for the run). */
  embeddingDetached?: boolean
  /** embedding: rows that failed to embed/index (server-side running total). */
  failed?: number
  /** embedding: rows actually indexed (honest count; `done` counts attempts). */
  indexed?: number
}

type Run = {
  id: string
  ingest?: Track
  embed?: Track
  /**
   * Leads stream ids seen for this run, tracked per kind so cancelling one ring
   * never tears down the other (404s on cancel are ignored). Cancelling embedding
   * must not cancel the ingest stream, and vice versa.
   */
  ingestStreamIds: Set<string>
  embedStreamIds: Set<string>
  controller: AbortController
}

export type ProgressReport = {
  done?: number
  total?: number
  streamId?: string
  streamIds?: string[]
  complete?: boolean
  /** ingest only: set true on a `throttled` line; any later non-throttled ingest
   * report clears it. Carries no done/total (the ring must not advance). */
  throttled?: boolean
  /** ingest only: embedding cancelled while fetching continues — sticky, not cleared. */
  embeddingDetached?: boolean
  /** embedding only: server-side running failed / indexed counts. */
  failed?: number
  indexed?: number
}

/**
 * Handle for one search or enrichment run. Report ingest/embedding progress as
 * it streams in; the two global circles aggregate across every active run.
 * Always call `end()` (e.g. in a `finally`) so the run's contribution clears.
 */
export type ProgressRun = {
  id: string
  signal: AbortSignal
  reportIngest: (report: ProgressReport) => void
  reportEmbed: (report: ProgressReport) => void
  end: () => void
}

type Aggregate = {
  visible: boolean
  percent: number | null
  /** ingest: at least one active run is paused for backpressure. */
  throttled: boolean
  /** ingest: at least one active run had its embedding cancelled (still collecting). */
  embeddingDetached: boolean
  /** embedding: total failed / indexed across active runs. */
  failed: number
  indexed: number
}

type ProgressContextValue = {
  beginRun: () => ProgressRun
  cancel: (kind: ProgressKind) => Promise<void>
  ingest: Aggregate
  embedding: Aggregate
}

const ProgressContext = createContext<ProgressContextValue | null>(null)

function aggregate(runs: Map<string, Run>, kind: ProgressKind): Aggregate {
  let done = 0
  let total = 0
  let pending = 0
  let throttled = false
  let embeddingDetached = false
  let failed = 0
  let indexed = 0
  for (const run of runs.values()) {
    const track = kind === 'ingest' ? run.ingest : run.embed
    if (!track || track.complete) continue
    pending += 1
    done += Math.max(0, Math.min(track.done, track.total || track.done))
    total += Math.max(0, track.total)
    if (track.throttled) throttled = true
    if (track.embeddingDetached) embeddingDetached = true
    failed += Math.max(0, track.failed ?? 0)
    indexed += Math.max(0, track.indexed ?? 0)
  }
  if (pending === 0)
    return {
      visible: false,
      percent: null,
      throttled: false,
      embeddingDetached: false,
      failed: 0,
      indexed: 0,
    }
  // Indeterminate until we know a total and have made real progress; this avoids
  // a ring stuck at a determinate 0% while work is still queued upstream.
  if (total <= 0 || done <= 0)
    return { visible: true, percent: null, throttled, embeddingDetached, failed, indexed }
  return {
    visible: true,
    percent: Math.min(100, Math.round((done / total) * 100)),
    throttled,
    embeddingDetached,
    failed,
    indexed,
  }
}

export function ProgressProvider({ children }: { children: ReactNode }) {
  const runsRef = useRef<Map<string, Run>>(new Map())
  const [, setVersion] = useState(0)
  const bump = useCallback(() => setVersion((v) => v + 1), [])

  const beginRun = useCallback((): ProgressRun => {
    const id =
      typeof crypto !== 'undefined' && 'randomUUID' in crypto
        ? crypto.randomUUID()
        : `run-${Math.random().toString(36).slice(2)}-${Date.now()}`
    const run: Run = {
      id,
      ingestStreamIds: new Set<string>(),
      embedStreamIds: new Set<string>(),
      controller: new AbortController(),
    }
    runsRef.current.set(id, run)

    const apply = (key: 'ingest' | 'embed', report: ProgressReport) => {
      const current = runsRef.current.get(id)
      if (!current) return
      const streamIds = key === 'ingest' ? current.ingestStreamIds : current.embedStreamIds
      if (report.streamId) streamIds.add(report.streamId)
      report.streamIds?.forEach((streamId) => {
        if (streamId) streamIds.add(streamId)
      })
      const prev = current[key] ?? { done: 0, total: 0, complete: false }
      current[key] = {
        done: report.done ?? prev.done,
        total: report.total ?? prev.total,
        complete: report.complete ?? prev.complete,
        // A throttled ingest line sets the paused flag; any other ingest report
        // (progress/complete) clears it. Irrelevant for the embedding track.
        throttled: key === 'ingest' ? report.throttled === true : prev.throttled,
        // Embedding-detached is sticky once set — embedding stays cancelled.
        embeddingDetached:
          key === 'ingest'
            ? report.embeddingDetached || prev.embeddingDetached
            : prev.embeddingDetached,
        failed: report.failed ?? prev.failed,
        indexed: report.indexed ?? prev.indexed,
      }
      bump()
    }

    bump()
    return {
      id,
      signal: run.controller.signal,
      reportIngest: (report) => apply('ingest', report),
      reportEmbed: (report) => apply('embed', report),
      end: () => {
        if (runsRef.current.delete(id)) bump()
      },
    }
  }, [bump])

  const cancel = useCallback(
    async (kind: ProgressKind) => {
      const ids = new Set<string>()
      for (const run of [...runsRef.current.values()]) {
        const track = kind === 'ingest' ? run.ingest : run.embed
        if (!track || track.complete) continue
        if (kind === 'ingest') {
          // Cancelling fetching tears down the whole run: aborting the fetch stops
          // the browser read, and the backend disconnect handler cancels any
          // leftover streams. Cancel both id sets so the leads jobs stop promptly.
          run.ingestStreamIds.forEach((streamId) => ids.add(streamId))
          run.embedStreamIds.forEach((streamId) => ids.add(streamId))
          run.controller.abort()
          runsRef.current.delete(run.id)
        } else {
          // Cancel ONLY embedding streams; keep the fetch (ingest) running so the
          // ingest ring is unaffected.
          run.embedStreamIds.forEach((streamId) => ids.add(streamId))
          run.embed = undefined
          run.embedStreamIds = new Set<string>()
          if (!run.ingest) {
            run.controller.abort()
            runsRef.current.delete(run.id)
          }
        }
      }
      bump()
      await Promise.all(
        [...ids].map((streamId) => cancelStream(streamId).catch(() => undefined)),
      )
    },
    [bump],
  )

  const value: ProgressContextValue = {
    beginRun,
    cancel,
    ingest: aggregate(runsRef.current, 'ingest'),
    embedding: aggregate(runsRef.current, 'embedding'),
  }

  return <ProgressContext.Provider value={value}>{children}</ProgressContext.Provider>
}

export function useProgress(): ProgressContextValue {
  const ctx = useContext(ProgressContext)
  if (!ctx) throw new Error('useProgress must be used within a ProgressProvider')
  return ctx
}

/** Fixed, bottom-right loading rings that aggregate every active run. */
export function ProgressCircles() {
  const { ingest, embedding, cancel } = useProgress()

  const items: {
    kind: ProgressKind
    percent: number | null
    throttled: boolean
    embeddingDetached: boolean
    failed: number
    indexed: number
  }[] = []
  if (ingest.visible)
    items.push({
      kind: 'ingest',
      percent: ingest.percent,
      throttled: ingest.throttled,
      embeddingDetached: ingest.embeddingDetached,
      failed: 0,
      indexed: 0,
    })
  if (embedding.visible)
    items.push({
      kind: 'embedding',
      percent: embedding.percent,
      throttled: false,
      embeddingDetached: false,
      failed: embedding.failed,
      indexed: embedding.indexed,
    })
  if (!items.length) return null

  const onCancel = async (kind: ProgressKind) => {
    const message = kind === 'embedding' ? 'Cancel all embedding?' : 'Cancel all fetching?'
    if (!window.confirm(message)) return
    await cancel(kind)
  }

  return (
    <Stack
      spacing={1.25}
      sx={{
        position: 'fixed',
        right: 24,
        bottom: 24,
        zIndex: (t) => t.zIndex.snackbar,
        flexDirection: 'column-reverse',
        alignItems: 'center',
      }}
    >
      {items.map(({ kind, percent, throttled, embeddingDetached, failed, indexed }) => {
        const isEmbedding = kind === 'embedding'
        const label = isEmbedding ? 'Embedding' : 'Fetching'
        const hasFailures = isEmbedding && failed > 0
        // Embedding-cancelled (info) outranks throttled (paused): once embedding is
        // cancelled ingest is no longer waiting on it, so don't also read as paused.
        const isPaused = throttled && !embeddingDetached
        const ringColor = hasFailures ? 'error.main' : isEmbedding ? 'warning.main' : 'primary.main'
        // Throttled ingest keeps spinning determinately at its current % but reads
        // as "paused": dimmed, a pause glyph, and an explanatory tooltip. It clears
        // on the next non-throttled ingest event.
        const tooltip = embeddingDetached
          ? 'Embedding cancelled — leads still collecting; embeddings can be backfilled later'
          : isPaused
            ? 'Fetching paused — waiting for embedding to catch up'
            : hasFailures
              ? `Embedding — ${indexed.toLocaleString()} indexed, ${failed.toLocaleString()} failed`
              : label
        const ariaLabel = embeddingDetached
          ? 'Embedding cancelled, leads still collecting'
          : isPaused
            ? 'Fetching paused, waiting for embedding'
            : hasFailures
              ? `Embedding ${indexed} indexed, ${failed} failed`
              : percent != null
                ? `${label} ${percent}%`
                : label
        return (
          <Tooltip key={kind} title={tooltip} placement="left" describeChild>
            <Box
              data-testid={`progress-ring-${kind}`}
              data-throttled={isPaused ? 'true' : undefined}
              data-embedding-detached={embeddingDetached ? 'true' : undefined}
              data-failed={hasFailures ? String(failed) : undefined}
              sx={{
                position: 'relative',
                display: 'grid',
                placeItems: 'center',
                width: 56,
                height: 56,
                borderRadius: '50%',
                bgcolor: 'background.paper',
                border: '1px solid',
                borderColor: 'divider',
                boxShadow: 2,
                opacity: isPaused ? 0.68 : 1,
                transition: 'opacity 0.2s ease, transform 0.2s ease',
                '&:hover .progress-cancel': {
                  opacity: 1,
                  pointerEvents: 'auto',
                },
              }}
              aria-label={ariaLabel}
            >
              <CircularProgress
                size={36}
                variant={percent == null ? 'indeterminate' : 'determinate'}
                value={percent ?? undefined}
                sx={{ color: ringColor }}
              />
              {isPaused ? (
                <PauseCircleOutlineIcon
                  sx={{ position: 'absolute', fontSize: 18, color: 'text.secondary' }}
                />
              ) : (
                percent != null && (
                  <Typography
                    variant="caption"
                    sx={{
                      position: 'absolute',
                      fontSize: 10,
                      fontWeight: 700,
                      lineHeight: 1,
                      color: ringColor,
                    }}
                  >
                    {percent}%
                  </Typography>
                )
              )}
              {embeddingDetached && (
                <InfoOutlinedIcon
                  sx={{
                    position: 'absolute',
                    top: 2,
                    right: 2,
                    fontSize: 14,
                    color: 'info.main',
                  }}
                />
              )}
              <IconButton
                className="progress-cancel"
                size="small"
                aria-label={`Cancel ${label.toLowerCase()}`}
                onClick={() => void onCancel(kind)}
                sx={{
                  position: 'absolute',
                  inset: 0,
                  m: 'auto',
                  width: 36,
                  height: 36,
                  opacity: 0,
                  pointerEvents: 'none',
                  bgcolor: (t) => alpha(t.palette.background.paper, 0.92),
                  border: '1px solid',
                  borderColor: 'divider',
                  color: 'text.primary',
                  transition: 'opacity 0.15s ease',
                  '&:hover': {
                    bgcolor: (t) => alpha(t.palette.background.paper, 0.98),
                  },
                }}
              >
                <CloseIcon sx={{ fontSize: 18 }} />
              </IconButton>
            </Box>
          </Tooltip>
        )
      })}
    </Stack>
  )
}
