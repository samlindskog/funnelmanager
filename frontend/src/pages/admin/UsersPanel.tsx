import DeleteOutlinedIcon from '@mui/icons-material/DeleteOutlined'
import KeyIcon from '@mui/icons-material/Key'
import LinkOffIcon from '@mui/icons-material/LinkOff'
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
  IconButton,
  MenuItem,
  Select,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import { useCallback, useEffect, useState } from 'react'
import { createUser, deleteUser, listUsers, unlinkChannel, updateUser } from '../../api'
import { useAuth } from '../../auth'
import type { Role, UserDetail } from '../../types'
import { RoleSelect } from './RequestsPanel'

export function UsersPanel({ roles }: { roles: Role[] }) {
  const { user: me } = useAuth()
  const [users, setUsers] = useState<UserDetail[]>([])
  const [error, setError] = useState<string | null>(null)
  const [passwordTarget, setPasswordTarget] = useState<UserDetail | null>(null)
  const [creating, setCreating] = useState(false)

  const reload = useCallback(() => {
    listUsers()
      .then((data) => {
        setUsers(data)
        setError(null)
      })
      .catch((err) => setError(err instanceof Error ? err.message : 'Failed to load users'))
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
    <Stack spacing={2}>
      {error && <Alert severity="error">{error}</Alert>}
      <Card variant="outlined">
        <CardContent>
          <Stack direction="row" sx={{ mb: 1.5, alignItems: 'center' }}>
            <Typography variant="h6" sx={{ flexGrow: 1 }}>
              Users
            </Typography>
            <Button size="small" variant="contained" onClick={() => setCreating(true)}>
              New user
            </Button>
          </Stack>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Username</TableCell>
                <TableCell>Role</TableCell>
                <TableCell>Channels</TableCell>
                <TableCell align="right" />
              </TableRow>
            </TableHead>
            <TableBody>
              {users.map((user) => (
                <TableRow key={user.username}>
                  <TableCell>
                    {user.username}
                    {me?.username === user.username && (
                      <Chip label="you" size="small" sx={{ ml: 1 }} />
                    )}
                  </TableCell>
                  <TableCell>
                    <Select
                      size="small"
                      value={user.role}
                      onChange={(e) => act(() => updateUser(user.username, { role: e.target.value }))}
                    >
                      {roles.map((role) => (
                        <MenuItem key={role.name} value={role.name}>
                          {role.name}
                        </MenuItem>
                      ))}
                    </Select>
                  </TableCell>
                  <TableCell>
                    <Stack direction="row" spacing={0.5} useFlexGap sx={{ flexWrap: 'wrap' }}>
                      {user.channels.length === 0 && (
                        <Typography variant="body2" color="text.secondary">
                          —
                        </Typography>
                      )}
                      {user.channels.map((link) => (
                        <Chip
                          key={`${link.channel}|${link.device_id}`}
                          size="small"
                          label={`${link.channel}:${link.device_id}`}
                          onDelete={() =>
                            act(() => unlinkChannel(user.username, link.channel, link.device_id))
                          }
                          deleteIcon={
                            <Tooltip title="Unlink channel">
                              <LinkOffIcon />
                            </Tooltip>
                          }
                        />
                      ))}
                    </Stack>
                  </TableCell>
                  <TableCell align="right">
                    <Tooltip title="Set password">
                      <IconButton size="small" onClick={() => setPasswordTarget(user)}>
                        <KeyIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                    <Tooltip title="Delete user">
                      <span>
                        <IconButton
                          size="small"
                          disabled={me?.username === user.username}
                          onClick={() => act(() => deleteUser(user.username))}
                        >
                          <DeleteOutlinedIcon fontSize="small" />
                        </IconButton>
                      </span>
                    </Tooltip>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      {creating && (
        <CreateUserDialog
          roles={roles}
          onClose={() => setCreating(false)}
          onDone={() => {
            setCreating(false)
            reload()
          }}
          onError={(message) => setError(message)}
        />
      )}
      {passwordTarget && (
        <SetPasswordDialog
          user={passwordTarget}
          onClose={() => setPasswordTarget(null)}
          onDone={() => {
            setPasswordTarget(null)
            reload()
          }}
          onError={(message) => setError(message)}
        />
      )}
    </Stack>
  )
}

function CreateUserDialog({
  roles,
  onClose,
  onDone,
  onError,
}: {
  roles: Role[]
  onClose: () => void
  onDone: () => void
  onError: (message: string) => void
}) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState(roles[0]?.name ?? '')
  const [submitting, setSubmitting] = useState(false)

  async function onSubmit() {
    setSubmitting(true)
    try {
      await createUser(username, password, role)
      onDone()
    } catch (err) {
      onError(err instanceof Error ? err.message : 'Create failed')
      onClose()
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Dialog open onClose={onClose} fullWidth maxWidth="xs">
      <DialogTitle>New user</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          <TextField
            label="Username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            helperText="Lowercase letters, digits, . _ - (3–32 chars)"
            fullWidth
          />
          <TextField
            label="Password"
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
        <Button
          variant="contained"
          onClick={onSubmit}
          disabled={submitting || !username || !password || !role}
        >
          Create
        </Button>
      </DialogActions>
    </Dialog>
  )
}

function SetPasswordDialog({
  user,
  onClose,
  onDone,
  onError,
}: {
  user: UserDetail
  onClose: () => void
  onDone: () => void
  onError: (message: string) => void
}) {
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)

  async function onSubmit() {
    setSubmitting(true)
    try {
      await updateUser(user.username, { password })
      onDone()
    } catch (err) {
      onError(err instanceof Error ? err.message : 'Password update failed')
      onClose()
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Dialog open onClose={onClose} fullWidth maxWidth="xs">
      <DialogTitle>Set password for “{user.username}”</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          <TextField
            label="New password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            fullWidth
          />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="contained" onClick={onSubmit} disabled={submitting || !password}>
          Save
        </Button>
      </DialogActions>
    </Dialog>
  )
}
