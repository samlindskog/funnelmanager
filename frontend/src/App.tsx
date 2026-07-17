import { CircularProgress, Box } from '@mui/material'
import { CssBaseline, ThemeProvider } from '@mui/material'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { AuthProvider, useAuth } from './auth'
import { LoginPage } from './pages/LoginPage'
import { SearchPage } from './pages/SearchPage'
import { ProgressCircles, ProgressProvider } from './progress'
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
        <ProgressProvider>
          <BrowserRouter>
            <Routes>
              <Route path="/login" element={<LoginPage />} />
              <Route
                path="/"
                element={
                  <ProtectedRoute>
                    <SearchPage />
                  </ProtectedRoute>
                }
              />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </BrowserRouter>
          <ProgressCircles />
        </ProgressProvider>
      </AuthProvider>
    </ThemeProvider>
  )
}
