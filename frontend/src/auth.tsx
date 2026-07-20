import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { onUnauthorized } from './api'
import {
  beginLogin,
  currentClaims,
  getAccessToken,
  hasSession,
  logout as oidcLogout,
} from './oidc'
import type { User } from './types'

interface AuthContextValue {
  user: User | null
  loading: boolean
  /** Redirects to Keycloak (never resolves in this document). */
  login: () => Promise<void>
  logout: () => void
}

function userFromClaims(): User | null {
  const claims = currentClaims()
  if (!claims) return null
  return {
    username: claims.username,
    role: claims.roles.includes('admin') ? 'admin' : '',
  }
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (!hasSession()) {
      setLoading(false)
      return
    }
    // Refresh if stale; a dead session yields null and the user stays logged
    // out. A transient Keycloak outage keeps the stored session for retry.
    getAccessToken()
      .then(() => setUser(userFromClaims()))
      .finally(() => setLoading(false))
  }, [])

  // A mid-session 401 (revoked/expired beyond refresh) clears tokens in
  // api.ts; dropping the user here routes back to the sign-in page.
  useEffect(() => onUnauthorized(() => setUser(null)), [])

  const login = useCallback(async () => {
    await beginLogin('/')
  }, [])

  const logout = useCallback(() => {
    oidcLogout()
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
