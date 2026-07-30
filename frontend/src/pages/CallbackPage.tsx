import { Alert, Box, Button, CircularProgress, Stack } from '@mui/material'
import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { completeLogin } from '../oidc'

/** Keycloak redirects here with ?code=...&state=...; exchange and resume. */
export function CallbackPage() {
  const navigate = useNavigate()
  const [error, setError] = useState<string | null>(null)
  const ran = useRef(false)

  useEffect(() => {
    if (ran.current) return // StrictMode double-invoke guard: codes are single-use
    ran.current = true
    completeLogin()
      .then((returnTo) => {
        // Full reload so AuthProvider re-reads the fresh session.
        window.location.replace(returnTo)
      })
      .catch((err) => setError(err instanceof Error ? err.message : 'Sign in failed'))
  }, [navigate])

  return (
    <Box sx={{ minHeight: '100vh', display: 'grid', placeItems: 'center' }}>
      {error ? (
        <Stack spacing={2} sx={{ alignItems: 'center' }}>
          <Alert severity="error">{error}</Alert>
          <Button
            data-testid="hub-signin-retry"
            variant="contained"
            onClick={() => window.location.replace('/login')}
          >
            Back to sign in
          </Button>
        </Stack>
      ) : (
        <CircularProgress />
      )}
    </Box>
  )
}
