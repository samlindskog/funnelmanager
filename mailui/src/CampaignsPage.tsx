import AddIcon from '@mui/icons-material/Add'
import CampaignIcon from '@mui/icons-material/Campaign'
import CancelIcon from '@mui/icons-material/Cancel'
import PauseIcon from '@mui/icons-material/Pause'
import PlaylistAddIcon from '@mui/icons-material/PlaylistAdd'
import PlayArrowIcon from '@mui/icons-material/PlayArrow'
import RefreshIcon from '@mui/icons-material/Refresh'
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  IconButton,
  LinearProgress,
  List,
  ListItemButton,
  ListItemText,
  MenuItem,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  addCampaignSource,
  cancelCampaign,
  confirmationRequired,
  createCampaign,
  fetchCampaign,
  fetchCampaigns,
  fetchSavedSearches,
  fetchSearchRecipients,
  pauseCampaign,
  resumeCampaign,
  startCampaign,
} from './api'
import { ConfirmationDialog } from './ConfirmationDialog'
import type { Campaign, CampaignSourceIn, ConfirmationRequired, SavedSearch, User } from './types'

type Notify = (text: string, severity: 'success' | 'error') => void

const STRATEGIES = [
  { value: 'balanced', label: 'Balanced — spread evenly across your mailboxes/domains' },
  { value: 'sequential', label: 'Sequential — fill one mailbox before the next' },
] as const

/** A gated call awaiting the human confirm=true re-invoke (Principle 4). */
interface Pending {
  detail: ConfirmationRequired
  run: (confirmToken: string) => Promise<void>
}

function errorMessage(err: unknown): string {
  if (err instanceof Error) return err.message
  return 'Something went wrong'
}

function statusColor(status: string): 'default' | 'success' | 'warning' | 'error' | 'info' {
  switch (status) {
    case 'running':
      return 'success'
    case 'paused':
      return 'warning'
    case 'completed':
      return 'info'
    case 'cancelled':
      return 'error'
    default:
      return 'default'
  }
}

function formatDate(iso: string | null): string {
  if (!iso) return ''
  return new Date(iso).toLocaleString(undefined, {
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function CampaignsPage({ user, notify }: { user: User; notify: Notify }) {
  const [ownerFilter, setOwnerFilter] = useState<string>('') // '' = all users (Principle 1)
  const [campaigns, setCampaigns] = useState<Campaign[] | null>(null)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [detail, setDetail] = useState<Campaign | null>(null)
  const [busy, setBusy] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [continueOpen, setContinueOpen] = useState(false)
  const [pending, setPending] = useState<Pending | null>(null)

  const owners = useMemo(() => {
    const set = new Set<string>()
    for (const campaign of campaigns ?? []) if (campaign.owner) set.add(campaign.owner)
    return [...set].sort()
  }, [campaigns])

  const refreshList = useCallback(async () => {
    try {
      setCampaigns(await fetchCampaigns(ownerFilter || undefined))
    } catch (err) {
      notify(errorMessage(err), 'error')
    }
  }, [ownerFilter, notify])

  useEffect(() => {
    void refreshList()
  }, [refreshList])

  const loadDetail = useCallback(
    async (id: number) => {
      try {
        setDetail(await fetchCampaign(id))
      } catch (err) {
        notify(errorMessage(err), 'error')
      }
    },
    [notify],
  )

  useEffect(() => {
    if (selectedId != null) void loadDetail(selectedId)
    else setDetail(null)
  }, [selectedId, loadDetail])

  // Poll the open campaign while it is actively sending so progress advances.
  useEffect(() => {
    if (selectedId == null || detail?.status !== 'running') return
    const timer = window.setInterval(() => void loadDetail(selectedId), 4000)
    return () => window.clearInterval(timer)
  }, [selectedId, detail?.status, loadDetail])

  /**
   * Run a gated campaign action. On 409 confirmation_required, stash a Pending
   * that re-invokes with confirm=true + the echoed token; the dialog drives it.
   */
  const runGated = useCallback(
    async (call: (opts: { confirm?: boolean; confirmToken?: string }) => Promise<unknown>) => {
      setBusy(true)
      try {
        await call({})
        if (selectedId != null) await loadDetail(selectedId)
        await refreshList()
      } catch (err) {
        const confirmation = confirmationRequired(err)
        if (confirmation) {
          setPending({
            detail: confirmation,
            run: async (confirmToken) => {
              await call({ confirm: true, confirmToken })
              if (selectedId != null) await loadDetail(selectedId)
              await refreshList()
            },
          })
        } else {
          notify(errorMessage(err), 'error')
        }
      } finally {
        setBusy(false)
      }
    },
    [selectedId, loadDetail, refreshList, notify],
  )

  const handleSimple = async (fn: () => Promise<Campaign>) => {
    setBusy(true)
    try {
      await fn()
      if (selectedId != null) await loadDetail(selectedId)
      await refreshList()
    } catch (err) {
      notify(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  const confirmPending = async () => {
    if (!pending) return
    setBusy(true)
    try {
      await pending.run(pending.detail.confirm_token)
      setPending(null)
      if (selectedId != null) await loadDetail(selectedId)
      await refreshList()
    } catch (err) {
      notify(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Box sx={{ flex: 1, display: 'flex', minHeight: 0, overflow: 'hidden' }}>
      {/* Campaign list */}
      <Box
        sx={{
          width: 340,
          flexShrink: 0,
          borderRight: 1,
          borderColor: 'divider',
          display: 'flex',
          flexDirection: 'column',
          minHeight: 0,
        }}
      >
        <Stack spacing={1} sx={{ px: 2, py: 1.5 }}>
          <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
            <Typography variant="subtitle1" sx={{ flex: 1 }}>
              Campaigns
            </Typography>
            <Tooltip title="Refresh">
              <IconButton size="small" onClick={() => void refreshList()}>
                <RefreshIcon fontSize="small" />
              </IconButton>
            </Tooltip>
            <Button size="small" variant="contained" startIcon={<AddIcon />} onClick={() => setCreateOpen(true)}>
              New
            </Button>
          </Stack>
          <TextField
            select
            size="small"
            label="Owner"
            value={ownerFilter}
            onChange={(event) => setOwnerFilter(event.target.value)}
          >
            <MenuItem value="">All users</MenuItem>
            <MenuItem value={user.username}>Me ({user.username})</MenuItem>
            {owners
              .filter((owner) => owner !== user.username)
              .map((owner) => (
                <MenuItem key={owner} value={owner}>
                  {owner}
                </MenuItem>
              ))}
          </TextField>
        </Stack>
        <Divider />
        <Box sx={{ flex: 1, overflowY: 'auto' }}>
          {campaigns == null ? (
            <Box sx={{ display: 'grid', placeItems: 'center', py: 4 }}>
              <CircularProgress size={24} />
            </Box>
          ) : campaigns.length === 0 ? (
            <Typography variant="body2" color="text.secondary" sx={{ p: 2 }}>
              No campaigns yet. Create one from a saved search list.
            </Typography>
          ) : (
            <List dense disablePadding>
              {campaigns.map((campaign) => (
                <ListItemButton
                  key={campaign.id}
                  divider
                  selected={campaign.id === selectedId}
                  onClick={() => setSelectedId(campaign.id)}
                  sx={{ alignItems: 'flex-start', py: 1 }}
                >
                  <ListItemText
                    primary={
                      <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
                        <Typography variant="body2" sx={{ fontWeight: 600, flex: 1 }} noWrap>
                          {campaign.name || `Campaign #${campaign.id}`}
                        </Typography>
                        <Chip label={campaign.status} size="small" color={statusColor(campaign.status)} />
                      </Stack>
                    }
                    secondary={
                      <Typography variant="caption" color="text.secondary" component="span">
                        {campaign.owner}
                        {campaign.origin === 'agent' && ` (via agent${campaign.actor ? ` ${campaign.actor}` : ''})`}
                        {' · '}
                        {campaign.stats.sent}/{campaign.stats.recipients_total} sent
                      </Typography>
                    }
                    disableTypography
                  />
                </ListItemButton>
              ))}
            </List>
          )}
        </Box>
      </Box>

      {/* Campaign detail */}
      <Box component="main" sx={{ flex: 1, minWidth: 0, minHeight: 0, overflowY: 'auto', bgcolor: 'background.paper' }}>
        {detail == null ? (
          <Box sx={{ display: 'grid', placeItems: 'center', height: '100%' }}>
            <Stack spacing={1} sx={{ alignItems: 'center' }}>
              <CampaignIcon sx={{ fontSize: 48, color: 'text.disabled' }} />
              <Typography color="text.secondary">Select a campaign, or create one.</Typography>
            </Stack>
          </Box>
        ) : (
          <CampaignDetail
            campaign={detail}
            busy={busy}
            onStart={() => void runGated((opts) => startCampaign(detail.id, opts))}
            onResume={() => void runGated((opts) => resumeCampaign(detail.id, opts))}
            onPause={() => void handleSimple(() => pauseCampaign(detail.id))}
            onCancel={() => void handleSimple(() => cancelCampaign(detail.id))}
            onContinue={() => setContinueOpen(true)}
          />
        )}
      </Box>

      <CreateCampaignDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        notify={notify}
        onCreated={async (campaign) => {
          setCreateOpen(false)
          await refreshList()
          setSelectedId(campaign.id)
        }}
      />

      {detail && (
        <ContinueCampaignDialog
          open={continueOpen}
          campaign={detail}
          notify={notify}
          onClose={() => setContinueOpen(false)}
          onAdded={async (source, run) => {
            setContinueOpen(false)
            if (source) notify(`Added ${source.added} recipients (${source.suppressed} suppressed)`, 'success')
            if (run) setPending(run)
            await loadDetail(detail.id)
            await refreshList()
          }}
        />
      )}

      <ConfirmationDialog
        detail={pending?.detail ?? null}
        busy={busy}
        confirmLabel="Confirm & continue"
        onCancel={() => setPending(null)}
        onConfirm={() => void confirmPending()}
      />
    </Box>
  )
}

function StatCell({ label, value }: { label: string; value: number | string }) {
  return (
    <Box>
      <Typography variant="h6" sx={{ lineHeight: 1.1 }}>
        {value}
      </Typography>
      <Typography variant="caption" color="text.secondary">
        {label}
      </Typography>
    </Box>
  )
}

function CampaignDetail({
  campaign,
  busy,
  onStart,
  onResume,
  onPause,
  onCancel,
  onContinue,
}: {
  campaign: Campaign
  busy: boolean
  onStart: () => void
  onResume: () => void
  onPause: () => void
  onCancel: () => void
  onContinue: () => void
}) {
  const { stats } = campaign
  const done = stats.sent + stats.failed + stats.suppressed
  const progress = stats.recipients_total > 0 ? Math.round((done / stats.recipients_total) * 100) : 0
  const terminal = campaign.status === 'completed' || campaign.status === 'cancelled'
  const domainCounts = Object.entries(stats.sent_today_by_domain)

  return (
    <Box sx={{ p: 3 }}>
      <Stack direction="row" spacing={2} sx={{ alignItems: 'flex-start' }}>
        <Box sx={{ flex: 1 }}>
          <Typography variant="h5">{campaign.name || `Campaign #${campaign.id}`}</Typography>
          <Typography variant="body2" color="text.secondary">
            {campaign.owner}
            {campaign.origin === 'agent' && ` (via agent${campaign.actor ? ` ${campaign.actor}` : ''})`} ·{' '}
            {campaign.send_strategy} · cap {campaign.throttle.per_domain_daily ?? '—'}/domain/day · created{' '}
            {formatDate(campaign.created_at)}
          </Typography>
        </Box>
        <Chip label={campaign.status} color={statusColor(campaign.status)} />
      </Stack>

      {campaign.last_error && (
        <Alert severity="error" sx={{ mt: 2 }}>
          {campaign.last_error}
        </Alert>
      )}

      <Stack direction="row" spacing={1} sx={{ mt: 2, flexWrap: 'wrap' }} useFlexGap>
        {(campaign.status === 'draft' || campaign.status === 'completed') && (
          <Button
            variant="contained"
            startIcon={<PlayArrowIcon />}
            disabled={busy || campaign.status === 'completed'}
            onClick={onStart}
          >
            Launch
          </Button>
        )}
        {campaign.status === 'running' && (
          <Button variant="outlined" startIcon={<PauseIcon />} disabled={busy} onClick={onPause}>
            Pause
          </Button>
        )}
        {campaign.status === 'paused' && (
          <Button variant="contained" startIcon={<PlayArrowIcon />} disabled={busy} onClick={onResume}>
            Resume
          </Button>
        )}
        <Button variant="outlined" startIcon={<PlaylistAddIcon />} disabled={busy || terminal} onClick={onContinue}>
          Continue (add search)
        </Button>
        {!terminal && (
          <Button color="error" startIcon={<CancelIcon />} disabled={busy} onClick={onCancel}>
            Cancel
          </Button>
        )}
      </Stack>

      <Box sx={{ mt: 3 }}>
        <Stack direction="row" sx={{ justifyContent: 'space-between', mb: 0.5 }}>
          <Typography variant="body2" color="text.secondary">
            Progress
          </Typography>
          <Typography variant="body2" color="text.secondary">
            {done}/{stats.recipients_total} ({progress}%)
          </Typography>
        </Stack>
        <LinearProgress variant="determinate" value={progress} sx={{ height: 8, borderRadius: 1 }} />
      </Box>

      <Stack direction="row" spacing={4} sx={{ mt: 3, flexWrap: 'wrap' }} useFlexGap>
        <StatCell label="recipients" value={stats.recipients_total} />
        <StatCell label="pending" value={stats.pending} />
        <StatCell label="sent" value={stats.sent} />
        <StatCell label="messages sent" value={stats.messages_sent} />
        <StatCell label="suppressed" value={stats.suppressed} />
        <StatCell label="failed" value={stats.failed} />
      </Stack>

      {domainCounts.length > 0 && (
        <Box sx={{ mt: 3 }}>
          <Typography variant="subtitle2" gutterBottom>
            Sent today by domain
          </Typography>
          <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap' }} useFlexGap>
            {domainCounts.map(([domain, count]) => (
              <Chip key={domain} size="small" variant="outlined" label={`${domain}: ${count}`} />
            ))}
          </Stack>
        </Box>
      )}

      <Divider sx={{ my: 3 }} />

      <Typography variant="subtitle1" gutterBottom>
        Message
      </Typography>
      <Typography variant="body2" sx={{ fontWeight: 600 }}>
        {campaign.subject || '(no subject)'}
      </Typography>
      <Box
        component="pre"
        sx={{ m: 0, mt: 1, whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontFamily: 'inherit', fontSize: 14 }}
      >
        {campaign.body_text || '(no body)'}
      </Box>

      <Divider sx={{ my: 3 }} />

      <Typography variant="subtitle1" gutterBottom>
        Source lists ({campaign.sources.length})
      </Typography>
      <List dense disablePadding>
        {campaign.sources.map((source) => (
          <ListItemButton key={source.id} disableRipple sx={{ cursor: 'default' }}>
            <ListItemText
              primary={source.label || source.search_id || `Source #${source.id}`}
              secondary={`${source.added_count} added of ${source.recipient_count} · by ${source.added_by} · ${formatDate(source.created_at)}`}
              slotProps={{ primary: { variant: 'body2' }, secondary: { variant: 'caption' } }}
            />
          </ListItemButton>
        ))}
        {campaign.sources.length === 0 && (
          <Typography variant="body2" color="text.secondary">
            No sources yet.
          </Typography>
        )}
      </List>
    </Box>
  )
}

/** Multi-select saved-search picker shared by create + continue. */
function SavedSearchPicker({
  searches,
  loading,
  selected,
  onToggle,
}: {
  searches: SavedSearch[]
  loading: boolean
  selected: Set<number>
  onToggle: (id: number) => void
}) {
  if (loading) {
    return (
      <Box sx={{ display: 'grid', placeItems: 'center', py: 3 }}>
        <CircularProgress size={22} />
      </Box>
    )
  }
  if (searches.length === 0) {
    return (
      <Typography variant="body2" color="text.secondary" sx={{ py: 2 }}>
        No saved searches found. Build a list in the Search app first.
      </Typography>
    )
  }
  return (
    <List dense sx={{ maxHeight: 260, overflowY: 'auto', border: 1, borderColor: 'divider', borderRadius: 1 }}>
      {searches.map((search) => (
        <ListItemButton key={search.id} onClick={() => onToggle(search.id)} dense>
          <Checkbox edge="start" checked={selected.has(search.id)} tabIndex={-1} disableRipple size="small" />
          <ListItemText
            primary={search.query || `Search #${search.id}`}
            secondary={`${search.total_results} results · ${search.entity_type} · ${search.username || 'unknown'}`}
            slotProps={{ primary: { variant: 'body2' }, secondary: { variant: 'caption' } }}
          />
        </ListItemButton>
      ))}
    </List>
  )
}

function useSavedSearches(open: boolean, notify: Notify) {
  const [searches, setSearches] = useState<SavedSearch[]>([])
  const [loading, setLoading] = useState(false)
  useEffect(() => {
    if (!open) return
    let cancelled = false
    setLoading(true)
    fetchSavedSearches()
      .then((rows) => {
        if (!cancelled) setSearches(rows)
      })
      .catch((err) => {
        if (!cancelled) notify(errorMessage(err), 'error')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [open, notify])
  return { searches, loading }
}

function CreateCampaignDialog({
  open,
  onClose,
  onCreated,
  notify,
}: {
  open: boolean
  onClose: () => void
  onCreated: (campaign: Campaign) => void | Promise<void>
  notify: Notify
}) {
  const { searches, loading } = useSavedSearches(open, notify)
  const [name, setName] = useState('')
  const [subject, setSubject] = useState('')
  const [body, setBody] = useState('')
  const [strategy, setStrategy] = useState<string>('balanced')
  const [perDomain, setPerDomain] = useState(20)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    if (open) {
      setName('')
      setSubject('')
      setBody('')
      setStrategy('balanced')
      setPerDomain(20)
      setSelected(new Set())
    }
  }, [open])

  const toggle = (id: number) =>
    setSelected((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const submit = async () => {
    setSubmitting(true)
    try {
      // Resolve each chosen list into recipients (name,email) before creating.
      const sources: CampaignSourceIn[] = []
      for (const search of searches) {
        if (!selected.has(search.id)) continue
        const recipients = await fetchSearchRecipients(search.id)
        sources.push({ search_id: String(search.id), label: search.query || `Search #${search.id}`, recipients })
      }
      const total = sources.reduce((sum, source) => sum + source.recipients.length, 0)
      if (total === 0) {
        notify('The selected lists have no usable email addresses', 'error')
        setSubmitting(false)
        return
      }
      const campaign = await createCampaign({
        name,
        subject,
        body_text: body,
        send_strategy: strategy,
        throttle: { per_domain_daily: perDomain },
        sources,
      })
      await onCreated(campaign)
    } catch (err) {
      notify(errorMessage(err), 'error')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Dialog open={open} onClose={() => !submitting && onClose()} maxWidth="sm" fullWidth>
      <DialogTitle>New campaign</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          <TextField label="Name" size="small" fullWidth value={name} onChange={(e) => setName(e.target.value)} />
          <Stack direction="row" spacing={2}>
            <TextField
              select
              label="Send strategy"
              size="small"
              fullWidth
              value={strategy}
              onChange={(e) => setStrategy(e.target.value)}
            >
              {STRATEGIES.map((option) => (
                <MenuItem key={option.value} value={option.value}>
                  {option.label}
                </MenuItem>
              ))}
            </TextField>
            <TextField
              label="Per-domain daily cap"
              type="number"
              size="small"
              sx={{ width: 200 }}
              value={perDomain}
              onChange={(e) => setPerDomain(Math.max(1, Number(e.target.value) || 1))}
              slotProps={{ htmlInput: { min: 1, max: 10000 } }}
            />
          </Stack>
          <TextField label="Subject" size="small" fullWidth value={subject} onChange={(e) => setSubject(e.target.value)} />
          <TextField
            label="Message"
            fullWidth
            multiline
            minRows={6}
            value={body}
            onChange={(e) => setBody(e.target.value)}
          />
          <Box>
            <Typography variant="subtitle2" gutterBottom>
              Source lists (one or more saved searches)
            </Typography>
            <SavedSearchPicker searches={searches} loading={loading} selected={selected} onToggle={toggle} />
          </Box>
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={submitting}>
          Cancel
        </Button>
        <Button
          variant="contained"
          onClick={() => void submit()}
          disabled={submitting || selected.size === 0 || subject.trim() === ''}
          startIcon={submitting ? <CircularProgress size={14} /> : undefined}
        >
          Create
        </Button>
      </DialogActions>
    </Dialog>
  )
}

function ContinueCampaignDialog({
  open,
  campaign,
  onClose,
  onAdded,
  notify,
}: {
  open: boolean
  campaign: Campaign
  onClose: () => void
  onAdded: (
    source: { added: number; suppressed: number } | null,
    pending: Pending | null,
  ) => void | Promise<void>
  notify: Notify
}) {
  const { searches, loading } = useSavedSearches(open, notify)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    if (open) setSelected(new Set())
  }, [open])

  const toggleOne = (id: number) => setSelected(new Set(selected.has(id) ? [] : [id]))

  const submit = async () => {
    const id = [...selected][0]
    const search = searches.find((s) => s.id === id)
    if (!search) return
    setSubmitting(true)
    try {
      const recipients = await fetchSearchRecipients(search.id)
      const body: CampaignSourceIn = {
        search_id: String(search.id),
        label: search.query || `Search #${search.id}`,
        recipients,
      }
      const buildPending = (): Pending => ({
        // Placeholder detail; replaced with the real 409 below.
        detail: {} as ConfirmationRequired,
        run: async (confirmToken) => {
          await addCampaignSource(campaign.id, body, { confirm: true, confirmToken })
        },
      })
      try {
        const merge = await addCampaignSource(campaign.id, body)
        await onAdded({ added: merge.added, suppressed: merge.suppressed }, null)
      } catch (err) {
        const confirmation = confirmationRequired(err)
        if (confirmation) {
          const pending = buildPending()
          pending.detail = confirmation
          await onAdded(null, pending)
        } else {
          throw err
        }
      }
    } catch (err) {
      notify(errorMessage(err), 'error')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Dialog open={open} onClose={() => !submitting && onClose()} maxWidth="sm" fullWidth>
      <DialogTitle>Continue campaign</DialogTitle>
      <DialogContent>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Append another saved search's results. Already-messaged people are dropped by
          dedupe + suppression; growth past the guarded size needs confirmation.
        </Typography>
        <SavedSearchPicker searches={searches} loading={loading} selected={selected} onToggle={toggleOne} />
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={submitting}>
          Cancel
        </Button>
        <Button
          variant="contained"
          onClick={() => void submit()}
          disabled={submitting || selected.size === 0}
          startIcon={submitting ? <CircularProgress size={14} /> : undefined}
        >
          Add list
        </Button>
      </DialogActions>
    </Dialog>
  )
}
