import { useCallback, useEffect, useState } from 'react'
import { authHeaders } from './apiKey'
import { ophydApiUrl } from './config'

/** `GET /api/v1/auth/permissions`, as ophyd-websocket returns it. */
export interface AuthPermissions {
  authEnabled: boolean
  read: boolean
  write: boolean
  console: boolean
  roles: string[]
}

export type AuthStatus =
  | { state: 'loading' }
  | { state: 'ok'; permissions: AuthPermissions }
  /** 401: missing or rejected key. Only possible with auth enabled. */
  | { state: 'unauthorized'; message: string }
  /** Server unreachable, or it couldn't check the key (503). */
  | { state: 'error'; message: string }

/**
 * What `apiKey` may do on the server, re-fetched whenever the key changes.
 * With auth disabled the server answers with every permission granted, so
 * callers can treat `permissions.write` as the single source of truth.
 */
export function useAuthPermissions(apiKey: string | null) {
  const [status, setStatus] = useState<AuthStatus>({ state: 'loading' })

  const refresh = useCallback(async () => {
    setStatus({ state: 'loading' })
    try {
      const response = await fetch(`${ophydApiUrl}/auth/permissions`, {
        headers: authHeaders(apiKey),
      })
      const body = await response.json().catch(() => ({}))
      if (response.status === 401) {
        setStatus({ state: 'unauthorized', message: body.detail ?? 'invalid or missing API key' })
      } else if (!response.ok) {
        setStatus({ state: 'error', message: body.detail ?? `${response.status} ${response.statusText}` })
      } else {
        setStatus({ state: 'ok', permissions: body as AuthPermissions })
      }
    } catch (cause) {
      setStatus({ state: 'error', message: cause instanceof Error ? cause.message : String(cause) })
    }
  }, [apiKey])

  useEffect(() => {
    void refresh()
  }, [refresh])

  return { status, refresh }
}
