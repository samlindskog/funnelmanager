import { Box, Button, CircularProgress, CssBaseline, Stack, ThemeProvider, Typography } from '@mui/material'
import { useEffect, useRef, useState } from 'react'
import { redirectToLogin } from './api'
import { AuthProvider, useAuth } from './auth'
import { completeLogin, hasSession } from './oidc'
import { SearchPage } from './pages/SearchPage'
import { ProgressCircles, ProgressProvider } from './progress'
import { theme } from './theme'

/** Auth gate mirroring mailui/agentsui: the Keycloak OIDC session is shared
 * same-origin with the hub. This app handles its own /search/callback (a direct
 * visit works without the hub) and redirects straight to Keycloak when there is
 * no session (returning to /search/ afterwards). Once authenticated the
 * AuthProvider re-reads the same session so SearchPage's useAuth/logout work. */
function Gate() {
  const { user, loading } = useAuth()
  const [error, setError] = useState<string | null>(null)
  const ranCallback = useRef(false)
  const isCallback = window.location.pathname.endsWith('/callback')

  // index.html ships the nondescript "Sign in" title; brand only post-auth.
  useEffect(() => {
    if (user) document.title = 'Search — Funnel Manager'
  }, [user])

  // Handle the Keycloak redirect (?code=&state=), then reload at a clean URL so
  // AuthProvider bootstraps with the fresh session (PKCE codes are single-use).
  useEffect(() => {
    if (!isCallback || ranCallback.current) return
    ranCallback.current = true
    completeLogin()
      .then((returnTo) => window.location.replace(returnTo || '/search/'))
      .catch((err) => setError(err instanceof Error ? err.message : 'Sign in failed'))
  }, [isCallback])

  // No session (and not mid-callback) → straight to Keycloak.
  useEffect(() => {
    if (isCallback || loading || user) return
    if (!hasSession()) redirectToLogin()
  }, [isCallback, loading, user])

  if (user) return <SearchPage />

  return (
    <Box sx={{ minHeight: '100vh', display: 'grid', placeItems: 'center' }}>
      {error ? (
        <Stack spacing={2} sx={{ alignItems: 'center' }}>
          <Typography color="text.secondary">{error}</Typography>
          <Button data-testid="search-signin-retry" variant="contained" onClick={() => redirectToLogin()}>
            Back to sign in
          </Button>
        </Stack>
      ) : (
        <CircularProgress />
      )}
    </Box>
  )
}

export default function App() {
  return (
    <ThemeProvider theme={theme} defaultMode="system" forceThemeRerender>
      <CssBaseline />
      <AuthProvider>
        <ProgressProvider>
          <Gate />
          <ProgressCircles />
        </ProgressProvider>
      </AuthProvider>
    </ThemeProvider>
  )
}
