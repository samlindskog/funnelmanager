import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { apiLogout, fetchMe, getToken, login as apiLogin, onUnauthorized, setToken } from './api'
import type { User } from './types'

interface AuthContextValue {
  user: User | null
  loading: boolean
  login: (username: string, password: string) => Promise<void>
  logout: () => void
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const token = getToken()
    if (!token) {
      setLoading(false)
      return
    }
    fetchMe()
      .then(setUser)
      .catch(() => setToken(null))
      .finally(() => setLoading(false))
  }, [])

  // A mid-session 401 (expired/revoked session) clears the token in api.ts;
  // clearing the user here makes ProtectedRoute redirect to login instead of
  // leaving a dead session where every action fails.
  useEffect(() => onUnauthorized(() => setUser(null)), [])

  const login = useCallback(async (username: string, password: string) => {
    await apiLogin(username, password)
    const me = await fetchMe()
    setUser(me)
  }, [])

  const logout = useCallback(() => {
    // Revoke the server-side session (best-effort) before dropping local state.
    void apiLogout()
    setToken(null)
    setUser(null)
  }, [])

  const value = useMemo(
    () => ({ user, loading, login, logout }),
    [user, loading, login, logout],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
