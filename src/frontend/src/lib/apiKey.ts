import { useSyncExternalStore } from 'react'

/**
 * The Queue Server API key this browser sends to ophyd-websocket, persisted in
 * localStorage so it survives reloads.
 *
 * Every component reads it through `useApiKey()`, so saving a new key
 * reconnects every socket at once. Storage access is wrapped because it throws
 * in some private-browsing modes; the app then simply forgets the key on reload.
 */
const STORAGE_KEY = 'ophyd-websocket.apiKey'

function readStoredKey(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY) || null
  } catch {
    return null
  }
}

let currentKey: string | null = readStoredKey()
const listeners = new Set<() => void>()

function notify() {
  listeners.forEach((listener) => listener())
}

/** Saves (or with `null`, forgets) the API key and updates every subscriber. */
export function setApiKey(key: string | null) {
  const trimmed = key?.trim() || null
  currentKey = trimmed
  try {
    if (trimmed) window.localStorage.setItem(STORAGE_KEY, trimmed)
    else window.localStorage.removeItem(STORAGE_KEY)
  } catch {
    // Not persisted; still applies for this page load.
  }
  notify()
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  // Keep other tabs in sync when one of them saves a key.
  const onStorage = (event: StorageEvent) => {
    if (event.key !== STORAGE_KEY) return
    currentKey = event.newValue || null
    notify()
  }
  window.addEventListener('storage', onStorage)
  return () => {
    listeners.delete(listener)
    window.removeEventListener('storage', onStorage)
  }
}

/** The saved API key, or `null` when none is set. */
export function useApiKey(): string | null {
  return useSyncExternalStore(subscribe, () => currentKey)
}

/** `Authorization` header for ophyd-websocket REST calls (empty without a key). */
export function authHeaders(apiKey: string | null): Record<string, string> {
  return apiKey ? { Authorization: `ApiKey ${apiKey}` } : {}
}

/**
 * DEMO keys injected by docker-compose-auth.yml (qserver/demo-api-keys.env),
 * offered as one-click buttons. Absent in the plain docker-compose.yml stack.
 */
export const demoApiKeys: { label: string; key: string }[] = [
  { label: 'Demo operator', key: import.meta.env.VITE_DEMO_OPERATOR_API_KEY ?? '' },
  { label: 'Demo observer', key: import.meta.env.VITE_DEMO_OBSERVER_API_KEY ?? '' },
].filter((demo) => demo.key)

/** First 8 characters, for showing which key is active without revealing it. */
export function maskApiKey(key: string): string {
  return `${key.slice(0, 8)}…`
}
