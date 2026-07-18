import {
  Alert,
  Button,
  Card,
  CardContent,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  FormControlLabel,
  InputLabel,
  MenuItem,
  Radio,
  RadioGroup,
  Select,
  Stack,
  TextField,
  Typography,
} from '@mui/material'
import { useCallback, useEffect, useState } from 'react'
import {
  approveAccountRequest,
  approveChannelPairing,
  assignChannelRequest,
  denyAccountRequest,
  denyChannelRequest,
  listChannelRequests,
  listAccountRequests,
  listUsers,
} from '../../api'
import type { AccountRequest, ChannelRequest, Role, UserDetail } from '../../types'

/** Pending channel requests (from OpenClaw) + pending account requests. */
export function RequestsPanel({ roles }: { roles: Role[] }) {
  const [channelRequests, setChannelRequests] = useState<ChannelRequest[]>([])
  const [accountRequests, setAccountRequests] = useState<AccountRequest[]>([])
  const [users, setUsers] = useState<UserDetail[]>([])
  const [error, setError] = useState<string | null>(null)
  const [assignTarget, setAssignTarget] = useState<ChannelRequest | null>(null)
  const [approveTarget, setApproveTarget] = useState<AccountRequest | null>(null)

  const reload = useCallback(() => {
    Promise.all([listChannelRequests(), listAccountRequests(), listUsers()])
      .then(([channels, accounts, allUsers]) => {
        setChannelRequests(channels)
        setAccountRequests(accounts)
        setUsers(allUsers)
        setError(null)
      })
      .catch((err) => setError(err instanceof Error ? err.message : 'Failed to load requests'))
  }, [])

  useEffect(() => {
    reload()
  }, [reload])

  async function act(action: () => Promise<unknown>) {
    setError(null)
    try {
      await action()
      reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Action failed')
    }
  }

  return (
    <Stack spacing={3}>
      {error && <Alert severity="error">{error}</Alert>}

      <Card variant="outlined">
        <CardContent>
          <Typography variant="h6" sx={{ mb: 1.5 }}>
            Channel requests
          </Typography>
          {channelRequests.length === 0 && (
            <Typography color="text.secondary">
              No pending channel requests. New channel identities (e.g. Telegram senders)
              appear here when they first message the agent — approve the pairing and
              assign a profile without leaving this page.
            </Typography>
          )}
          <Stack spacing={1.5}>
            {channelRequests.map((req) => (
              <Stack
                key={`${req.channel}|${req.device_id}`}
                direction={{ xs: 'column', sm: 'row' }}
                spacing={1}
                sx={{ alignItems: { sm: 'center' } }}
              >
                <Chip label={req.channel} size="small" />
                {req.pairing_code && (
                  <Chip label="pairing pending" size="small" color="warning" variant="outlined" />
                )}
                <Typography sx={{ flexGrow: 1, fontFamily: 'monospace' }}>
                  {req.device_id}
                  {req.display_name ? `  (${req.display_name})` : ''}
                </Typography>
                {req.pairing_code && (
                  <Button
                    size="small"
                    variant="outlined"
                    onClick={() =>
                      act(() => approveChannelPairing(req.channel, req.device_id))
                    }
                  >
                    Approve pairing
                  </Button>
                )}
                <Button size="small" variant="contained" onClick={() => setAssignTarget(req)}>
                  {req.pairing_code ? 'Approve & assign' : 'Assign'}
                </Button>
                <Button
                  size="small"
                  color="inherit"
                  onClick={() => act(() => denyChannelRequest(req.channel, req.device_id))}
                >
                  Deny
                </Button>
              </Stack>
            ))}
          </Stack>
        </CardContent>
      </Card>

      <Card variant="outlined">
        <CardContent>
          <Typography variant="h6" sx={{ mb: 1.5 }}>
            Account requests
          </Typography>
          {accountRequests.length === 0 && (
            <Typography color="text.secondary">No pending account requests.</Typography>
          )}
          <Stack spacing={1.5}>
            {accountRequests.map((req) => (
              <Stack
                key={req.username}
                direction={{ xs: 'column', sm: 'row' }}
                spacing={1}
                sx={{ alignItems: { sm: 'center' } }}
              >
                <Typography sx={{ flexGrow: 1 }}>{req.username}</Typography>
                <Button size="small" variant="contained" onClick={() => setApproveTarget(req)}>
                  Approve
                </Button>
                <Button
                  size="small"
                  color="inherit"
                  onClick={() => act(() => denyAccountRequest(req.username))}
                >
                  Deny
                </Button>
              </Stack>
            ))}
          </Stack>
        </CardContent>
      </Card>

      {assignTarget && (
        <AssignChannelDialog
          request={assignTarget}
          users={users}
          roles={roles}
          onClose={() => setAssignTarget(null)}
          onDone={() => {
            setAssignTarget(null)
            reload()
          }}
          onError={(message) => setError(message)}
        />
      )}
      {approveTarget && (
        <ApproveAccountDialog
          request={approveTarget}
          roles={roles}
          onClose={() => setApproveTarget(null)}
          onDone={() => {
            setApproveTarget(null)
            reload()
          }}
          onError={(message) => setError(message)}
        />
      )}
    </Stack>
  )
}

function AssignChannelDialog({
  request,
  users,
  roles,
  onClose,
  onDone,
  onError,
}: {
  request: ChannelRequest
  users: UserDetail[]
  roles: Role[]
  onClose: () => void
  onDone: () => void
  onError: (message: string) => void
}) {
  const [mode, setMode] = useState<'existing' | 'new'>(users.length ? 'existing' : 'new')
  const [username, setUsername] = useState(users[0]?.username ?? '')
  const [newUsername, setNewUsername] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [newRole, setNewRole] = useState(roles[0]?.name ?? '')
  const [submitting, setSubmitting] = useState(false)

  async function onSubmit() {
    setSubmitting(true)
    try {
      await assignChannelRequest({
        channel: request.channel,
        device_id: request.device_id,
        ...(mode === 'existing'
          ? { username }
          : { new_user: { username: newUsername, password: newPassword, role: newRole } }),
      })
      onDone()
    } catch (err) {
      onError(err instanceof Error ? err.message : 'Assign failed')
      onClose()
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Dialog open onClose={onClose} fullWidth maxWidth="xs">
      <DialogTitle>
        Assign {request.channel} · {request.device_id}
      </DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          <RadioGroup value={mode} onChange={(e) => setMode(e.target.value as 'existing' | 'new')}>
            <FormControlLabel
              value="existing"
              control={<Radio />}
              label="Existing user"
              disabled={!users.length}
            />
            <FormControlLabel value="new" control={<Radio />} label="New user" />
          </RadioGroup>
          {mode === 'existing' ? (
            <FormControl fullWidth>
              <InputLabel id="assign-user-label">User</InputLabel>
              <Select
                labelId="assign-user-label"
                label="User"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              >
                {users.map((user) => (
                  <MenuItem key={user.username} value={user.username}>
                    {user.username} ({user.role})
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
          ) : (
            <>
              <TextField
                label="Username"
                value={newUsername}
                onChange={(e) => setNewUsername(e.target.value)}
                fullWidth
              />
              <TextField
                label="Password"
                type="password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                fullWidth
              />
              <RoleSelect roles={roles} value={newRole} onChange={setNewRole} />
            </>
          )}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button
          variant="contained"
          onClick={onSubmit}
          disabled={
            submitting ||
            (mode === 'existing' ? !username : !newUsername || !newPassword || !newRole)
          }
        >
          {request.pairing_code ? 'Approve & assign' : 'Assign'}
        </Button>
      </DialogActions>
    </Dialog>
  )
}

function ApproveAccountDialog({
  request,
  roles,
  onClose,
  onDone,
  onError,
}: {
  request: AccountRequest
  roles: Role[]
  onClose: () => void
  onDone: () => void
  onError: (message: string) => void
}) {
  const [password, setPassword] = useState('')
  const [role, setRole] = useState(roles[0]?.name ?? '')
  const [submitting, setSubmitting] = useState(false)

  async function onSubmit() {
    setSubmitting(true)
    try {
      await approveAccountRequest(request.username, password, role)
      onDone()
    } catch (err) {
      onError(err instanceof Error ? err.message : 'Approve failed')
      onClose()
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Dialog open onClose={onClose} fullWidth maxWidth="xs">
      <DialogTitle>Approve “{request.username}”</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          <TextField
            label="Initial password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            fullWidth
          />
          <RoleSelect roles={roles} value={role} onChange={setRole} />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="contained" onClick={onSubmit} disabled={submitting || !password || !role}>
          Create user
        </Button>
      </DialogActions>
    </Dialog>
  )
}

export function RoleSelect({
  roles,
  value,
  onChange,
  label = 'Role',
}: {
  roles: Role[]
  value: string
  onChange: (role: string) => void
  label?: string
}) {
  return (
    <FormControl fullWidth>
      <InputLabel id={`role-select-${label}`}>{label}</InputLabel>
      <Select
        labelId={`role-select-${label}`}
        label={label}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        {roles.map((role) => (
          <MenuItem key={role.name} value={role.name}>
            {role.name}
          </MenuItem>
        ))}
      </Select>
    </FormControl>
  )
}
