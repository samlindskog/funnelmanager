import CampaignIcon from '@mui/icons-material/Campaign'
import LogoutIcon from '@mui/icons-material/Logout'
import MailIcon from '@mui/icons-material/Email'
import {
  Alert,
  AppBar,
  Box,
  Button,
  Divider,
  Snackbar,
  Tab,
  Tabs,
  Toolbar,
  Typography,
} from '@mui/material'
import { useCallback, useEffect, useState } from 'react'
import { logout } from './api'
import { CampaignsPage } from './CampaignsPage'
import { ColorModeToggle } from './ColorModeToggle'
import { InboxPage } from './InboxPage'
import type { User } from './types'

type View = 'inbox' | 'campaigns'
type Notice = { text: string; severity: 'success' | 'error' }

export function MailApp({ user }: { user: User }) {
  const [view, setView] = useState<View>('inbox')
  const [notice, setNotice] = useState<Notice | null>(null)

  const notify = useCallback((text: string, severity: 'success' | 'error') => {
    setNotice({ text, severity })
  }, [])

  // The OAuth round-trip lands back on /mail/?connected= or ?error=.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const connected = params.get('connected')
    const error = params.get('error')
    if (connected) notify(`Connected ${connected}`, 'success')
    if (error) notify(error, 'error')
    if (connected || error) {
      window.history.replaceState(null, '', window.location.pathname)
    }
  }, [notify])

  return (
    <Box sx={{ height: '100dvh', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      <AppBar position="static" elevation={0} color="transparent">
        <Toolbar sx={{ gap: 2 }}>
          <Box>
            <Typography variant="overline" sx={{ lineHeight: 1, display: 'block' }}>
              Funnel Manager
            </Typography>
            <Typography variant="h6" sx={{ lineHeight: 1.2 }}>
              Mail
            </Typography>
          </Box>
          <Divider orientation="vertical" flexItem sx={{ my: 1.5 }} />
          <Tabs value={view} onChange={(_, value: View) => setView(value)} sx={{ minHeight: 48 }}>
            <Tab data-testid="mail-tab-inbox" value="inbox" icon={<MailIcon fontSize="small" />} iconPosition="start" label="Inbox" sx={{ minHeight: 48 }} />
            <Tab
              data-testid="mail-tab-campaigns"
              value="campaigns"
              icon={<CampaignIcon fontSize="small" />}
              iconPosition="start"
              label="Campaigns"
              sx={{ minHeight: 48 }}
            />
          </Tabs>
          <Box sx={{ flex: 1 }} />
          <Button data-testid="mail-open-hub" component="a" href="/" color="inherit" size="small">
            Hub
          </Button>
          <Typography variant="body2" color="text.secondary">
            {user.username}
          </Typography>
          <ColorModeToggle />
          <Button data-testid="mail-signout" color="inherit" size="small" startIcon={<LogoutIcon />} onClick={() => void logout()}>
            Log out
          </Button>
        </Toolbar>
      </AppBar>
      <Divider />

      {view === 'inbox' ? (
        <InboxPage notify={notify} />
      ) : (
        <CampaignsPage user={user} notify={notify} />
      )}

      <Snackbar
        open={notice != null}
        autoHideDuration={5000}
        onClose={() => setNotice(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
      >
        <Alert severity={notice?.severity ?? 'success'} variant="filled" onClose={() => setNotice(null)}>
          {notice?.text}
        </Alert>
      </Snackbar>
    </Box>
  )
}
