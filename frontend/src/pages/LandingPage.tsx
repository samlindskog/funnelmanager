import { Alert, Box, Button, Paper, Stack, Typography } from '@mui/material'
import { alpha } from '@mui/material/styles'
import { useEffect, useState } from 'react'
import { Navigate } from 'react-router-dom'
import { useAuth } from '../auth'
import { ColorModeToggle } from '../components/ColorModeToggle'

/**
 * Deliberately nondescript landing page: no product name, no description —
 * a visitor who stumbles on it learns nothing about what it protects.
 * Authentication happens at Keycloak (auth-code + PKCE); this page only
 * starts the redirect. Accounts are provisioned by an admin in Keycloak.
 */
export function LandingPage() {
  const { user, loading, login } = useAuth()
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  // Restore the nondescript title after signing out of a titled app.
  useEffect(() => {
    document.title = 'Sign in'
  }, [])

  if (!loading && user) return <Navigate to="/" replace />

  async function onSignIn() {
    setError(null)
    setSubmitting(true)
    try {
      await login() // navigates away
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sign in failed')
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
          {error && <Alert severity="error">{error}</Alert>}
          <Button
            data-testid="hub-signin"
            variant="contained"
            size="large"
            onClick={onSignIn}
            disabled={submitting}
          >
            {submitting ? 'Redirecting…' : 'Sign in'}
          </Button>
        </Stack>
      </Paper>
    </Box>
  )
}
