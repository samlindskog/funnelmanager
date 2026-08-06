import LogoutIcon from '@mui/icons-material/Logout'
import {
  Alert,
  AppBar,
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  MenuItem,
  Snackbar,
  Stack,
  TextField,
  Toolbar,
  Typography,
} from '@mui/material'
import { useCallback, useEffect, useRef, useState } from 'react'
import { createSession, fetchModels, fetchSessions, logout } from './api'
import { ChatView } from './ChatView'
import { ColorModeToggle } from './ColorModeToggle'
import { SessionList } from './SessionList'
import { errorMessage } from './status'
import type { ModelInfo, SessionSummary, User } from './types'

const PAGE = 50
const LIST_REFRESH_MS = 4000

export function AgentsApp({ user }: { user: User }) {
  const [sessions, setSessions] = useState<SessionSummary[] | null>(null)
  const [owners, setOwners] = useState<string[]>([])
  const [ownerFilter, setOwnerFilter] = useState('')
  const [limit, setLimit] = useState(PAGE)
  const [loadingMore, setLoadingMore] = useState(false)
  const [hasMore, setHasMore] = useState(false)

  const [selected, setSelected] = useState<SessionSummary | null>(null)

  const [models, setModels] = useState<ModelInfo[]>([])
  const [defaultModel, setDefaultModel] = useState('')

  const [newOpen, setNewOpen] = useState(false)
  const [newTitle, setNewTitle] = useState('')
  const [newModel, setNewModel] = useState('')
  const [creating, setCreating] = useState(false)

  const [notice, setNotice] = useState<{ text: string; severity: 'success' | 'error' } | null>(null)
  const notify = useCallback((text: string, severity: 'success' | 'error') => setNotice({ text, severity }), [])

  // Latest filter/limit read by the poller without re-arming it.
  const paramsRef = useRef({ owner: '', limit: PAGE })
  paramsRef.current = { owner: ownerFilter, limit }

  const refresh = useCallback(
    async (silent = false) => {
      try {
        const { owner, limit: lim } = paramsRef.current
        const data = await fetchSessions({ owner: owner || undefined, limit: lim })
        setSessions(data.sessions)
        setOwners(data.owners)
        setHasMore(data.sessions.length >= lim)
      } catch (err) {
        if (!silent) notify(errorMessage(err), 'error')
      } finally {
        setLoadingMore(false)
      }
    },
    [notify],
  )

  useEffect(() => {
    fetchModels()
      .then((data) => {
        setModels(data.models)
        setDefaultModel(data.default)
        setNewModel(data.default)
      })
      .catch(() => {
        /* models are optional chrome; the composer still works */
      })
  }, [])

  // Reload on filter/limit change; poll quietly so live statuses advance.
  useEffect(() => {
    void refresh()
  }, [refresh, ownerFilter, limit])

  useEffect(() => {
    const timer = window.setInterval(() => void refresh(true), LIST_REFRESH_MS)
    return () => window.clearInterval(timer)
  }, [refresh])

  const handleCreate = async () => {
    setCreating(true)
    try {
      const created = await createSession({
        title: newTitle.trim() || undefined,
        model: newModel || undefined,
      })
      setNewOpen(false)
      setNewTitle('')
      setSelected(created)
      notify('Session created', 'success')
      void refresh(true)
    } catch (err) {
      notify(errorMessage(err), 'error')
    } finally {
      setCreating(false)
    }
  }

  const handleRenamed = useCallback((updated: SessionSummary) => {
    setSessions((current) =>
      current ? current.map((s) => (s.id === updated.id ? { ...s, title: updated.title } : s)) : current,
    )
    setSelected((current) => (current && current.id === updated.id ? { ...current, title: updated.title } : current))
  }, [])

  const handleDeleted = useCallback(
    (sessionId: string) => {
      setSessions((current) => (current ? current.filter((s) => s.id !== sessionId) : current))
      setSelected((current) => (current && current.id === sessionId ? null : current))
      void refresh(true)
    },
    [refresh],
  )

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
          <Button data-testid="agents-open-hub" component="a" href="/" color="inherit" size="small">
            Hub
          </Button>
          <Box sx={{ flex: 1 }} />
          <Typography variant="body2" color="text.secondary">
            {user.username}
          </Typography>
          <ColorModeToggle />
          <Button
            data-testid="agents-signout"
            color="inherit"
            size="small"
            startIcon={<LogoutIcon />}
            onClick={() => void logout()}
          >
            Log out
          </Button>
        </Toolbar>
      </AppBar>
      <Divider />

      <Box sx={{ flex: 1, display: 'flex', minHeight: 0, overflow: 'hidden' }}>
        <SessionList
          user={user}
          sessions={sessions}
          owners={owners}
          ownerFilter={ownerFilter}
          onOwnerFilter={(owner) => {
            setLimit(PAGE)
            setOwnerFilter(owner)
          }}
          selectedId={selected?.id ?? null}
          onSelect={setSelected}
          onNew={() => {
            setNewModel(defaultModel)
            setNewOpen(true)
          }}
          onRefresh={() => void refresh()}
          onLoadMore={() => {
            setLoadingMore(true)
            setLimit((current) => current + PAGE)
          }}
          hasMore={hasMore}
          loadingMore={loadingMore}
        />

        {selected ? (
          <ChatView
            key={selected.id}
            user={user}
            session={selected}
            models={models}
            onRenamed={handleRenamed}
            onDeleted={handleDeleted}
            notify={notify}
          />
        ) : (
          <Box sx={{ flex: 1, display: 'grid', placeItems: 'center', bgcolor: 'background.paper' }}>
            <Stack spacing={1} sx={{ alignItems: 'center' }}>
              <Typography variant="h6">Pick a session</Typography>
              <Typography color="text.secondary">
                Select a conversation on the left, or start a new chat with a runtime agent.
              </Typography>
              <Button data-testid="agents-new-session-empty" variant="contained" onClick={() => setNewOpen(true)}>
                New session
              </Button>
            </Stack>
          </Box>
        )}
      </Box>

      {/* New session dialog with an optional model pick */}
      <Dialog open={newOpen} onClose={() => setNewOpen(false)} maxWidth="xs" fullWidth>
        <DialogTitle>New session</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 0.5 }}>
            <TextField
              data-testid="agents-new-title"
              label="Title (optional)"
              placeholder="Auto-derives from your first message"
              fullWidth
              value={newTitle}
              onChange={(event) => setNewTitle(event.target.value)}
            />
            <TextField
              data-testid="agents-new-model"
              select
              label="Model"
              fullWidth
              value={newModel}
              onChange={(event) => setNewModel(event.target.value)}
              helperText="The OpenAI model this session's turns run on"
            >
              {models.length === 0 && defaultModel && (
                <MenuItem value={defaultModel} data-testid={`agents-new-model-${defaultModel}`}>
                  {defaultModel}
                </MenuItem>
              )}
              {models.map((m) => (
                <MenuItem key={m.id} value={m.id} data-testid={`agents-new-model-${m.id}`}>
                  {m.id}
                  {m.id === defaultModel ? ' (default)' : ''}
                </MenuItem>
              ))}
            </TextField>
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button data-testid="agents-new-cancel" onClick={() => setNewOpen(false)}>
            Cancel
          </Button>
          <Button data-testid="agents-new-create" variant="contained" disabled={creating} onClick={() => void handleCreate()}>
            Create
          </Button>
        </DialogActions>
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
