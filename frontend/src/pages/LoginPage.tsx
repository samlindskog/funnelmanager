import {
  Alert,
  Box,
  Button,
  Paper,
  Stack,
  TextField,
  Typography,
} from '@mui/material'
import { alpha, useTheme } from '@mui/material/styles'
import { useState, type FormEvent } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth'
import { ColorModeToggle } from '../components/ColorModeToggle'

export function LoginPage() {
  const theme = useTheme()
  const { user, loading, login } = useAuth()
  const navigate = useNavigate()
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  if (!loading && user) return <Navigate to="/" replace />

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await login(username, password)
      navigate('/', { replace: true })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed')
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
          maxWidth: 420,
          p: { xs: 3, sm: 4 },
          border: '1px solid',
          borderColor: 'divider',
        }}
      >
        <Stack spacing={2.5} component="form" onSubmit={onSubmit}>
          <Box>
            <Typography
              variant="overline"
              color={theme.palette.mode === 'dark' ? 'primary' : 'secondary'}
              sx={{ letterSpacing: 1.5 }}
            >
              Funnel Manager
            </Typography>
            <Typography variant="h4" sx={{ mt: 0.5 }}>
              Sign in
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
              Search and inspect Apollo person and company records.
            </Typography>
          </Box>
          {error && <Alert severity="error">{error}</Alert>}
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
      </Paper>
    </Box>
  )
}
