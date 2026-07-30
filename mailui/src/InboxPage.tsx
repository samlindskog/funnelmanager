import AddIcon from '@mui/icons-material/Add'
import AttachFileIcon from '@mui/icons-material/AttachFile'
import BackupIcon from '@mui/icons-material/Backup'
import DeleteOutlinedIcon from '@mui/icons-material/DeleteOutlined'
import EditIcon from '@mui/icons-material/Edit'
import InboxIcon from '@mui/icons-material/AllInbox'
import RefreshIcon from '@mui/icons-material/Refresh'
import ReplyIcon from '@mui/icons-material/Reply'
import SearchIcon from '@mui/icons-material/Search'
import SyncIcon from '@mui/icons-material/Sync'
import {
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
  MenuItem,
  Pagination,
  PaginationItem,
  Stack,
  Tab,
  Tabs,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  confirmationRequired,
  deleteMailAccount,
  downloadMailAttachment,
  fetchAllMailMessages,
  fetchMailAccounts,
  fetchMailMessage,
  fetchMailMessages,
  fetchMailOauthUrl,
  fetchMailThread,
  sendMailMessage,
  startBackup,
  triggerMailSync,
} from './api'
import { ConfirmationDialog } from './ConfirmationDialog'
import type {
  ConfirmationRequired,
  MailAccount,
  MailMessageDetail,
  MailMessagePage,
  MailMessageSummary,
  MailThread,
} from './types'

const LABELS = ['INBOX', 'SENT', 'ALL'] as const
type MailLabel = (typeof LABELS)[number]
const PER_PAGE = 50
const ACCOUNTS_REFRESH_MS = 30_000
const ALL_MAILBOXES = null

type Notify = (text: string, severity: 'success' | 'error') => void

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

interface ReadState {
  message: MailMessageDetail
  thread: MailThread | null
}

interface BackupGate {
  accountId: number
  detail: ConfirmationRequired
}

export function InboxPage({ notify }: { notify: Notify }) {
  const [accounts, setAccounts] = useState<MailAccount[] | null>(null)
  // null selectedId => the unified "All mailboxes" view across every domain.
  const [selectedId, setSelectedId] = useState<number | null>(ALL_MAILBOXES)
  const [label, setLabel] = useState<MailLabel>('INBOX')
  const [queryInput, setQueryInput] = useState('')
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(1)
  const [messages, setMessages] = useState<MailMessagePage | null>(null)
  const [loadingMessages, setLoadingMessages] = useState(false)
  const [read, setRead] = useState<ReadState | null>(null)
  const [compose, setCompose] = useState<ComposeState | null>(null)
  const [sending, setSending] = useState(false)
  const [connecting, setConnecting] = useState(false)
  const [backupGate, setBackupGate] = useState<BackupGate | null>(null)
  const [backupBusy, setBackupBusy] = useState(false)
  const selectedIdRef = useRef<number | null>(selectedId)
  selectedIdRef.current = selectedId

  const selectedAccount = useMemo(
    () => accounts?.find((account) => account.id === selectedId) ?? null,
    [accounts, selectedId],
  )
  const accountById = useCallback(
    (id: number) => accounts?.find((account) => account.id === id) ?? null,
    [accounts],
  )

  const refreshAccounts = useCallback(
    async (silent = false) => {
      try {
        setAccounts(await fetchMailAccounts())
      } catch (err) {
        if (!silent) notify(errorMessage(err), 'error')
      }
    },
    [notify],
  )

  useEffect(() => {
    void refreshAccounts()
    const timer = window.setInterval(() => void refreshAccounts(true), ACCOUNTS_REFRESH_MS)
    return () => window.clearInterval(timer)
  }, [refreshAccounts])

  const loadMessages = useCallback(async () => {
    setLoadingMessages(true)
    try {
      const params = { label, q: query, page, perPage: PER_PAGE }
      setMessages(
        selectedId == null
          ? await fetchAllMailMessages(params)
          : await fetchMailMessages({ accountId: selectedId, ...params }),
      )
    } catch (err) {
      notify(errorMessage(err), 'error')
    } finally {
      setLoadingMessages(false)
    }
  }, [selectedId, label, query, page, notify])

  useEffect(() => {
    void loadMessages()
  }, [loadMessages])

  const handleConnect = async () => {
    setConnecting(true)
    try {
      window.location.href = await fetchMailOauthUrl()
    } catch (err) {
      notify(errorMessage(err), 'error')
      setConnecting(false)
    }
  }

  const handleSync = async (accountId: number) => {
    try {
      await triggerMailSync(accountId)
      notify('Sync started', 'success')
    } catch (err) {
      notify(errorMessage(err), 'error')
    }
  }

  const handleRemove = async (account: MailAccount) => {
    if (!window.confirm(`Remove ${account.email} and its ${account.message_count} stored messages?`)) {
      return
    }
    try {
      await deleteMailAccount(account.id)
      if (selectedIdRef.current === account.id) setSelectedId(ALL_MAILBOXES)
      await refreshAccounts()
      notify(`Removed ${account.email}`, 'success')
    } catch (err) {
      notify(errorMessage(err), 'error')
    }
  }

  // Principle-4 backup gate: authorize the full archive; a mailbox estimated over
  // the size threshold returns 409 confirmation_required, answered by the dialog.
  const handleBackup = async (accountId: number, confirmToken?: string) => {
    setBackupBusy(true)
    try {
      await startBackup(accountId, confirmToken ? { confirm: true, confirmToken } : {})
      setBackupGate(null)
      notify('Full backup authorized', 'success')
      await refreshAccounts(true)
    } catch (err) {
      const confirmation = confirmationRequired(err)
      if (confirmation) {
        setBackupGate({ accountId, detail: confirmation })
      } else {
        notify(errorMessage(err), 'error')
      }
    } finally {
      setBackupBusy(false)
    }
  }

  const handleOpenMessage = async (summary: MailMessageSummary) => {
    try {
      const message = await fetchMailMessage(summary.id)
      setRead({ message, thread: null })
      // Best-effort thread hydration for a Gmail-like conversation view.
      if (summary.thread_id) {
        try {
          const thread = await fetchMailThread(summary.account_id, summary.thread_id)
          setRead((current) =>
            current && current.message.id === message.id ? { ...current, thread } : current,
          )
        } catch {
          /* single-message fallback */
        }
      }
    } catch (err) {
      notify(errorMessage(err), 'error')
    }
  }

  const openCompose = (reply?: MailMessageDetail) => {
    const fromId = reply?.account_id ?? selectedId ?? accounts?.[0]?.id
    if (fromId == null) {
      notify('Connect a mailbox before composing', 'error')
      return
    }
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
      setCompose({ accountId: fromId, to: '', cc: '', bcc: '', subject: '', body: '', replyToMessageId: null })
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
      notify('Sent', 'success')
      if (label !== 'INBOX') void loadMessages()
      void refreshAccounts(true)
    } catch (err) {
      notify(errorMessage(err), 'error')
    } finally {
      setSending(false)
    }
  }

  const pageCount = messages ? Math.max(1, Math.ceil(messages.total / messages.per_page)) : 1
  const threadMessages = read?.thread && read.thread.messages.length > 1 ? read.thread.messages : null

  return (
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
            data-testid="mail-connect-account"
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
          <List dense disablePadding>
            <ListItemButton
              data-testid="mail-mailbox-all"
              selected={selectedId === ALL_MAILBOXES}
              onClick={() => {
                setSelectedId(ALL_MAILBOXES)
                setPage(1)
              }}
              sx={{ py: 1 }}
            >
              <InboxIcon fontSize="small" sx={{ mr: 1.5, color: 'text.secondary' }} />
              <ListItemText
                primary="All mailboxes"
                secondary={accounts ? `${accounts.length} connected · every domain` : ''}
                slotProps={{ primary: { variant: 'body2', sx: { fontWeight: 600 } }, secondary: { variant: 'caption' } }}
              />
            </ListItemButton>
          </List>
          <Divider />
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
              {accounts.map((account) => {
                const backupPending = !account.backfill_authorized && !account.backfill_done
                return (
                  <ListItemButton
                    key={account.id}
                    data-testid={`mail-mailbox-${account.id}`}
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
                          ) : backupPending ? (
                            <Chip label="backup pending" color="warning" size="small" />
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
                          {backupPending && (
                            <Tooltip
                              title={
                                account.backup_estimate_bytes > 0
                                  ? `Authorize full backup (~${formatSize(account.backup_estimate_bytes)})`
                                  : 'Authorize full backup'
                              }
                            >
                              <IconButton
                                data-testid={`mail-account-backup-${account.id}`}
                                size="small"
                                onClick={(event) => {
                                  event.stopPropagation()
                                  void handleBackup(account.id)
                                }}
                              >
                                <BackupIcon fontSize="inherit" />
                              </IconButton>
                            </Tooltip>
                          )}
                          <Tooltip title="Sync now">
                            <IconButton
                              data-testid={`mail-account-sync-${account.id}`}
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
                              data-testid={`mail-account-delete-${account.id}`}
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
                )
              })}
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
              <Tab key={item} data-testid={`mail-label-${item.toLowerCase()}`} value={item} label={item.toLowerCase()} sx={{ minHeight: 40 }} />
            ))}
          </Tabs>
          <Box sx={{ flex: 1 }} />
          <TextField
            size="small"
            placeholder={selectedAccount ? 'Search this mailbox' : 'Search all mailboxes'}
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
            <IconButton data-testid="mail-refresh" size="small" onClick={() => void loadMessages()} sx={{ mb: 0.5 }}>
              <RefreshIcon fontSize="small" />
            </IconButton>
          </Tooltip>
          <Button
            data-testid="mail-compose"
            size="small"
            variant="contained"
            startIcon={<EditIcon />}
            onClick={() => openCompose()}
            sx={{ mb: 0.5 }}
          >
            Compose
          </Button>
        </Stack>

        <Box sx={{ px: 2, py: 0.75, borderBottom: 1, borderColor: 'divider' }}>
          <Typography variant="caption" color="text.secondary">
            {selectedAccount ? selectedAccount.email : 'All connected mailboxes across every domain'}
          </Typography>
        </Box>

        <Box sx={{ flex: 1, overflowY: 'auto' }}>
          {loadingMessages && messages == null ? (
            <Box sx={{ display: 'grid', placeItems: 'center', py: 6 }}>
              <CircularProgress size={28} />
            </Box>
          ) : messages == null || messages.items.length === 0 ? (
            <Typography color="text.secondary" sx={{ p: 3 }}>
              {query ? 'No messages match the search.' : 'No messages here.'}
            </Typography>
          ) : (
            <List dense disablePadding>
              {messages.items.map((message) => (
                <ListItemButton
                  key={message.id}
                  data-testid={`mail-message-${message.id}`}
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
                        {selectedId == null && (
                          <Chip
                            size="small"
                            variant="outlined"
                            label={accountById(message.account_id)?.email ?? `#${message.account_id}`}
                            sx={{ maxWidth: 180 }}
                          />
                        )}
                        {message.has_attachments && (
                          <AttachFileIcon sx={{ fontSize: 16, color: 'text.secondary' }} />
                        )}
                        <Typography variant="caption" color="text.secondary" sx={{ flexShrink: 0 }}>
                          {formatDate(message.date)}
                        </Typography>
                      </Stack>
                    }
                    secondary={
                      <Typography variant="caption" color="text.secondary" noWrap component="span">
                        {message.snippet}
                      </Typography>
                    }
                    disableTypography
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
            renderItem={(item) => (
              <PaginationItem
                {...item}
                data-testid={
                  item.type === 'previous'
                    ? 'mail-page-prev'
                    : item.type === 'next'
                      ? 'mail-page-next'
                      : undefined
                }
              />
            )}
          />
        </Stack>
      </Box>

      {/* Message / thread detail */}
      <Dialog open={read != null} onClose={() => setRead(null)} maxWidth="md" fullWidth>
        {read && (
          <>
            <DialogTitle sx={{ pb: 1 }}>
              {read.message.subject || '(no subject)'}
              {threadMessages && (
                <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
                  Conversation · {threadMessages.length} messages
                </Typography>
              )}
            </DialogTitle>
            <DialogContent dividers sx={{ p: 0 }}>
              {(threadMessages ?? [read.message]).map((item, index) => (
                <Box
                  key={item.id}
                  sx={{ borderTop: index === 0 ? 0 : 1, borderColor: 'divider' }}
                >
                  <Box sx={{ px: 2, pt: 1.5 }}>
                    <Typography variant="body2" sx={{ fontWeight: 600 }}>
                      {item.from_addr || '—'}
                    </Typography>
                    <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
                      To {item.to_addrs.join(', ') || '—'}
                      {item.cc_addrs.length > 0 && ` · Cc ${item.cc_addrs.join(', ')}`} ·{' '}
                      {formatDate(item.date)}
                    </Typography>
                  </Box>
                  {item.body_html ? (
                    <Box
                      component="iframe"
                      title={`Message ${item.id}`}
                      sandbox=""
                      srcDoc={item.body_html}
                      sx={{
                        width: '100%',
                        height: threadMessages ? '32vh' : '52vh',
                        border: 0,
                        display: 'block',
                        bgcolor: '#fff',
                        mt: 1,
                      }}
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
                      {item.body_text || item.snippet || '(empty message)'}
                    </Box>
                  )}
                  {item.attachments.length > 0 && (
                    <Stack direction="row" spacing={1} sx={{ p: 2, flexWrap: 'wrap' }} useFlexGap>
                      {item.attachments.map((attachment) => (
                        <Chip
                          key={attachment.attachment_id}
                          data-testid={`mail-attachment-download-${attachment.attachment_id}`}
                          icon={<AttachFileIcon />}
                          label={`${attachment.filename} (${formatSize(attachment.size)})`}
                          onClick={() =>
                            void downloadMailAttachment(item.id, attachment).catch((err) =>
                              notify(errorMessage(err), 'error'),
                            )
                          }
                        />
                      ))}
                    </Stack>
                  )}
                </Box>
              ))}
            </DialogContent>
            <DialogActions>
              <Button
                data-testid="mail-reply"
                startIcon={<ReplyIcon />}
                onClick={() => {
                  const latest = threadMessages ? threadMessages[threadMessages.length - 1] : read.message
                  openCompose(latest)
                  setRead(null)
                }}
              >
                Reply
              </Button>
              <Button data-testid="mail-read-close" onClick={() => setRead(null)}>Close</Button>
            </DialogActions>
          </>
        )}
      </Dialog>

      {/* Compose */}
      <Dialog open={compose != null} onClose={() => !sending && setCompose(null)} maxWidth="sm" fullWidth>
        {compose && (
          <>
            <DialogTitle>{compose.replyToMessageId != null ? 'Reply' : 'New message'}</DialogTitle>
            <DialogContent>
              <Stack spacing={2} sx={{ mt: 1 }}>
                <TextField
                  select
                  label="From"
                  size="small"
                  fullWidth
                  value={compose.accountId}
                  disabled={compose.replyToMessageId != null}
                  onChange={(event) =>
                    setCompose({ ...compose, accountId: Number(event.target.value) })
                  }
                >
                  {(accounts ?? []).map((account) => (
                    <MenuItem key={account.id} data-testid={`mail-compose-from-${account.id}`} value={account.id}>
                      {account.email}
                    </MenuItem>
                  ))}
                </TextField>
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
              <Button data-testid="mail-send-cancel" onClick={() => setCompose(null)} disabled={sending}>
                Cancel
              </Button>
              <Button
                data-testid="mail-send"
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

      <ConfirmationDialog
        detail={backupGate?.detail ?? null}
        busy={backupBusy}
        confirmLabel="Back up anyway"
        onCancel={() => setBackupGate(null)}
        onConfirm={() => {
          if (backupGate) void handleBackup(backupGate.accountId, backupGate.detail.confirm_token)
        }}
      />
    </Box>
  )
}

function errorMessage(err: unknown): string {
  if (err instanceof Error) return err.message
  return 'Something went wrong'
}
