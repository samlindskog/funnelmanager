import AddIcon from '@mui/icons-material/Add'
import DeleteOutlinedIcon from '@mui/icons-material/DeleteOutlined'
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
  IconButton,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import { useState } from 'react'
import { createRole, deleteRole } from '../../api'
import type { Grant, Role } from '../../types'

const SERVICES = ['*', 'auth', 'search', 'leads']
const METHOD_OPTIONS = ['*', 'GET', 'POST', 'PATCH', 'PUT', 'DELETE']

/** Role list + creation. Grants are (service, methods, path prefix) rows the
 * auth service pushes to OPA; a request is allowed when any grant matches. */
export function RolesPanel({ roles, onChanged }: { roles: Role[]; onChanged: () => void }) {
  const [error, setError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)

  async function act(action: () => Promise<unknown>) {
    setError(null)
    try {
      await action()
      onChanged()
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
              Roles
            </Typography>
            <Button size="small" variant="contained" onClick={() => setCreating(true)}>
              New role
            </Button>
          </Stack>
          <Stack spacing={2}>
            {roles.map((role) => (
              <Stack
                key={role.name}
                direction="row"
                spacing={1.5}
                sx={{ alignItems: 'flex-start', borderTop: '1px solid', borderColor: 'divider', pt: 1.5 }}
              >
                <Stack sx={{ flexGrow: 1 }} spacing={0.5}>
                  <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
                    <Typography sx={{ fontWeight: 600 }}>{role.name}</Typography>
                    {role.description && (
                      <Typography variant="body2" color="text.secondary">
                        {role.description}
                      </Typography>
                    )}
                  </Stack>
                  <Stack direction="row" spacing={0.5} useFlexGap sx={{ flexWrap: 'wrap' }}>
                    {role.grants.map((grant, index) => (
                      <Chip
                        key={index}
                        size="small"
                        variant="outlined"
                        label={`${grant.service} · ${grant.methods.join(',')} · ${grant.path_prefix}`}
                      />
                    ))}
                    {role.grants.length === 0 && (
                      <Typography variant="body2" color="text.secondary">
                        No grants (denies everything)
                      </Typography>
                    )}
                  </Stack>
                </Stack>
                <Tooltip
                  title={role.name === 'admin' ? 'The admin role cannot be deleted' : 'Delete role'}
                >
                  <span>
                    <IconButton
                      size="small"
                      disabled={role.name === 'admin'}
                      onClick={() => act(() => deleteRole(role.name))}
                    >
                      <DeleteOutlinedIcon fontSize="small" />
                    </IconButton>
                  </span>
                </Tooltip>
              </Stack>
            ))}
          </Stack>
        </CardContent>
      </Card>

      {creating && (
        <CreateRoleDialog
          onClose={() => setCreating(false)}
          onDone={() => {
            setCreating(false)
            onChanged()
          }}
          onError={(message) => setError(message)}
        />
      )}
    </Stack>
  )
}

function CreateRoleDialog({
  onClose,
  onDone,
  onError,
}: {
  onClose: () => void
  onDone: () => void
  onError: (message: string) => void
}) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [grants, setGrants] = useState<Grant[]>([
    { service: '*', methods: ['*'], path_prefix: '/' },
  ])
  const [submitting, setSubmitting] = useState(false)

  function setGrant(index: number, fields: Partial<Grant>) {
    setGrants((prev) => prev.map((grant, i) => (i === index ? { ...grant, ...fields } : grant)))
  }

  async function onSubmit() {
    setSubmitting(true)
    try {
      await createRole(name, description, grants)
      onDone()
    } catch (err) {
      onError(err instanceof Error ? err.message : 'Create failed')
      onClose()
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Dialog open onClose={onClose} fullWidth maxWidth="sm">
      <DialogTitle>New role</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          <TextField
            label="Name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            helperText="Lowercase letters, digits, _ - (2–32 chars)"
            fullWidth
          />
          <TextField
            label="Description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            fullWidth
          />
          <Typography variant="subtitle2">Grants</Typography>
          {grants.map((grant, index) => (
            <Stack key={index} direction="row" spacing={1} sx={{ alignItems: 'center' }}>
              <FormControl sx={{ minWidth: 110 }}>
                <InputLabel id={`grant-service-${index}`}>Service</InputLabel>
                <Select
                  labelId={`grant-service-${index}`}
                  label="Service"
                  size="small"
                  value={grant.service}
                  onChange={(e) => setGrant(index, { service: e.target.value })}
                >
                  {SERVICES.map((service) => (
                    <MenuItem key={service} value={service}>
                      {service}
                    </MenuItem>
                  ))}
                </Select>
              </FormControl>
              <FormControl sx={{ minWidth: 140 }}>
                <InputLabel id={`grant-methods-${index}`}>Methods</InputLabel>
                <Select
                  labelId={`grant-methods-${index}`}
                  label="Methods"
                  size="small"
                  multiple
                  value={grant.methods}
                  onChange={(e) => {
                    const value = e.target.value
                    const methods = typeof value === 'string' ? value.split(',') : value
                    setGrant(index, { methods: methods.length ? methods : ['*'] })
                  }}
                >
                  {METHOD_OPTIONS.map((method) => (
                    <MenuItem key={method} value={method}>
                      {method}
                    </MenuItem>
                  ))}
                </Select>
              </FormControl>
              <TextField
                label="Path prefix"
                size="small"
                value={grant.path_prefix}
                onChange={(e) => setGrant(index, { path_prefix: e.target.value })}
                sx={{ flexGrow: 1 }}
              />
              <IconButton
                size="small"
                disabled={grants.length === 1}
                onClick={() => setGrants((prev) => prev.filter((_, i) => i !== index))}
              >
                <DeleteOutlinedIcon fontSize="small" />
              </IconButton>
            </Stack>
          ))}
          <Button
            size="small"
            startIcon={<AddIcon />}
            onClick={() =>
              setGrants((prev) => [...prev, { service: '*', methods: ['*'], path_prefix: '/' }])
            }
            sx={{ alignSelf: 'flex-start' }}
          >
            Add grant
          </Button>
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="contained" onClick={onSubmit} disabled={submitting || !name}>
          Create
        </Button>
      </DialogActions>
    </Dialog>
  )
}
