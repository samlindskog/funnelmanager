import {
  Alert,
  Box,
  Button,
  Paper,
  Stack,
  Tab,
  Tabs,
  TextField,
  Typography,
} from '@mui/material'
import { alpha } from '@mui/material/styles'
import { useState, type FormEvent } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import { requestAccount } from '../api'
import { useAuth } from '../auth'
import { ColorModeToggle } from '../components/ColorModeToggle'

/**
 * Deliberately nondescript landing page: no product name, no description —
 * a visitor who stumbles on it learns nothing about what it protects.
 */
export function LandingPage() {
  const { user, loading, login } = useAuth()
  const navigate = useNavigate()
  const [tab, setTab] = useState(0)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [requestedUsername, setRequestedUsername] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  if (!loading && user) return <Navigate to="/" replace />

  async function onSignIn(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setNotice(null)
    setSubmitting(true)
    try {
      await login(username, password)
      navigate('/', { replace: true })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sign in failed')
    } finally {
      setSubmitting(false)
    }
  }

  async function onRequestAccess(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setNotice(null)
    setSubmitting(true)
    try {
      await requestAccount(requestedUsername)
      setNotice('Request received.')
      setRequestedUsername('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Request failed')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Box
      sx={{
        minHeight: '100vh',
        display: 'grid',
        placeItems: 'center',
        px: 2,
        position: 'relative',
        background: (t) =>
          t.palette.mode === 'dark'
            ? `radial-gradient(circle at 15% 20%, ${alpha(t.palette.primary.main, 0.22)}, transparent 40%), radial-gradient(circle at 85% 10%, ${alpha(t.palette.primary.main, 0.16)}, transparent 45%), linear-gradient(160deg, #070B14 0%, #0C1424 55%, #10182A 100%)`
            : `radial-gradient(circle at 15% 20%, ${alpha(t.palette.secondary.main, 0.18)}, transparent 40%), radial-gradient(circle at 85% 10%, ${alpha(t.palette.primary.main, 0.22)}, transparent 45%), linear-gradient(160deg, #E8EEE9 0%, #D5E0D8 55%, #C9D5CC 100%)`,
      }}
    >
      <Box sx={{ position: 'absolute', top: 16, right: 16 }}>
        <ColorModeToggle />
      </Box>
      <Paper
        elevation={0}
        sx={{
          width: '100%',
          maxWidth: 400,
          p: { xs: 3, sm: 4 },
          border: '1px solid',
          borderColor: 'divider',
        }}
      >
        <Stack spacing={2.5}>
          <Typography variant="h4">Welcome</Typography>
          <Tabs
            value={tab}
            onChange={(_, value) => {
              setTab(value)
              setError(null)
              setNotice(null)
            }}
          >
            <Tab label="Sign in" />
            <Tab label="Request access" />
          </Tabs>
          {error && <Alert severity="error">{error}</Alert>}
          {notice && <Alert severity="success">{notice}</Alert>}
          {tab === 0 ? (
            <Stack spacing={2.5} component="form" onSubmit={onSignIn}>
              <TextField
                label="Username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoComplete="username"
                required
                fullWidth
              />
              <TextField
                label="Password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                required
                fullWidth
              />
              <Button type="submit" variant="contained" size="large" disabled={submitting}>
                {submitting ? 'Signing in…' : 'Sign in'}
              </Button>
            </Stack>
          ) : (
            <Stack spacing={2.5} component="form" onSubmit={onRequestAccess}>
              <TextField
                label="Username"
                value={requestedUsername}
                onChange={(e) => setRequestedUsername(e.target.value)}
                autoComplete="off"
                helperText="Lowercase letters, digits, . _ - (3–32 chars)"
                required
                fullWidth
              />
              <Button type="submit" variant="contained" size="large" disabled={submitting}>
                {submitting ? 'Sending…' : 'Request access'}
              </Button>
            </Stack>
          )}
        </Stack>
      </Paper>
    </Box>
  )
}
