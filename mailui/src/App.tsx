import { Box, CircularProgress, CssBaseline, ThemeProvider, Typography } from '@mui/material'
import { useEffect, useState } from 'react'
import { fetchMe, getToken, redirectToLogin } from './api'
import { MailApp } from './MailApp'
import { theme } from './theme'
import type { User } from './types'

/** Auth gate: the session is issued by the hub (same origin), we only consume
 * it. No token or a 401 sends the browser back to the hub's /login. */
export default function App() {
  const [user, setUser] = useState<User | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!getToken()) {
      redirectToLogin()
      return
    }
    fetchMe()
      .then(setUser)
      .catch((err: unknown) => {
        // A 401 already redirected to /login inside request(); anything else
        // (auth service down) is worth surfacing instead of spinning forever.
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
