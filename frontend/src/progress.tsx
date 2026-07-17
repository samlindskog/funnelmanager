import CloseIcon from '@mui/icons-material/Close'
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

type Track = { done: number; total: number; complete: boolean }

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

type Aggregate = { visible: boolean; percent: number | null }

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
  for (const run of runs.values()) {
    const track = kind === 'ingest' ? run.ingest : run.embed
    if (!track || track.complete) continue
    pending += 1
    done += Math.max(0, Math.min(track.done, track.total || track.done))
    total += Math.max(0, track.total)
  }
  if (pending === 0) return { visible: false, percent: null }
  // Indeterminate until we know a total and have made real progress; this avoids
  // a ring stuck at a determinate 0% while work is still queued upstream.
  if (total <= 0 || done <= 0) return { visible: true, percent: null }
  return { visible: true, percent: Math.min(100, Math.round((done / total) * 100)) }
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

  const items: { kind: ProgressKind; percent: number | null }[] = []
  if (ingest.visible) items.push({ kind: 'ingest', percent: ingest.percent })
  if (embedding.visible) items.push({ kind: 'embedding', percent: embedding.percent })
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
      {items.map(({ kind, percent }) => {
        const isEmbedding = kind === 'embedding'
        const label = isEmbedding ? 'Embedding' : 'Fetching'
        const ringColor = isEmbedding ? 'warning.main' : 'primary.main'
        return (
          <Tooltip key={kind} title={label} placement="left" describeChild>
            <Box
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
                transition: 'opacity 0.2s ease, transform 0.2s ease',
                '&:hover .progress-cancel': {
                  opacity: 1,
                  pointerEvents: 'auto',
                },
              }}
              aria-label={percent != null ? `${label} ${percent}%` : label}
            >
              <CircularProgress
                size={36}
                variant={percent == null ? 'indeterminate' : 'determinate'}
                value={percent ?? undefined}
                sx={{ color: ringColor }}
              />
              {percent != null && (
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
