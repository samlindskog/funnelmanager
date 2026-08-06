import BlockIcon from '@mui/icons-material/Block'
import CheckCircleOutlineIcon from '@mui/icons-material/CheckCircleOutlined'
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutlined'
import EditOutlinedIcon from '@mui/icons-material/EditOutlined'
import InsightsOutlinedIcon from '@mui/icons-material/InsightsOutlined'
import SendIcon from '@mui/icons-material/Send'
import WarningAmberIcon from '@mui/icons-material/WarningAmber'
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  IconButton,
  MenuItem,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  decideApproval,
  deleteSession,
  fetchSession,
  fetchStats,
  postMessage,
  reattachStream,
  renameSession,
} from './api'
import { readNdjson } from './stream'
import {
  attribution,
  errorMessage,
  formatEstimate,
  isActive,
  STATUS_COLOR,
} from './status'
import { UsagePanel } from './UsagePanel'
import {
  applyEvent,
  approvalFromEvent,
  cumulativeUsage,
  lastTurnUsage,
  mergeUsage,
  messagesToTranscript,
  type TranscriptItem,
} from './transcript'
import type {
  ApprovalDecision,
  ModelInfo,
  PendingApproval,
  SessionDetail,
  SessionSummary,
  StatsResponse,
  TurnEvent,
  UsageStat,
  User,
} from './types'

type Notify = (text: string, severity: 'success' | 'error') => void

// --- transcript item rendering ---------------------------------------------

function jsonPreview(value: unknown): string {
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function ToolBlock({ item }: { item: Extract<TranscriptItem, { kind: 'tool_call' | 'tool_result' }> }) {
  const isCall = item.kind === 'tool_call'
  const body = isCall ? jsonPreview((item as { args: unknown }).args) : jsonPreview((item as { content: unknown }).content)
  return (
    <Box
      data-testid={`agents-transcript-${item.kind}`}
      sx={{
        alignSelf: 'flex-start',
        maxWidth: '85%',
        border: 1,
        borderColor: 'divider',
        borderRadius: 1.5,
        overflow: 'hidden',
        bgcolor: 'action.hover',
      }}
    >
      <Box sx={{ px: 1.25, py: 0.5, display: 'flex', gap: 1, alignItems: 'center' }}>
        <Chip
          size="small"
          label={isCall ? 'tool call' : 'tool result'}
          color={isCall ? 'secondary' : 'default'}
          variant="outlined"
        />
        <Typography variant="caption" sx={{ fontFamily: 'monospace', fontWeight: 600 }}>
          {item.name || '—'}
        </Typography>
      </Box>
      <Box
        component="pre"
        sx={{
          m: 0,
          px: 1.25,
          py: 1,
          maxHeight: 220,
          overflow: 'auto',
          fontFamily: 'monospace',
          fontSize: 12,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
          borderTop: 1,
          borderColor: 'divider',
        }}
      >
        {body}
      </Box>
    </Box>
  )
}

function TranscriptRow({ item }: { item: TranscriptItem }) {
  switch (item.kind) {
    case 'user':
      return (
        <Box
          data-testid="agents-transcript-user"
          sx={{
            alignSelf: 'flex-end',
            maxWidth: '85%',
            px: 1.5,
            py: 1,
            borderRadius: 2,
            bgcolor: 'primary.main',
            color: 'primary.contrastText',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          <Typography variant="body2">{item.text}</Typography>
        </Box>
      )
    case 'assistant':
      return (
        <Box
          data-testid="agents-transcript-assistant"
          sx={{
            alignSelf: 'flex-start',
            maxWidth: '85%',
            px: 1.5,
            py: 1,
            borderRadius: 2,
            bgcolor: 'action.selected',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          <Typography variant="body2">{item.text}</Typography>
        </Box>
      )
    case 'thinking':
      return (
        <Box
          data-testid="agents-transcript-thinking"
          sx={{ alignSelf: 'flex-start', maxWidth: '85%', px: 1.5, py: 0.5, opacity: 0.7 }}
        >
          <Typography variant="caption" sx={{ fontStyle: 'italic', whiteSpace: 'pre-wrap' }}>
            {item.text}
          </Typography>
        </Box>
      )
    case 'summary':
      return (
        <Box data-testid="agents-transcript-summary" sx={{ alignSelf: 'center', maxWidth: '90%', py: 0.5 }}>
          <Typography variant="caption" color="text.secondary" sx={{ fontStyle: 'italic' }}>
            {item.text}
          </Typography>
        </Box>
      )
    case 'error':
      return (
        <Alert data-testid="agents-transcript-error" severity="error" sx={{ alignSelf: 'stretch' }}>
          {item.text}
        </Alert>
      )
    case 'tool_call':
    case 'tool_result':
      return <ToolBlock item={item} />
    default:
      return null
  }
}

// --- approval card ----------------------------------------------------------

function ApprovalCard({
  approval,
  isOwner,
  ownerName,
  busy,
  onDecide,
}: {
  approval: PendingApproval
  isOwner: boolean
  ownerName: string
  busy: boolean
  onDecide: (approval: PendingApproval, decision: ApprovalDecision) => void
}) {
  return (
    <Box
      data-testid="agents-approval-card"
      sx={{ alignSelf: 'stretch', border: 2, borderColor: 'warning.main', borderRadius: 1.5, overflow: 'hidden' }}
    >
      <Box
        sx={{ display: 'flex', alignItems: 'center', gap: 1, px: 1.5, py: 1, bgcolor: 'warning.main', color: 'warning.contrastText' }}
      >
        <WarningAmberIcon fontSize="small" />
        <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
          Approval required to continue
        </Typography>
      </Box>
      <Box sx={{ p: 1.5, bgcolor: 'background.paper' }}>
        <Typography variant="body2" sx={{ mb: 1 }}>
          The agent wants to run an over-threshold action it cannot self-confirm.
          {approval.message ? ` ${approval.message}` : ''}
        </Typography>
        <Stack spacing={0.5} sx={{ mb: 1.5 }}>
          <Typography variant="caption" color="text.secondary">
            Action: <strong>{approval.action || '—'}</strong>
          </Typography>
          <Typography variant="caption" color="text.secondary">
            Estimate: <strong>{formatEstimate(approval)}</strong>
          </Typography>
          {approval.tool_name && (
            <Typography variant="caption" color="text.secondary">
              Tool: <strong>{approval.tool_name}</strong>
            </Typography>
          )}
        </Stack>
        {isOwner ? (
          <Stack direction="row" spacing={1}>
            <Button
              data-testid="agents-approve"
              variant="contained"
              color="success"
              size="small"
              startIcon={busy ? <CircularProgress size={14} color="inherit" /> : <CheckCircleOutlineIcon />}
              disabled={busy}
              onClick={() => onDecide(approval, 'approve')}
            >
              Approve
            </Button>
            <Button
              data-testid="agents-reject"
              variant="outlined"
              color="error"
              size="small"
              startIcon={busy ? <CircularProgress size={14} color="inherit" /> : <BlockIcon />}
              disabled={busy}
              onClick={() => onDecide(approval, 'reject')}
            >
              Reject
            </Button>
          </Stack>
        ) : (
          <Typography variant="caption" color="text.secondary">
            Only <strong>{ownerName}</strong>, who started this session, can approve or reject this action.
          </Typography>
        )}
      </Box>
    </Box>
  )
}

// --- chat view --------------------------------------------------------------

export function ChatView({
  user,
  session,
  models,
  onRenamed,
  onDeleted,
  notify,
}: {
  user: User
  session: SessionSummary
  models: ModelInfo[]
  onRenamed: (session: SessionSummary) => void
  onDeleted: (sessionId: string) => void
  notify: Notify
}) {
  const sessionId = session.id
  const [detail, setDetail] = useState<SessionDetail | null>(null)
  const [live, setLive] = useState<TranscriptItem[]>([])
  const [pending, setPending] = useState<PendingApproval[]>([])
  const [streaming, setStreaming] = useState(false)
  const [turnError, setTurnError] = useState(false)
  const [input, setInput] = useState('')
  const [deciding, setDeciding] = useState<string | null>(null)

  const [lastUsage, setLastUsage] = useState<UsageStat | null>(null)
  const [liveCumulative, setLiveCumulative] = useState<UsageStat>({})
  const [usageOpen, setUsageOpen] = useState(false)
  const [stats, setStats] = useState<StatsResponse | null>(null)

  const [renameOpen, setRenameOpen] = useState(false)
  const [renameText, setRenameText] = useState('')
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [busy, setBusy] = useState(false)

  const abortRef = useRef<AbortController | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)

  const owner = detail?.owner ?? session.owner
  const isOwner = user.username === owner
  const status = detail?.status ?? session.status
  const model = detail?.model ?? session.model

  // The header chip tracks LIVE turn state (matching the transcript), not just
  // the status captured at fetch: running while a stream is active, error on a
  // failed turn, paused while an approval blocks the turn, else the derived
  // status (which turn_complete falls back to, since streaming/turnError clear).
  const displayStatus = streaming
    ? 'running'
    : turnError
      ? 'error'
      : pending.length > 0
        ? 'paused'
        : status

  const consume = useCallback(
    async (response: Response, abort: AbortController) => {
      setStreaming(true)
      setTurnError(false)
      try {
        for await (const raw of readNdjson(response)) {
          if (abort.signal.aborted) break
          const event = raw as TurnEvent
          if (event.type === 'idle') continue
          if (event.type === 'approval_required') {
            const card = approvalFromEvent(event as Extract<TurnEvent, { type: 'approval_required' }>, sessionId)
            setPending((prev) => (prev.some((p) => p.id === card.id) ? prev : [...prev, card]))
          } else if (event.type === 'usage') {
            setLastUsage(event as UsageStat)
          } else if (event.type === 'turn_complete') {
            const usage = (event as { usage?: UsageStat }).usage ?? null
            if (usage) {
              setLastUsage(usage)
              setLiveCumulative((prev) => mergeUsage(prev, usage))
            }
            setPending([])
          } else if (event.type === 'error') {
            // A turn that ends via an in-stream error resolves this turn's pending
            // approvals — never leave a stale card that would 409/404 on Approve.
            setLive((prev) => applyEvent(prev, event))
            setPending([])
            setTurnError(true)
          } else {
            setLive((prev) => applyEvent(prev, event))
          }
        }
      } catch (err) {
        if (!abort.signal.aborted) {
          // The turn ended by a thrown error mid-stream — same fail-closed cleanup:
          // surface the error line and drop any pending approval for the dead turn.
          setLive((prev) => applyEvent(prev, { type: 'error', detail: errorMessage(err) }))
          setPending([])
          setTurnError(true)
        }
      } finally {
        if (!abort.signal.aborted) setStreaming(false)
      }
    },
    [sessionId],
  )

  // Load the session on open; reattach to an in-flight turn (running/paused).
  useEffect(() => {
    let cancelled = false
    const abort = new AbortController()
    abortRef.current?.abort()
    abortRef.current = abort
    setDetail(null)
    setLive([])
    setPending([])
    setLastUsage(null)
    setLiveCumulative({})
    setStreaming(false)
    setTurnError(false)
    setInput('')
    ;(async () => {
      try {
        const loaded = await fetchSession(sessionId)
        if (cancelled) return
        setDetail(loaded)
        setPending(loaded.pending_approvals)
        setLastUsage(lastTurnUsage(loaded.messages))
        // A running/paused session has a live turn buffered — replay then live.
        if (loaded.status === 'running' || loaded.status === 'paused') {
          const response = await reattachStream(sessionId, abort.signal)
          if (cancelled) return
          await consume(response, abort)
        }
      } catch (err) {
        if (!cancelled) notify(errorMessage(err), 'error')
      }
    })()
    return () => {
      cancelled = true
      abort.abort()
    }
  }, [sessionId, consume, notify])

  // Auto-scroll to the newest transcript item.
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
  }, [live, pending, detail])

  const history = useMemo(() => (detail ? messagesToTranscript(detail.messages) : []), [detail])
  const sessionCumulative = useMemo(
    () => mergeUsage(cumulativeUsage(detail?.messages ?? []), liveCumulative),
    [detail, liveCumulative],
  )

  const handleSend = async () => {
    const content = input.trim()
    if (!content || streaming || !isOwner) return
    const abort = new AbortController()
    abortRef.current?.abort() // cancel any prior in-flight turn
    abortRef.current = abort
    setInput('')
    // A new turn supersedes any prior turn's pending approval — clear stale cards
    // so the owner can't Approve an action whose turn no longer exists (409/404).
    setPending([])
    try {
      const response = await postMessage(sessionId, content, abort.signal)
      await consume(response, abort)
    } catch (err) {
      notify(errorMessage(err), 'error')
    }
  }

  const handleDecision = async (approval: PendingApproval, decision: ApprovalDecision) => {
    setDeciding(approval.id)
    try {
      await decideApproval(sessionId, approval.id, decision)
      setPending((prev) => prev.filter((p) => p.id !== approval.id))
      notify(
        decision === 'approve' ? 'Approved — the turn is resuming the action' : 'Rejected — the action will be skipped',
        'success',
      )
    } catch (err) {
      notify(errorMessage(err), 'error')
    } finally {
      setDeciding(null)
    }
  }

  const openUsage = async () => {
    setUsageOpen(true)
    try {
      setStats(await fetchStats())
    } catch {
      /* the panel shows the session totals regardless */
    }
  }

  const handleRename = async () => {
    const title = renameText.trim()
    if (!title) return
    setBusy(true)
    try {
      const updated = await renameSession(sessionId, title)
      setDetail((current) => (current ? { ...current, title: updated.title } : current))
      onRenamed(updated)
      setRenameOpen(false)
      notify('Session renamed', 'success')
    } catch (err) {
      notify(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  const handleDelete = async () => {
    setBusy(true)
    try {
      await deleteSession(sessionId)
      setDeleteOpen(false)
      onDeleted(sessionId)
      notify('Session deleted', 'success')
    } catch (err) {
      notify(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  const items = [...history, ...live]
  const composerDisabled = streaming || !isOwner

  return (
    <Box sx={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0, minHeight: 0, bgcolor: 'background.paper' }}>
      {/* Header */}
      <Stack
        direction="row"
        spacing={1}
        sx={{ alignItems: 'center', px: 2, py: 1.25, borderBottom: 1, borderColor: 'divider' }}
      >
        <Box sx={{ minWidth: 0, flex: 1 }}>
          <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
            <Typography variant="subtitle1" noWrap sx={{ minWidth: 0 }}>
              {detail?.title ?? session.title}
            </Typography>
            <Chip
              data-testid="agents-session-status"
              label={displayStatus}
              size="small"
              color={STATUS_COLOR[displayStatus] ?? 'default'}
              variant="outlined"
            />
          </Stack>
          <Typography variant="caption" color="text.secondary">
            {attribution({ owner, origin: detail?.origin ?? session.origin })}
            {!isOwner ? ' · read-only (owned by another user)' : ''}
          </Typography>
        </Box>

        <TextField
          select
          size="small"
          label="Model"
          data-testid="agents-model-select"
          value={model}
          disabled
          helperText="Fixed per session"
          sx={{ width: 200, '& .MuiFormHelperText-root': { m: 0, mt: 0.25 } }}
        >
          {(models.some((m) => m.id === model) ? models : [{ id: model, context_window: 0, owned_by: '' }, ...models]).map(
            (m) => (
              <MenuItem key={m.id} value={m.id} data-testid={`agents-model-option-${m.id}`}>
                {m.id}
              </MenuItem>
            ),
          )}
        </TextField>

        <Tooltip title="Token usage">
          <IconButton data-testid="agents-usage-open" size="small" onClick={() => void openUsage()}>
            <InsightsOutlinedIcon fontSize="small" />
          </IconButton>
        </Tooltip>
        <Tooltip title={isOwner ? 'Rename session' : `Owned by ${owner}`}>
          <span>
            <IconButton
              data-testid="agents-rename-open"
              size="small"
              disabled={!isOwner}
              onClick={() => {
                setRenameText(detail?.title ?? session.title)
                setRenameOpen(true)
              }}
            >
              <EditOutlinedIcon fontSize="small" />
            </IconButton>
          </span>
        </Tooltip>
        <Tooltip title={isOwner ? 'Delete session' : `Owned by ${owner}`}>
          <span>
            <IconButton
              data-testid="agents-delete-open"
              size="small"
              color="error"
              disabled={!isOwner}
              onClick={() => setDeleteOpen(true)}
            >
              <DeleteOutlineIcon fontSize="small" />
            </IconButton>
          </span>
        </Tooltip>
      </Stack>

      {/* Transcript */}
      <Box ref={scrollRef} sx={{ flex: 1, overflowY: 'auto', px: 2, py: 2 }}>
        {detail == null ? (
          <Box sx={{ display: 'grid', placeItems: 'center', py: 6 }}>
            <CircularProgress size={28} />
          </Box>
        ) : (
          <Stack spacing={1.25} data-testid="agents-transcript">
            {items.length === 0 && pending.length === 0 && (
              <Typography color="text.secondary" sx={{ textAlign: 'center', py: 4 }}>
                No messages yet. Send one to start the conversation.
              </Typography>
            )}
            {items.map((item) => (
              <TranscriptRow key={item.key} item={item} />
            ))}
            {pending.map((approval) => (
              <ApprovalCard
                key={approval.id}
                approval={approval}
                isOwner={isOwner}
                ownerName={owner}
                busy={deciding === approval.id}
                onDecide={handleDecision}
              />
            ))}
            {streaming && (
              <Stack direction="row" spacing={1} sx={{ alignItems: 'center', alignSelf: 'flex-start', px: 1, opacity: 0.7 }}>
                <CircularProgress size={14} />
                <Typography variant="caption">The agent is working…</Typography>
              </Stack>
            )}
          </Stack>
        )}
      </Box>

      {/* Composer */}
      <Box sx={{ borderTop: 1, borderColor: 'divider', p: 1.5 }}>
        {detail?.last_error && !isActive(status) && (
          <Alert severity="error" sx={{ mb: 1 }} data-testid="agents-session-error">
            {detail.last_error}
          </Alert>
        )}
        <Stack direction="row" spacing={1} sx={{ alignItems: 'flex-end' }}>
          <TextField
            data-testid="agents-composer-input"
            placeholder={isOwner ? 'Message the agent…' : 'Read-only — this session belongs to another user'}
            multiline
            maxRows={6}
            fullWidth
            size="small"
            value={input}
            disabled={composerDisabled}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault()
                void handleSend()
              }
            }}
          />
          <Button
            data-testid="agents-composer-send"
            variant="contained"
            endIcon={streaming ? <CircularProgress size={16} color="inherit" /> : <SendIcon />}
            disabled={composerDisabled || input.trim().length === 0}
            onClick={() => void handleSend()}
          >
            Send
          </Button>
        </Stack>
      </Box>

      <UsagePanel
        open={usageOpen}
        onClose={() => setUsageOpen(false)}
        sessionUsage={sessionCumulative}
        lastUsage={lastUsage}
        stats={stats}
      />

      {/* Rename dialog (owner-only) */}
      <Dialog open={renameOpen} onClose={() => setRenameOpen(false)} maxWidth="xs" fullWidth>
        <DialogTitle>Rename session</DialogTitle>
        <DialogContent>
          <TextField
            data-testid="agents-rename-input"
            autoFocus
            fullWidth
            margin="dense"
            value={renameText}
            onChange={(event) => setRenameText(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') void handleRename()
            }}
          />
        </DialogContent>
        <DialogActions>
          <Button data-testid="agents-rename-cancel" onClick={() => setRenameOpen(false)}>
            Cancel
          </Button>
          <Button
            data-testid="agents-rename-save"
            variant="contained"
            disabled={busy || renameText.trim().length === 0}
            onClick={() => void handleRename()}
          >
            Save
          </Button>
        </DialogActions>
      </Dialog>

      {/* Delete confirm (owner-only) */}
      <Dialog open={deleteOpen} onClose={() => setDeleteOpen(false)} maxWidth="xs" fullWidth>
        <DialogTitle>Delete session?</DialogTitle>
        <DialogContent>
          <DialogContentText>
            This permanently deletes “{detail?.title ?? session.title}” and its transcript. This cannot be undone.
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button data-testid="agents-delete-cancel" onClick={() => setDeleteOpen(false)}>
            Cancel
          </Button>
          <Button
            data-testid="agents-delete-confirm"
            variant="contained"
            color="error"
            disabled={busy}
            onClick={() => void handleDelete()}
          >
            Delete
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  )
}
