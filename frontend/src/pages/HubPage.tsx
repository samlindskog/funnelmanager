import LaunchIcon from '@mui/icons-material/Launch'
import LogoutIcon from '@mui/icons-material/Logout'
import {
  Alert,
  Box,
  Button,
  Card,
  CardActionArea,
  CardContent,
  Chip,
  Container,
  Divider,
  Stack,
  Typography,
} from '@mui/material'
import { useEffect, useState } from 'react'
import { fetchApps } from '../api'
import { useAuth } from '../auth'
import { ColorModeToggle } from '../components/ColorModeToggle'
import type { AppLink } from '../types'
import { AdminSection } from './admin/AdminSection'

export function HubPage() {
  const { user, logout } = useAuth()
  const [apps, setApps] = useState<AppLink[]>([])
  const [appsError, setAppsError] = useState<string | null>(null)

  useEffect(() => {
    fetchApps()
      .then(setApps)
      .catch((err) => setAppsError(err instanceof Error ? err.message : 'Failed to load apps'))
  }, [])

  if (!user) return null
  const isAdmin = user.role === 'admin'

  return (
    <Box sx={{ minHeight: '100vh', bgcolor: 'background.default' }}>
      <Box
        component="header"
        sx={{
          px: 3,
          py: 1.5,
          display: 'flex',
          alignItems: 'center',
          gap: 2,
          borderBottom: '1px solid',
          borderColor: 'divider',
          bgcolor: 'background.paper',
        }}
      >
        <Typography variant="h6" sx={{ flexGrow: 1 }}>
          Funnel Manager
        </Typography>
        <ColorModeToggle />
        <Button
          size="small"
          color="inherit"
          startIcon={<LogoutIcon fontSize="small" />}
          onClick={logout}
        >
          Sign out
        </Button>
      </Box>

      <Container maxWidth="md" sx={{ py: 4 }}>
        <Stack spacing={4}>
          <Card variant="outlined">
            <CardContent>
              <Stack direction="row" spacing={2} sx={{ alignItems: 'center' }}>
                <Box sx={{ flexGrow: 1 }}>
                  <Typography variant="overline" color="text.secondary">
                    Signed in as
                  </Typography>
                  <Typography variant="h5">{user.username}</Typography>
                </Box>
                <Chip
                  label={user.role || 'no role'}
                  color={isAdmin ? 'primary' : 'default'}
                  size="small"
                />
              </Stack>
            </CardContent>
          </Card>

          <Box>
            <Typography variant="h5" sx={{ mb: 2 }}>
              Apps
            </Typography>
            {appsError && <Alert severity="error">{appsError}</Alert>}
            {!appsError && apps.length === 0 && (
              <Typography color="text.secondary">No apps configured.</Typography>
            )}
            <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} useFlexGap sx={{ flexWrap: 'wrap' }}>
              {apps.map((app) => (
                <Card key={app.name} variant="outlined" sx={{ minWidth: 240, flex: '0 1 280px' }}>
                  <CardActionArea component="a" href={app.url} target="_blank" rel="noopener">
                    <CardContent>
                      <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
                        <Typography variant="h6" sx={{ flexGrow: 1 }}>
                          {app.name}
                        </Typography>
                        <LaunchIcon fontSize="small" color="action" />
                      </Stack>
                      {app.description && (
                        <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                          {app.description}
                        </Typography>
                      )}
                    </CardContent>
                  </CardActionArea>
                </Card>
              ))}
            </Stack>
          </Box>

          {isAdmin && (
            <>
              <Divider />
              <AdminSection />
            </>
          )}
        </Stack>
      </Container>
    </Box>
  )
}
