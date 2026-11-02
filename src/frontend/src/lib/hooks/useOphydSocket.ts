import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { LiveSignal } from '../../components/SignalCard'
import { ophydWsUrl } from '../config'

/**
 * Drop-in stand-ins for finch's `useOphydPVSocket` / `useOphydDeviceSocket`
 * that can authenticate. Same arguments and return shape, plus an `apiKey`
 * option and `permissions` / `authError` / `lastError` results.
 *
 * Temporary: once finch's hooks send the auth message these can go. The
 * protocol follows react_examples/useOphydPVSocket.tsx: with a key, the first
 * message is `{"action": "auth", "api_key": ...}` and subscriptions go out only
 * after the server acks it. Every (re)connect authenticates again.
 */

/** What the server says this socket's key may do. */
export interface SocketPermissions {
  read: boolean
  write: boolean
  console: boolean
}

export interface UseOphydSocketOptions {
  /** Queue Server API key. Without one, the socket subscribes straight away. */
  apiKey?: string | null
}

type SignalState = LiveSignal & Record<string, unknown>

interface SocketKind {
  path: 'pv-socket' | 'device-socket'
  /** Field naming the signal, both in our requests and the server's updates. */
  idField: 'pv' | 'device'
  /** Request key that carries the list of names to subscribe to. */
  listField: 'pvs' | 'devices'
  subscribeAction: 'subscribe' | 'subscribeSafely'
}

const PV_SOCKET: SocketKind = {
  path: 'pv-socket',
  idField: 'pv',
  listField: 'pvs',
  subscribeAction: 'subscribeSafely',
}

const DEVICE_SOCKET: SocketKind = {
  path: 'device-socket',
  idField: 'device',
  listField: 'devices',
  subscribeAction: 'subscribe',
}

function useOphydSocket(kind: SocketKind, names: string[], { apiKey }: UseOphydSocketOptions) {
  // Callers usually pass a fresh array each render; key on its contents.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const memoizedNames = useMemo(() => names, [JSON.stringify(names)])
  const [devices, setDevices] = useState<Record<string, SignalState>>({})
  const [permissions, setPermissions] = useState<SocketPermissions | null>(null)
  const [authError, setAuthError] = useState<string | null>(null)
  const [lastError, setLastError] = useState<string | null>(null)
  const wsRef = useRef<WebSocket | null>(null)

  const handleSetValueRequest = useCallback(
    (name: string, value: string | number | boolean) => {
      setLastError(null)
      wsRef.current?.send(JSON.stringify({ action: 'set', [kind.idField]: name, value }))
    },
    [kind.idField],
  )

  useEffect(() => {
    setDevices({})
    setPermissions(null)
    setAuthError(null)
    setLastError(null)
    if (memoizedNames.length === 0) return

    const ws = new WebSocket(`${ophydWsUrl}/${kind.path}`)
    wsRef.current = ws

    let subscribed = false
    const subscribe = () => {
      if (subscribed) return
      subscribed = true
      ws.send(JSON.stringify({ action: kind.subscribeAction, [kind.listField]: memoizedNames }))
    }

    ws.onopen = () => {
      if (apiKey) ws.send(JSON.stringify({ action: 'auth', api_key: apiKey }))
      else subscribe()
    }

    ws.onmessage = (event) => {
      let message: Record<string, unknown>
      try {
        message = JSON.parse(event.data)
      } catch {
        return
      }

      // Also how an auth-enabled server answers a keyless client's subscribe.
      if (message.action === 'auth') {
        if (message.ok) {
          setPermissions(message.permissions as SocketPermissions)
          setAuthError(null)
          subscribe() // no-op on a later re-auth ack
        } else {
          // The server closes the socket after a failed or revoked auth.
          setPermissions(null)
          setAuthError(String(message.error ?? 'authentication failed'))
        }
        return
      }

      const name = message[kind.idField]
      if (typeof name === 'string') {
        setDevices((current) => ({
          ...current,
          [name]: {
            ...current[name],
            ...message,
            // Meta updates carry the control limits under their EPICS names.
            ...(message.sub_type === 'meta'
              ? { min: message.lower_ctrl_limit, max: message.upper_ctrl_limit }
              : {}),
          } as SignalState,
        }))
      } else if (typeof message.error === 'string') {
        // e.g. a refused write: {"error": "insufficient permissions", "code": "forbidden"}
        setLastError(message.code ? `${message.error} (${message.code})` : message.error)
      }
    }

    ws.onclose = (event) => {
      // 1008/1013 are the server's auth rejections (see README "Authentication").
      if (event.code === 1008 || event.code === 1013) {
        setAuthError((current) => current ?? (event.reason || 'authentication failed'))
      }
    }

    return () => {
      ws.close()
      if (wsRef.current === ws) wsRef.current = null
    }
  }, [kind, memoizedNames, apiKey])

  return { devices, handleSetValueRequest, permissions, authError, lastError }
}

/** `/pv-socket`: subscribe to raw EPICS PV names. */
export function useOphydPVSocket(pvs: string[], options: UseOphydSocketOptions = {}) {
  return useOphydSocket(PV_SOCKET, pvs, options)
}

/** `/device-socket`: subscribe to ophyd devices from the server's registry. */
export function useOphydDeviceSocket(devices: string[], options: UseOphydSocketOptions = {}) {
  return useOphydSocket(DEVICE_SOCKET, devices, options)
}
