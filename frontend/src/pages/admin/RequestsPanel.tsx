import {
  Alert,
  Button,
  Card,
  CardContent,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  TextField,
  Typography,
} from '@mui/material'
import { useCallback, useEffect, useState } from 'react'
import { approveAccountRequest, denyAccountRequest, listAccountRequests } from '../../api'
import type { AccountRequest, Role } from '../../types'

/** Pending account requests from the public "request an account" form. */
export function RequestsPanel({ roles }: { roles: Role[] }) {
  const [accountRequests, setAccountRequests] = useState<AccountRequest[]>([])
  const [error, setError] = useState<string | null>(null)
  const [approveTarget, setApproveTarget] = useState<AccountRequest | null>(null)

  const reload = useCallback(() => {
    listAccountRequests()
      .then((accounts) => {
        setAccountRequests(accounts)
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
