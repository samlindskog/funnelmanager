import { CircularProgress, Box } from '@mui/material'
import { CssBaseline, ThemeProvider } from '@mui/material'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { AuthProvider, useAuth } from './auth'
import { CallbackPage } from './pages/CallbackPage'
import { HubPage } from './pages/HubPage'
import { LandingPage } from './pages/LandingPage'
import { theme } from './theme'

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth()
  if (loading) {
    return (
      <Box sx={{ minHeight: '100vh', display: 'grid', placeItems: 'center' }}>
        <CircularProgress />
      </Box>
    )
  }
  if (!user) return <Navigate to="/login" replace />
  return children
}

export default function App() {
  return (
    <ThemeProvider theme={theme} defaultMode="system" forceThemeRerender>
      <CssBaseline />
      <AuthProvider>
        <BrowserRouter>
          <Routes>
            <Route path="/login" element={<LandingPage />} />
            <Route path="/callback" element={<CallbackPage />} />
            <Route
              path="/"
              element={
                <ProtectedRoute>
                  <HubPage />
                </ProtectedRoute>
              }
            />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </BrowserRouter>
      </AuthProvider>
    </ThemeProvider>
  )
}
