import AddIcon from '@mui/icons-material/Add'
import AttachFileIcon from '@mui/icons-material/AttachFile'
import DeleteOutlinedIcon from '@mui/icons-material/DeleteOutlined'
import EditIcon from '@mui/icons-material/Edit'
import LogoutIcon from '@mui/icons-material/Logout'
import RefreshIcon from '@mui/icons-material/Refresh'
import ReplyIcon from '@mui/icons-material/Reply'
import SearchIcon from '@mui/icons-material/Search'
import SyncIcon from '@mui/icons-material/Sync'
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
  List,
  ListItemButton,
  ListItemText,
  Pagination,
  Snackbar,
  Stack,
  Tab,
  Tabs,
  TextField,
  Toolbar,
  Tooltip,
  Typography,
} from '@mui/material'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link as RouterLink, useSearchParams } from 'react-router-dom'
import {
  ApiError,
  deleteMailAccount,
  downloadMailAttachment,
  fetchMailAccounts,
  fetchMailMessage,
  fetchMailMessages,
  fetchMailOauthUrl,
  sendMailMessage,
  triggerMailSync,
} from '../api'
import { useAuth } from '../auth'
import { ColorModeToggle } from '../components/ColorModeToggle'
import type {
  MailAccount,
  MailMessageDetail,
  MailMessagePage,
  MailMessageSummary,
} from '../types'

const LABELS = ['INBOX', 'SENT', 'ALL'] as const
type MailLabel = (typeof LABELS)[number]
const PER_PAGE = 50
const ACCOUNTS_REFRESH_MS = 30_000

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message
  if (err instanceof Error) return err.message
  return 'Something went wrong'
}

/** "Jane Doe <jane@x.com>" -> "jane@x.com" (used to prefill replies). */
function extractEmail(formatted: string): string {
  const match = formatted.match(/<([^>]+)>/)
  return (match ? match[1] : formatted).trim()
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

function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`
  return `${bytes} B`
}

interface ComposeState {
  accountId: number
  to: string
  cc: string
  bcc: string
  subject: string
  body: string
  replyToMessageId: number | null
}

export function MailPage() {
  const { user, logout } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()

  const [accounts, setAccounts] = useState<MailAccount[] | null>(null)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [label, setLabel] = useState<MailLabel>('INBOX')
  const [queryInput, setQueryInput] = useState('')
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(1)
  const [messages, setMessages] = useState<MailMessagePage | null>(null)
  const [loadingMessages, setLoadingMessages] = useState(false)
  const [openMessage, setOpenMessage] = useState<MailMessageDetail | null>(null)
  const [compose, setCompose] = useState<ComposeState | null>(null)
  const [sending, setSending] = useState(false)
  const [connecting, setConnecting] = useState(false)
  const [notice, setNotice] = useState<{ text: string; severity: 'success' | 'error' } | null>(null)
  const selectedIdRef = useRef<number | null>(null)
  selectedIdRef.current = selectedId

  const selectedAccount = useMemo(
    () => accounts?.find((account) => account.id === selectedId) ?? null,
    [accounts, selectedId],
  )

  const refreshAccounts = useCallback(async (silent = false) => {
    try {
      const list = await fetchMailAccounts()
      setAccounts(list)
      if (selectedIdRef.current == null && list.length > 0) {
        setSelectedId(list[0].id)
      } else if (
        selectedIdRef.current != null &&
        !list.some((account) => account.id === selectedIdRef.current)
      ) {
        setSelectedId(list.length > 0 ? list[0].id : null)
      }
    } catch (err) {
      if (!silent) setNotice({ text: errorMessage(err), severity: 'error' })
    }
  }, [])

  // OAuth round-trip lands back here with ?connected= / ?error=.
  useEffect(() => {
    const connected = searchParams.get('connected')
    const error = searchParams.get('error')
    if (connected) setNotice({ text: `Connected ${connected}`, severity: 'success' })
    if (error) setNotice({ text: error, severity: 'error' })
    if (connected || error) setSearchParams({}, { replace: true })
  }, [searchParams, setSearchParams])

  useEffect(() => {
    void refreshAccounts()
    const timer = window.setInterval(() => void refreshAccounts(true), ACCOUNTS_REFRESH_MS)
    return () => window.clearInterval(timer)
  }, [refreshAccounts])

  const loadMessages = useCallback(async () => {
    if (selectedId == null) {
      setMessages(null)
      return
    }
    setLoadingMessages(true)
    try {
      setMessages(
        await fetchMailMessages({ accountId: selectedId, label, q: query, page, perPage: PER_PAGE }),
      )
    } catch (err) {
      setNotice({ text: errorMessage(err), severity: 'error' })
    } finally {
      setLoadingMessages(false)
    }
  }, [selectedId, label, query, page])

  useEffect(() => {
    void loadMessages()
  }, [loadMessages])

  const handleConnect = async () => {
    setConnecting(true)
    try {
      window.location.href = await fetchMailOauthUrl()
    } catch (err) {
      setNotice({ text: errorMessage(err), severity: 'error' })
      setConnecting(false)
    }
  }

  const handleSync = async (accountId: number) => {
    try {
      await triggerMailSync(accountId)
      setNotice({ text: 'Sync started', severity: 'success' })
    } catch (err) {
      setNotice({ text: errorMessage(err), severity: 'error' })
    }
  }

  const handleRemove = async (account: MailAccount) => {
    if (!window.confirm(`Remove ${account.email} and its ${account.message_count} stored messages?`)) {
      return
    }
    try {
      await deleteMailAccount(account.id)
      await refreshAccounts()
      setNotice({ text: `Removed ${account.email}`, severity: 'success' })
    } catch (err) {
      setNotice({ text: errorMessage(err), severity: 'error' })
    }
  }

  const handleOpenMessage = async (summary: MailMessageSummary) => {
    try {
      setOpenMessage(await fetchMailMessage(summary.id))
    } catch (err) {
      setNotice({ text: errorMessage(err), severity: 'error' })
    }
  }

  const openCompose = (reply?: MailMessageDetail) => {
    if (selectedId == null) return
    if (reply) {
      const quoted = reply.body_text
        ? `\n\nOn ${formatDate(reply.date)}, ${reply.from_addr} wrote:\n` +
          reply.body_text
            .split('\n')
            .map((line) => `> ${line}`)
            .join('\n')
        : ''
      setCompose({
        accountId: reply.account_id,
        to: extractEmail(reply.from_addr),
        cc: '',
        bcc: '',
        subject: reply.subject.toLowerCase().startsWith('re:') ? reply.subject : `Re: ${reply.subject}`,
        body: quoted,
        replyToMessageId: reply.id,
      })
    } else {
      setCompose({
        accountId: selectedId,
        to: '',
        cc: '',
        bcc: '',
        subject: '',
        body: '',
        replyToMessageId: null,
      })
    }
  }

  const splitAddresses = (value: string): string[] =>
    value
      .split(/[,;]/)
      .map((item) => extractEmail(item))
      .filter(Boolean)

  const handleSend = async () => {
    if (!compose) return
    setSending(true)
    try {
      await sendMailMessage(compose.accountId, {
        to: splitAddresses(compose.to),
        cc: splitAddresses(compose.cc),
        bcc: splitAddresses(compose.bcc),
        subject: compose.subject,
        body_text: compose.body,
        reply_to_message_id: compose.replyToMessageId,
      })
      setCompose(null)
      setNotice({ text: 'Sent', severity: 'success' })
      if (label !== 'INBOX') void loadMessages()
      void refreshAccounts(true)
    } catch (err) {
      setNotice({ text: errorMessage(err), severity: 'error' })
    } finally {
      setSending(false)
    }
  }

  const pageCount = messages ? Math.max(1, Math.ceil(messages.total / messages.per_page)) : 1

  return (
    <Box sx={{ height: '100dvh', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      <AppBar position="static" elevation={0} color="transparent">
        <Toolbar sx={{ gap: 2 }}>
          <Box>
            <Typography variant="overline" sx={{ lineHeight: 1, display: 'block' }}>
              Funnel Manager
            </Typography>
            <Typography variant="h6" sx={{ lineHeight: 1.2 }}>
              Mail
            </Typography>
          </Box>
          <Divider orientation="vertical" flexItem sx={{ my: 1.5 }} />
          <Button component={RouterLink} to="/" color="inherit" size="small">
            Hub
          </Button>
          <Box sx={{ flex: 1 }} />
          {user && (
            <Typography variant="body2" color="text.secondary">
              {user.username}
            </Typography>
          )}
          <ColorModeToggle />
          <Button color="inherit" size="small" startIcon={<LogoutIcon />} onClick={logout}>
            Log out
          </Button>
        </Toolbar>
      </AppBar>
      <Divider />

      <Box sx={{ flex: 1, display: 'flex', minHeight: 0, overflow: 'hidden' }}>
        {/* Mailboxes sidebar */}
        <Box
          sx={{
            width: 320,
            flexShrink: 0,
            borderRight: 1,
            borderColor: 'divider',
            display: 'flex',
            flexDirection: 'column',
            minHeight: 0,
          }}
        >
          <Stack direction="row" spacing={1} sx={{ alignItems: 'center', px: 2, py: 1.5 }}>
            <Typography variant="subtitle1" sx={{ flex: 1 }}>
              Mailboxes
            </Typography>
            <Button
              size="small"
              variant="outlined"
              startIcon={connecting ? <CircularProgress size={14} /> : <AddIcon />}
              disabled={connecting}
              onClick={handleConnect}
            >
              Connect
            </Button>
          </Stack>
          <Divider />
          <Box sx={{ flex: 1, overflowY: 'auto' }}>
            {accounts == null ? (
              <Box sx={{ display: 'grid', placeItems: 'center', py: 4 }}>
                <CircularProgress size={24} />
              </Box>
            ) : accounts.length === 0 ? (
              <Typography variant="body2" color="text.secondary" sx={{ p: 2 }}>
                No mailboxes yet. Connect a Google account to start syncing its mail.
              </Typography>
            ) : (
              <List dense disablePadding>
                {accounts.map((account) => (
                  <ListItemButton
                    key={account.id}
                    selected={account.id === selectedId}
                    onClick={() => {
                      setSelectedId(account.id)
                      setPage(1)
                    }}
                    sx={{ alignItems: 'flex-start', py: 1 }}
                  >
                    <ListItemText
                      primary={
                        <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
                          <Typography variant="body2" sx={{ fontWeight: 600, flex: 1 }} noWrap>
                            {account.email}
                          </Typography>
                          {account.status === 'error' ? (
                            <Tooltip title={account.last_error}>
                              <Chip label="error" color="error" size="small" />
                            </Tooltip>
                          ) : !account.backfill_done ? (
                            <Chip label="backfilling" color="warning" size="small" />
                          ) : null}
                        </Stack>
                      }
                      secondary={
                        <Stack
                          direction="row"
                          spacing={1}
                          component="span"
                          sx={{ alignItems: 'center', mt: 0.25 }}
                        >
                          <Typography variant="caption" color="text.secondary" component="span">
                            {account.inbox_count} inbox · {account.sent_count} sent ·{' '}
                            {account.message_count} total
                          </Typography>
                          <Box sx={{ flex: 1 }} component="span" />
                          <Tooltip title="Sync now">
                            <IconButton
                              size="small"
                              onClick={(event) => {
                                event.stopPropagation()
                                void handleSync(account.id)
                              }}
                            >
                              <SyncIcon fontSize="inherit" />
                            </IconButton>
                          </Tooltip>
                          <Tooltip title="Remove mailbox">
                            <IconButton
                              size="small"
                              onClick={(event) => {
                                event.stopPropagation()
                                void handleRemove(account)
                              }}
                            >
                              <DeleteOutlinedIcon fontSize="inherit" />
                            </IconButton>
                          </Tooltip>
                        </Stack>
                      }
                      disableTypography
                    />
                  </ListItemButton>
                ))}
              </List>
            )}
          </Box>
        </Box>

        {/* Messages pane */}
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
          {selectedAccount == null ? (
            <Box sx={{ display: 'grid', placeItems: 'center', flex: 1 }}>
              <Typography color="text.secondary">
                Connect or select a mailbox to browse its messages.
              </Typography>
            </Box>
          ) : (
            <>
              <Stack
                direction="row"
                spacing={2}
                sx={{ alignItems: 'center', px: 2, pt: 1, borderBottom: 1, borderColor: 'divider' }}
              >
                <Tabs
                  value={label}
                  onChange={(_, value: MailLabel) => {
                    setLabel(value)
                    setPage(1)
                  }}
                  sx={{ minHeight: 40 }}
                >
                  {LABELS.map((item) => (
                    <Tab key={item} value={item} label={item.toLowerCase()} sx={{ minHeight: 40 }} />
                  ))}
                </Tabs>
                <Box sx={{ flex: 1 }} />
                <TextField
                  size="small"
                  placeholder="Filter messages"
                  value={queryInput}
                  onChange={(event) => setQueryInput(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') {
                      setQuery(queryInput.trim())
                      setPage(1)
                    }
                  }}
                  slotProps={{
                    input: {
                      startAdornment: (
                        <InputAdornment position="start">
                          <SearchIcon fontSize="small" />
                        </InputAdornment>
                      ),
                    },
                  }}
                  sx={{ width: 260, mb: 0.5 }}
                />
                <Tooltip title="Refresh list">
                  <IconButton size="small" onClick={() => void loadMessages()} sx={{ mb: 0.5 }}>
                    <RefreshIcon fontSize="small" />
                  </IconButton>
                </Tooltip>
                <Button
                  size="small"
                  variant="contained"
                  startIcon={<EditIcon />}
                  onClick={() => openCompose()}
                  sx={{ mb: 0.5 }}
                >
                  Compose
                </Button>
              </Stack>

              <Box sx={{ flex: 1, overflowY: 'auto' }}>
                {loadingMessages && messages == null ? (
                  <Box sx={{ display: 'grid', placeItems: 'center', py: 6 }}>
                    <CircularProgress size={28} />
                  </Box>
                ) : messages == null || messages.items.length === 0 ? (
                  <Typography color="text.secondary" sx={{ p: 3 }}>
                    {query
                      ? 'No messages match the filter.'
                      : selectedAccount.backfill_done
                        ? 'No messages here.'
                        : 'No messages here yet — the mailbox is still backfilling.'}
                  </Typography>
                ) : (
                  <List dense disablePadding>
                    {messages.items.map((message) => (
                      <ListItemButton
                        key={message.id}
                        divider
                        onClick={() => void handleOpenMessage(message)}
                        sx={{ alignItems: 'flex-start', py: 1 }}
                      >
                        <ListItemText
                          primary={
                            <Stack direction="row" spacing={1} sx={{ alignItems: 'baseline' }}>
                              <Typography
                                variant="body2"
                                noWrap
                                sx={{ width: 220, flexShrink: 0, fontWeight: message.unread ? 700 : 400 }}
                              >
                                {label === 'SENT'
                                  ? `To: ${message.to_addrs.join(', ') || '—'}`
                                  : message.from_addr || '—'}
                              </Typography>
                              <Typography
                                variant="body2"
                                noWrap
                                sx={{ flex: 1, fontWeight: message.unread ? 600 : 400 }}
                              >
                                {message.subject || '(no subject)'}
                              </Typography>
                              {message.has_attachments && (
                                <AttachFileIcon sx={{ fontSize: 16, color: 'text.secondary' }} />
                              )}
                              <Typography
                                variant="caption"
                                color="text.secondary"
                                sx={{ flexShrink: 0 }}
                              >
                                {formatDate(message.date)}
                              </Typography>
                            </Stack>
                          }
                          secondary={
                            <Typography variant="caption" color="text.secondary" noWrap component="span">
                              {message.snippet}
                            </Typography>
                          }
                        />
                      </ListItemButton>
                    ))}
                  </List>
                )}
              </Box>

              <Stack direction="row" sx={{ alignItems: 'center', p: 1, borderTop: 1, borderColor: 'divider' }}>
                <Typography variant="caption" color="text.secondary" sx={{ px: 1 }}>
                  {messages ? `${messages.total} messages` : ''}
                </Typography>
                <Box sx={{ flex: 1 }} />
                <Pagination
                  size="small"
                  count={pageCount}
                  page={page}
                  onChange={(_, value) => setPage(value)}
                />
              </Stack>
            </>
          )}
        </Box>
      </Box>

      {/* Message detail */}
      <Dialog open={openMessage != null} onClose={() => setOpenMessage(null)} maxWidth="md" fullWidth>
        {openMessage && (
          <>
            <DialogTitle sx={{ pb: 1 }}>
              {openMessage.subject || '(no subject)'}
              <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                From {openMessage.from_addr || '—'} · {formatDate(openMessage.date)}
              </Typography>
              <Typography variant="body2" color="text.secondary">
                To {openMessage.to_addrs.join(', ') || '—'}
                {openMessage.cc_addrs.length > 0 && ` · Cc ${openMessage.cc_addrs.join(', ')}`}
              </Typography>
            </DialogTitle>
            <DialogContent dividers sx={{ p: 0 }}>
              {openMessage.body_html ? (
                <Box
                  component="iframe"
                  title="Message body"
                  sandbox=""
                  srcDoc={openMessage.body_html}
                  sx={{ width: '100%', height: '55vh', border: 0, display: 'block', bgcolor: '#fff' }}
                />
              ) : (
                <Box
                  component="pre"
                  sx={{
                    m: 0,
                    p: 2,
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-word',
                    fontFamily: 'inherit',
                    fontSize: 14,
                  }}
                >
                  {openMessage.body_text || openMessage.snippet || '(empty message)'}
                </Box>
              )}
              {openMessage.attachments.length > 0 && (
                <Stack direction="row" spacing={1} sx={{ p: 2, flexWrap: 'wrap' }} useFlexGap>
                  {openMessage.attachments.map((attachment) => (
                    <Chip
                      key={attachment.attachment_id}
                      icon={<AttachFileIcon />}
                      label={`${attachment.filename} (${formatSize(attachment.size)})`}
                      onClick={() =>
                        void downloadMailAttachment(openMessage.id, attachment).catch((err) =>
                          setNotice({ text: errorMessage(err), severity: 'error' }),
                        )
                      }
                    />
                  ))}
                </Stack>
              )}
            </DialogContent>
            <DialogActions>
              <Button
                startIcon={<ReplyIcon />}
                onClick={() => {
                  openCompose(openMessage)
                  setOpenMessage(null)
                }}
              >
                Reply
              </Button>
              <Button onClick={() => setOpenMessage(null)}>Close</Button>
            </DialogActions>
          </>
        )}
      </Dialog>

      {/* Compose */}
      <Dialog open={compose != null} onClose={() => !sending && setCompose(null)} maxWidth="sm" fullWidth>
        {compose && (
          <>
            <DialogTitle>
              {compose.replyToMessageId != null ? 'Reply' : 'New message'}
              <Typography variant="body2" color="text.secondary">
                From {accounts?.find((account) => account.id === compose.accountId)?.email}
              </Typography>
            </DialogTitle>
            <DialogContent>
              <Stack spacing={2} sx={{ mt: 1 }}>
                <TextField
                  label="To"
                  size="small"
                  fullWidth
                  value={compose.to}
                  onChange={(event) => setCompose({ ...compose, to: event.target.value })}
                  helperText="Separate multiple addresses with commas"
                />
                <Stack direction="row" spacing={2}>
                  <TextField
                    label="Cc"
                    size="small"
                    fullWidth
                    value={compose.cc}
                    onChange={(event) => setCompose({ ...compose, cc: event.target.value })}
                  />
                  <TextField
                    label="Bcc"
                    size="small"
                    fullWidth
                    value={compose.bcc}
                    onChange={(event) => setCompose({ ...compose, bcc: event.target.value })}
                  />
                </Stack>
                <TextField
                  label="Subject"
                  size="small"
                  fullWidth
                  value={compose.subject}
                  onChange={(event) => setCompose({ ...compose, subject: event.target.value })}
                />
                <TextField
                  label="Message"
                  fullWidth
                  multiline
                  minRows={8}
                  value={compose.body}
                  onChange={(event) => setCompose({ ...compose, body: event.target.value })}
                />
              </Stack>
            </DialogContent>
            <DialogActions>
              <Button onClick={() => setCompose(null)} disabled={sending}>
                Cancel
              </Button>
              <Button
                variant="contained"
                onClick={() => void handleSend()}
                disabled={sending || splitAddresses(compose.to).length === 0}
                startIcon={sending ? <CircularProgress size={14} /> : undefined}
              >
                Send
              </Button>
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
        <Alert
          severity={notice?.severity ?? 'success'}
          variant="filled"
          onClose={() => setNotice(null)}
        >
          {notice?.text}
        </Alert>
      </Snackbar>
    </Box>
  )
}
