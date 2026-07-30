import { Box, Button, CircularProgress, CssBaseline, Stack, ThemeProvider, Typography } from '@mui/material'
import { useEffect, useRef, useState } from 'react'
import { redirectToLogin } from './api'
import { AgentsApp } from './AgentsApp'
import { beginLogin, completeLogin, currentClaims, getAccessToken, hasSession } from './oidc'
import { theme } from './theme'
import type { User } from './types'

/** Auth gate: the Keycloak OIDC session is shared same-origin with the hub.
 * No session -> straight to Keycloak (back to /agents/ afterwards). This app
 * also handles its own /agents/callback so a direct visit works without the
 * hub. */
export default function App() {
  const [user, setUser] = useState<User | null>(null)
  const [error, setError] = useState<string | null>(null)
  const ran = useRef(false)

  // index.html ships the nondescript "Sign in" title; brand only post-auth.
  useEffect(() => {
    if (user) document.title = 'Agents — Funnel Manager'
  }, [user])

  useEffect(() => {
    if (ran.current) return // StrictMode double-invoke guard: PKCE codes are single-use
    ran.current = true
    async function bootstrap() {
      if (window.location.pathname.endsWith('/callback')) {
        const returnTo = await completeLogin()
        window.history.replaceState(null, '', returnTo || '/agents/')
      }
      if (!hasSession()) {
        void beginLogin('/agents/')
        return
      }
      await getAccessToken() // refresh if stale
      const claims = currentClaims()
      if (!claims) {
        redirectToLogin()
        return
      }
      setUser({
        username: claims.username,
        role: claims.roles.includes('admin') ? 'admin' : '',
      })
    }
    bootstrap().catch((err: unknown) => {
      setError(err instanceof Error ? err.message : 'Could not load your session')
    })
  }, [])

  return (
    <ThemeProvider theme={theme} defaultMode="system">
      <CssBaseline />
      {user ? (
        <AgentsApp user={user} />
      ) : (
        <Box sx={{ minHeight: '100vh', display: 'grid', placeItems: 'center' }}>
          {error ? (
            <Stack spacing={2} sx={{ alignItems: 'center' }}>
              <Typography color="text.secondary">{error}</Typography>
              <Button data-testid="agents-signin-retry" variant="contained" onClick={() => redirectToLogin()}>
                Back to sign in
              </Button>
            </Stack>
          ) : (
            <CircularProgress />
          )}
        </Box>
      )}
    </ThemeProvider>
  )
}
