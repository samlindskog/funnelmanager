import { Box, CircularProgress, CssBaseline, ThemeProvider, Typography } from '@mui/material'
import { useEffect, useState } from 'react'
import { redirectToLogin } from './api'
import { MailApp } from './MailApp'
import { beginLogin, completeLogin, currentClaims, getAccessToken, hasSession } from './oidc'
import { theme } from './theme'
import type { User } from './types'

/** Auth gate: the Keycloak OIDC session is shared same-origin with the hub.
 * No session -> straight to Keycloak (back to /mail/ afterwards). This app
 * also handles its own /mail/callback so a direct visit works without the
 * hub. */
export default function App() {
  const [user, setUser] = useState<User | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    async function bootstrap() {
      if (window.location.pathname.endsWith('/callback')) {
        const returnTo = await completeLogin()
        window.history.replaceState(null, '', returnTo || '/mail/')
      }
      if (!hasSession()) {
        void beginLogin('/mail/')
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
        <MailApp user={user} />
      ) : (
        <Box sx={{ minHeight: '100vh', display: 'grid', placeItems: 'center' }}>
          {error ? <Typography color="text.secondary">{error}</Typography> : <CircularProgress />}
        </Box>
      )}
    </ThemeProvider>
  )
}
