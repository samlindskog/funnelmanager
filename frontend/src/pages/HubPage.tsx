import LaunchIcon from '@mui/icons-material/Launch'
import LogoutIcon from '@mui/icons-material/Logout'
import {
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
import { fetchAdminApps, fetchApps, filterAvailableApps } from '../api'
import { useAuth } from '../auth'
import { ColorModeToggle } from '../components/ColorModeToggle'
import { config, currentClaims } from '../oidc'
import type { AppLink } from '../types'

/** Users, roles, and credentials are managed in Keycloak now — the hub links
 * admins to its console instead of embedding admin panels. */
function keycloakConsoleUrl(): string {
  const issuer = config().oidcIssuer
  const [base, realm] = issuer.split('/realms/')
  return realm ? `${base}/admin/${realm}/console/` : issuer
}

/** Kebab-case an app/tile name into a stable testid qualifier (e.g. the
 * "Search" tile → `hub-open-search`). Non-alphanumerics collapse to hyphens. */
function slug(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
}

export function HubPage() {
  const { user, logout } = useAuth()
  // null = probes still in flight (tiles render once discovery settles).
  const [apps, setApps] = useState<AppLink[] | null>(null)

  useEffect(() => {
    document.title = 'Funnel Manager'
  }, [])

  useEffect(() => {
    let cancelled = false
    const roles = currentClaims()?.roles ?? []
    filterAvailableApps(fetchApps(), roles).then((available) => {
      if (!cancelled) setApps(available)
    })
    return () => {
      cancelled = true
    }
  }, [])

  if (!user) return null
  const isAdmin = user.role === 'admin'
  const roles = currentClaims()?.roles ?? []

  const adminTiles = [
    {
      name: 'Keycloak console',
      description: 'Manage users, roles, and service clients for this realm.',
      url: keycloakConsoleUrl(),
    },
    ...fetchAdminApps().filter(
      (app) => !app.roles?.length || app.roles.some((role) => roles.includes(role)),
    ),
  ]

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
          data-testid="hub-signout"
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
            {apps?.length === 0 && (
              <Typography color="text.secondary">
                No apps are available to your account.
              </Typography>
            )}
            <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} useFlexGap sx={{ flexWrap: 'wrap' }}>
              {(apps ?? []).map((app) => (
                <Card key={app.name} variant="outlined" sx={{ minWidth: 240, flex: '0 1 280px' }}>
                  <CardActionArea
                    data-testid={`hub-open-${slug(app.name)}`}
                    component="a"
                    href={app.url}
                    target="_blank"
                    rel="noopener"
                  >
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
              <Box>
                <Typography variant="h5" sx={{ mb: 2 }}>
                  Administration
                </Typography>
                <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} useFlexGap sx={{ flexWrap: 'wrap' }}>
                  {adminTiles.map((tile) => (
                    <Card key={tile.name} variant="outlined" sx={{ minWidth: 240, flex: '0 1 280px' }}>
                      <CardActionArea
                        data-testid={`hub-admin-${slug(tile.name)}`}
                        component="a"
                        href={tile.url}
                        target="_blank"
                        rel="noopener"
                      >
                        <CardContent>
                          <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
                            <Typography variant="h6" sx={{ flexGrow: 1 }}>
                              {tile.name}
                            </Typography>
                            <LaunchIcon fontSize="small" color="action" />
                          </Stack>
                          {tile.description && (
                            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                              {tile.description}
                            </Typography>
                          )}
                        </CardContent>
                      </CardActionArea>
                    </Card>
                  ))}
                </Stack>
              </Box>
            </>
          )}
        </Stack>
      </Container>
    </Box>
  )
}
