import { Alert, Box, Tab, Tabs, Typography } from '@mui/material'
import { useCallback, useEffect, useState } from 'react'
import { listRoles } from '../../api'
import type { Role } from '../../types'
import { RequestsPanel } from './RequestsPanel'
import { RolesPanel } from './RolesPanel'
import { UsersPanel } from './UsersPanel'

export function AdminSection() {
  const [tab, setTab] = useState(0)
  const [roles, setRoles] = useState<Role[]>([])
  const [error, setError] = useState<string | null>(null)

  const reloadRoles = useCallback(() => {
    listRoles()
      .then((data) => {
        setRoles(data)
        setError(null)
      })
      .catch((err) => setError(err instanceof Error ? err.message : 'Failed to load roles'))
  }, [])

  useEffect(() => {
    reloadRoles()
  }, [reloadRoles])

  return (
    <Box>
      <Typography variant="h5" sx={{ mb: 1 }}>
        Administration
      </Typography>
      {error && (
        <Alert severity="error" sx={{ mb: 2 }}>
          {error}
        </Alert>
      )}
      <Tabs value={tab} onChange={(_, value) => setTab(value)} sx={{ mb: 2 }}>
        <Tab label="Requests" />
        <Tab label="Users" />
        <Tab label="Roles" />
      </Tabs>
      {tab === 0 && <RequestsPanel roles={roles} />}
      {tab === 1 && <UsersPanel roles={roles} />}
      {tab === 2 && <RolesPanel roles={roles} onChanged={reloadRoles} />}
    </Box>
  )
}
