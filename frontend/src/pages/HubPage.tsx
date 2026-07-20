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
import { fetchApps } from '../api'
import { useAuth } from '../auth'
import { ColorModeToggle } from '../components/ColorModeToggle'
import { config } from '../oidc'

/** Users, roles, and credentials are managed in Keycloak now — the hub links
 * admins to its console instead of embedding admin panels. */
function keycloakConsoleUrl(): string {
  const issuer = config().oidcIssuer
  const [base, realm] = issuer.split('/realms/')
  return realm ? `${base}/admin/${realm}/console/` : issuer
}

export function HubPage() {
  const { user, logout } = useAuth()
  const apps = fetchApps()

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
            {apps.length === 0 && (
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
              <Box>
                <Typography variant="h5" sx={{ mb: 2 }}>
                  Administration
                </Typography>
                <Card variant="outlined" sx={{ maxWidth: 420 }}>
                  <CardActionArea
                    component="a"
                    href={keycloakConsoleUrl()}
                    target="_blank"
                    rel="noopener"
                  >
                    <CardContent>
                      <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
                        <Typography variant="h6" sx={{ flexGrow: 1 }}>
                          Keycloak console
                        </Typography>
                        <LaunchIcon fontSize="small" color="action" />
                      </Stack>
                      <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                        Manage users, roles, and service clients for this realm.
                      </Typography>
                    </CardContent>
                  </CardActionArea>
                </Card>
              </Box>
            </>
          )}
        </Stack>
      </Container>
    </Box>
  )
}
