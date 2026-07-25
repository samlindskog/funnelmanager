import BlockIcon from '@mui/icons-material/Block'
import CheckCircleOutlineIcon from '@mui/icons-material/CheckCircleOutlined'
import LogoutIcon from '@mui/icons-material/Logout'
import PersonSearchIcon from '@mui/icons-material/PersonSearch'
import RefreshIcon from '@mui/icons-material/Refresh'
import SendIcon from '@mui/icons-material/Send'
import SmartToyOutlinedIcon from '@mui/icons-material/SmartToyOutlined'
import WarningAmberIcon from '@mui/icons-material/WarningAmber'
import {
  Alert,
  AppBar,
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  IconButton,
  InputAdornment,
  LinearProgress,
  List,
  ListItemButton,
  ListItemText,
  MenuItem,
  Snackbar,
  Stack,
  TextField,
  Toolbar,
  Tooltip,
  Typography,
} from '@mui/material'
import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, createTask, decideApproval, fetchTask, fetchTasks, logout } from './api'
import { ColorModeToggle } from './ColorModeToggle'
import type { ApprovalDecision, PendingApproval, TaskDetail, TaskSummary, User } from './types'

const LIST_LIMIT = 100
const LIST_REFRESH_MS = 4000
const DETAIL_REFRESH_MS = 2000

// Lifecycle words shared with the agents backend (fm_runtime.JobStatus).
const STATUS_OPTIONS = ['queued', 'running', 'paused', 'completed', 'failed', 'canceled'] as const
const ACTIVE = new Set(['queued', 'running', 'paused'])

type ChipColor = 'default' | 'info' | 'warning' | 'success' | 'error'
const STATUS_COLOR: Record<string, ChipColor> = {
  queued: 'default',
  running: 'info',
  paused: 'warning',
  completed: 'success',
  failed: 'error',
  canceled: 'default',
}

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message
  if (err instanceof Error) return err.message
  return 'Something went wrong'
}

/** "alice" or "alice (via agent)" — attribution per principle 1. */
function attribution(run: TaskSummary): string {
  return run.origin === 'agent' ? `${run.owner} (via agent)` : run.owner
}

function formatDate(iso: string | null): string {
  if (!iso) return ''
  const date = new Date(iso)
  const now = new Date()
  const sameDay = date.toDateString() === now.toDateString()
  return sameDay
    ? date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    : date.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })
}

function progressPercent(run: TaskSummary): number | null {
  if (run.progress == null) return null
  return Math.round(Math.max(0, Math.min(1, run.progress)) * 100)
}

/** Trim a resource estimate to a readable number (integers stay whole). */
function fmtNum(value: number): string {
  return Number.isInteger(value) ? String(value) : String(Math.round(value * 100) / 100)
}

/** "3.4 GB (threshold 2 GB)" — the over-threshold estimate that tripped the gate. */
function formatEstimate(approval: PendingApproval): string {
  const unit = approval.unit ? ` ${approval.unit}` : ''
  const estimate = `${fmtNum(approval.estimate)}${unit}`
  if (approval.threshold != null) return `${estimate} (threshold ${fmtNum(approval.threshold)}${unit})`
  return estimate
}

function StatusChip({ status }: { status: string }) {
  return <Chip label={status} size="small" color={STATUS_COLOR[status] ?? 'default'} variant="outlined" />
}

export function AgentsApp({ user }: { user: User }) {
  const [goal, setGoal] = useState('')
  const [paramsText, setParamsText] = useState('')
  const [starting, setStarting] = useState(false)

  const [tasks, setTasks] = useState<TaskSummary[] | null>(null)
  const [ownerFilter, setOwnerFilter] = useState('')
  const [ownerInput, setOwnerInput] = useState('')
  const [statusFilter, setStatusFilter] = useState('')

  const [openTask, setOpenTask] = useState<TaskDetail | null>(null)
  // The approval id currently being approved/rejected (buttons show a spinner).
  const [deciding, setDeciding] = useState<string | null>(null)
  const [notice, setNotice] = useState<{ text: string; severity: 'success' | 'error' } | null>(null)

  // Latest filters, read by the interval poller without re-arming it.
  const filtersRef = useRef({ owner: '', status: '' })
  filtersRef.current = { owner: ownerFilter, status: statusFilter }

  const refreshTasks = useCallback(async (silent = false) => {
    try {
      const { owner, status } = filtersRef.current
      const data = await fetchTasks({
        owner: owner || undefined,
        status: status || undefined,
        limit: LIST_LIMIT,
      })
      setTasks(data.tasks)
    } catch (err) {
      if (!silent) setNotice({ text: errorMessage(err), severity: 'error' })
    }
  }, [])

  // Reload immediately when a filter changes; poll quietly otherwise so live
  // runs advance without flicker.
  useEffect(() => {
    void refreshTasks()
  }, [refreshTasks, ownerFilter, statusFilter])

  useEffect(() => {
    const timer = window.setInterval(() => void refreshTasks(true), LIST_REFRESH_MS)
    return () => window.clearInterval(timer)
  }, [refreshTasks])

  // Poll the open run while it is still active so progress/result stream in.
  useEffect(() => {
    if (!openTask || !ACTIVE.has(openTask.status)) return
    const id = openTask.id
    const timer = window.setInterval(async () => {
      try {
        const fresh = await fetchTask(id)
        setOpenTask((current) => (current && current.id === id ? fresh : current))
      } catch {
        /* transient; next tick retries */
      }
    }, DETAIL_REFRESH_MS)
    return () => window.clearInterval(timer)
  }, [openTask])

  const handleStart = async () => {
    const trimmed = goal.trim()
    if (!trimmed) return
    let params: Record<string, unknown> = {}
    const rawParams = paramsText.trim()
    if (rawParams) {
      try {
        const parsed = JSON.parse(rawParams)
        if (parsed == null || typeof parsed !== 'object' || Array.isArray(parsed)) {
          throw new Error('Params must be a JSON object')
        }
        params = parsed as Record<string, unknown>
      } catch (err) {
        setNotice({ text: `Invalid params JSON: ${errorMessage(err)}`, severity: 'error' })
        return
      }
    }
    setStarting(true)
    try {
      const created = await createTask({ goal: trimmed, params })
      setGoal('')
      setParamsText('')
      setNotice({ text: 'Task started', severity: 'success' })
      setOpenTask(created)
      void refreshTasks(true)
    } catch (err) {
      setNotice({ text: errorMessage(err), severity: 'error' })
    } finally {
      setStarting(false)
    }
  }

  const openDetail = async (summary: TaskSummary) => {
    // Show the summary immediately, then hydrate the full detail.
    setOpenTask({
      ...summary,
      params: {},
      result: null,
      error: null,
      steps: 0,
      usage: null,
      pending_approvals: [],
    })
    try {
      setOpenTask(await fetchTask(summary.id))
    } catch (err) {
      setNotice({ text: errorMessage(err), severity: 'error' })
    }
  }

  // Approve/reject a Principle-4 pending approval. Only the initiating human (the
  // run owner) sees these controls; the server is the real gate and rejects
  // anyone else. On success the run resumes (approve) or the action is skipped
  // (reject); we re-hydrate the detail so the pending list reflects the outcome.
  const handleDecision = async (runId: string, approval: PendingApproval, decision: ApprovalDecision) => {
    setDeciding(approval.id)
    try {
      const outcome = await decideApproval(runId, approval.id, decision)
      setNotice({
        text:
          decision === 'approve'
            ? 'Approved — the run is resuming the action'
            : 'Rejected — the action will be skipped',
        severity: 'success',
      })
      setOpenTask((current) =>
        current && current.id === runId ? { ...current, status: outcome.run_status } : current,
      )
      try {
        const fresh = await fetchTask(runId)
        setOpenTask((current) => (current && current.id === runId ? fresh : current))
      } catch {
        /* the notice already reflects the outcome; poll will reconcile */
      }
      void refreshTasks(true)
    } catch (err) {
      // Conflicts (already decided, no longer waiting, single-use ref spent) are
      // reported verbatim; re-hydrate so the UI shows the true current state.
      setNotice({ text: errorMessage(err), severity: 'error' })
      try {
        const fresh = await fetchTask(runId)
        setOpenTask((current) => (current && current.id === runId ? fresh : current))
      } catch {
        /* ignore */
      }
    } finally {
      setDeciding(null)
    }
  }

  const applyOwnerFilter = () => setOwnerFilter(ownerInput.trim())

  return (
    <Box sx={{ height: '100dvh', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      <AppBar position="static" elevation={0} color="transparent">
        <Toolbar sx={{ gap: 2 }}>
          <Box>
            <Typography variant="overline" sx={{ lineHeight: 1, display: 'block' }}>
              Funnel Manager
            </Typography>
            <Typography variant="h6" sx={{ lineHeight: 1.2 }}>
              Agents
            </Typography>
          </Box>
          <Divider orientation="vertical" flexItem sx={{ my: 1.5 }} />
          <Button component="a" href="/" color="inherit" size="small">
            Hub
          </Button>
          <Box sx={{ flex: 1 }} />
          <Typography variant="body2" color="text.secondary">
            {user.username}
          </Typography>
          <ColorModeToggle />
          <Button color="inherit" size="small" startIcon={<LogoutIcon />} onClick={() => void logout()}>
            Log out
          </Button>
        </Toolbar>
      </AppBar>
      <Divider />

      <Box sx={{ flex: 1, display: 'flex', minHeight: 0, overflow: 'hidden' }}>
        {/* Start-a-task panel */}
        <Box
          sx={{
            width: 380,
            flexShrink: 0,
            borderRight: 1,
            borderColor: 'divider',
            display: 'flex',
            flexDirection: 'column',
            minHeight: 0,
          }}
        >
          <Stack spacing={2} sx={{ p: 2, overflowY: 'auto' }}>
            <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
              <SmartToyOutlinedIcon fontSize="small" color="primary" />
              <Typography variant="subtitle1">Start a task</Typography>
            </Stack>
            <Typography variant="body2" color="text.secondary">
              Describe a goal. A runtime AI agent pursues it through the shared tools and
              records its work as <em>{user.username} (via agent)</em>.
            </Typography>
            <TextField
              label="Goal"
              placeholder="e.g. Find 50 VP Engineering leads at Series B fintech companies and enrich their emails"
              multiline
              minRows={5}
              value={goal}
              onChange={(event) => setGoal(event.target.value)}
              disabled={starting}
              fullWidth
            />
            <TextField
              label="Params (optional JSON)"
              placeholder={'{ "per_page": 25 }'}
              multiline
              minRows={2}
              value={paramsText}
              onChange={(event) => setParamsText(event.target.value)}
              disabled={starting}
              fullWidth
              slotProps={{ input: { sx: { fontFamily: 'monospace', fontSize: 13 } } }}
            />
            <Button
              variant="contained"
              startIcon={starting ? <CircularProgress size={16} /> : <SendIcon />}
              disabled={starting || goal.trim().length === 0}
              onClick={() => void handleStart()}
            >
              Start task
            </Button>
          </Stack>
        </Box>

        {/* Runs list — cross-user (principle 1) */}
        <Box
          component="main"
          sx={{
            flex: 1,
            display: 'flex',
            flexDirection: 'column',
            minWidth: 0,
            minHeight: 0,
            bgcolor: 'background.paper',
          }}
        >
          <Stack
            direction="row"
            spacing={2}
            sx={{ alignItems: 'center', px: 2, py: 1.25, borderBottom: 1, borderColor: 'divider' }}
          >
            <Typography variant="subtitle1" sx={{ flexShrink: 0 }}>
              Runs
            </Typography>
            <Box sx={{ flex: 1 }} />
            <TextField
              size="small"
              placeholder="Filter by owner"
              value={ownerInput}
              onChange={(event) => setOwnerInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') applyOwnerFilter()
              }}
              onBlur={applyOwnerFilter}
              slotProps={{
                input: {
                  startAdornment: (
                    <InputAdornment position="start">
                      <PersonSearchIcon fontSize="small" />
                    </InputAdornment>
                  ),
                },
              }}
              sx={{ width: 220 }}
            />
            <TextField
              size="small"
              select
              label="Status"
              value={statusFilter}
              onChange={(event) => setStatusFilter(event.target.value)}
              sx={{ width: 140 }}
            >
              <MenuItem value="">All</MenuItem>
              {STATUS_OPTIONS.map((option) => (
                <MenuItem key={option} value={option}>
                  {option}
                </MenuItem>
              ))}
            </TextField>
            <Tooltip title="Refresh">
              <IconButton size="small" onClick={() => void refreshTasks()}>
                <RefreshIcon fontSize="small" />
              </IconButton>
            </Tooltip>
          </Stack>

          <Box sx={{ flex: 1, overflowY: 'auto' }}>
            {tasks == null ? (
              <Box sx={{ display: 'grid', placeItems: 'center', py: 6 }}>
                <CircularProgress size={28} />
              </Box>
            ) : tasks.length === 0 ? (
              <Typography color="text.secondary" sx={{ p: 3 }}>
                {ownerFilter || statusFilter
                  ? 'No runs match the filter.'
                  : 'No runs yet. Start a task to launch a runtime agent.'}
              </Typography>
            ) : (
              <List dense disablePadding>
                {tasks.map((run) => {
                  const percent = progressPercent(run)
                  const paused = run.status === 'paused'
                  return (
                    <ListItemButton
                      key={run.id}
                      divider
                      onClick={() => void openDetail(run)}
                      sx={{
                        alignItems: 'flex-start',
                        py: 1.25,
                        ...(paused && {
                          borderLeft: 3,
                          borderLeftColor: 'warning.main',
                          bgcolor: 'warning.main',
                          '&, &:hover': { bgcolor: (t) => `${t.palette.warning.main}14` },
                        }),
                      }}
                    >
                      <ListItemText
                        primary={
                          <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
                            <Typography variant="body2" sx={{ flex: 1, fontWeight: 500 }} noWrap>
                              {run.goal}
                            </Typography>
                            <StatusChip status={run.status} />
                            <Typography variant="caption" color="text.secondary" sx={{ flexShrink: 0 }}>
                              {formatDate(run.created_at)}
                            </Typography>
                          </Stack>
                        }
                        secondary={
                          <Stack component="span" spacing={0.5} sx={{ mt: 0.5 }}>
                            <Typography variant="caption" color="text.secondary" component="span">
                              {attribution(run)}
                            </Typography>
                            {paused && (
                              <Stack
                                component="span"
                                direction="row"
                                spacing={0.5}
                                sx={{ alignItems: 'center', color: 'warning.main' }}
                              >
                                <WarningAmberIcon sx={{ fontSize: 14 }} />
                                <Typography variant="caption" component="span" sx={{ fontWeight: 600 }}>
                                  Paused — may need your approval
                                </Typography>
                              </Stack>
                            )}
                            {ACTIVE.has(run.status) && (
                              <LinearProgress
                                variant={percent == null ? 'indeterminate' : 'determinate'}
                                value={percent ?? undefined}
                                sx={{ borderRadius: 1 }}
                              />
                            )}
                          </Stack>
                        }
                        disableTypography
                      />
                    </ListItemButton>
                  )
                })}
              </List>
            )}
          </Box>
        </Box>
      </Box>

      {/* Run detail */}
      <Dialog open={openTask != null} onClose={() => setOpenTask(null)} maxWidth="md" fullWidth>
        {openTask && (
          <>
            <DialogTitle sx={{ pb: 1 }}>
              <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
                <Box sx={{ flex: 1, minWidth: 0 }}>{openTask.goal}</Box>
                <StatusChip status={openTask.status} />
              </Stack>
              <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                {attribution(openTask)} · started {formatDate(openTask.started_at ?? openTask.created_at)}
                {openTask.ended_at ? ` · ended ${formatDate(openTask.ended_at)}` : ''}
              </Typography>
            </DialogTitle>
            <DialogContent dividers>
              {/* Principle-4 pending approvals — the run is blocked until the
                  initiating human decides. Rendered first so a waiting run is
                  impossible to miss. */}
              {openTask.pending_approvals.length > 0 && (
                <Stack spacing={1.5} sx={{ mb: 2.5 }}>
                  {openTask.pending_approvals.map((approval) => {
                    const isOwner = user.username === openTask.owner
                    const busy = deciding === approval.id
                    return (
                      <Box
                        key={approval.id}
                        sx={{
                          border: 2,
                          borderColor: 'warning.main',
                          borderRadius: 1.5,
                          bgcolor: 'warning.main',
                          p: 0,
                        }}
                      >
                        <Box
                          sx={{
                            display: 'flex',
                            alignItems: 'center',
                            gap: 1,
                            px: 1.5,
                            py: 1,
                            color: 'warning.contrastText',
                          }}
                        >
                          <WarningAmberIcon fontSize="small" />
                          <Typography variant="subtitle2" sx={{ fontWeight: 700, flex: 1 }}>
                            Approval required to continue
                          </Typography>
                        </Box>
                        <Box sx={{ bgcolor: 'background.paper', p: 1.5, borderBottomLeftRadius: 6, borderBottomRightRadius: 6 }}>
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
                                variant="contained"
                                color="success"
                                size="small"
                                startIcon={
                                  busy ? <CircularProgress size={14} color="inherit" /> : <CheckCircleOutlineIcon />
                                }
                                disabled={busy}
                                onClick={() => void handleDecision(openTask.id, approval, 'approve')}
                              >
                                Approve
                              </Button>
                              <Button
                                variant="outlined"
                                color="error"
                                size="small"
                                startIcon={busy ? <CircularProgress size={14} color="inherit" /> : <BlockIcon />}
                                disabled={busy}
                                onClick={() => void handleDecision(openTask.id, approval, 'reject')}
                              >
                                Reject
                              </Button>
                            </Stack>
                          ) : (
                            <Typography variant="caption" color="text.secondary">
                              Only <strong>{openTask.owner}</strong>, who started this run, can approve or reject
                              this action.
                            </Typography>
                          )}
                        </Box>
                      </Box>
                    )
                  })}
                </Stack>
              )}

              {ACTIVE.has(openTask.status) && (
                <LinearProgress
                  variant={progressPercent(openTask) == null ? 'indeterminate' : 'determinate'}
                  value={progressPercent(openTask) ?? undefined}
                  sx={{ mb: 2, borderRadius: 1 }}
                />
              )}
              <Stack direction="row" spacing={3} sx={{ mb: 2, flexWrap: 'wrap' }} useFlexGap>
                <Typography variant="caption" color="text.secondary">
                  Steps: <strong>{openTask.steps}</strong>
                </Typography>
                {progressPercent(openTask) != null && (
                  <Typography variant="caption" color="text.secondary">
                    Progress: <strong>{progressPercent(openTask)}%</strong>
                  </Typography>
                )}
                <Typography variant="caption" color="text.secondary">
                  Actor: <strong>{openTask.actor || '—'}</strong>
                </Typography>
              </Stack>

              {openTask.error && (
                <Alert severity="error" sx={{ mb: 2 }}>
                  {openTask.error}
                </Alert>
              )}

              {openTask.result && (
                <>
                  <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
                    Result
                  </Typography>
                  <Box
                    component="pre"
                    sx={{
                      m: 0,
                      mb: 2,
                      p: 1.5,
                      whiteSpace: 'pre-wrap',
                      wordBreak: 'break-word',
                      fontFamily: 'inherit',
                      fontSize: 14,
                      bgcolor: 'action.hover',
                      borderRadius: 1,
                    }}
                  >
                    {openTask.result}
                  </Box>
                </>
              )}

              {Object.keys(openTask.params).length > 0 && (
                <>
                  <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
                    Params
                  </Typography>
                  <Box
                    component="pre"
                    sx={{
                      m: 0,
                      mb: 2,
                      p: 1.5,
                      overflowX: 'auto',
                      fontFamily: 'monospace',
                      fontSize: 13,
                      bgcolor: 'action.hover',
                      borderRadius: 1,
                    }}
                  >
                    {JSON.stringify(openTask.params, null, 2)}
                  </Box>
                </>
              )}

              {openTask.usage && Object.keys(openTask.usage).length > 0 && (
                <>
                  <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
                    Usage
                  </Typography>
                  <Box
                    component="pre"
                    sx={{
                      m: 0,
                      p: 1.5,
                      overflowX: 'auto',
                      fontFamily: 'monospace',
                      fontSize: 13,
                      bgcolor: 'action.hover',
                      borderRadius: 1,
                    }}
                  >
                    {JSON.stringify(openTask.usage, null, 2)}
                  </Box>
                </>
              )}
            </DialogContent>
            <DialogActions>
              <Button onClick={() => setOpenTask(null)}>Close</Button>
            </DialogActions>
          </>
        )}
      </Dialog>

      <Snackbar
        open={notice != null}
        autoHideDuration={5000}
        onClose={() => setNotice(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
      >
        <Alert severity={notice?.severity ?? 'success'} variant="filled" onClose={() => setNotice(null)}>
          {notice?.text}
        </Alert>
      </Snackbar>
    </Box>
  )
}
