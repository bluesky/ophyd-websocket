import { useCallback, useEffect, useState } from 'react'
import { ophydApiUrl } from './config'

interface DevicesResponse {
  devices: string[]
  /** The subset of `devices` that are image sources, streamed rather than subscribed. */
  cameras?: string[]
  count: number
  message: string
}

/**
 * Polls the ophyd-websocket REST API for the names in its device registry.
 *
 * `devices` excludes cameras: those go to `/camera-socket-shared`, and the
 * device socket refuses them.
 */
export function useDeviceRegistry() {
  const [devices, setDevices] = useState<string[]>([])
  const [cameras, setCameras] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const response = await fetch(`${ophydApiUrl}/devices`)
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}`)
      const body: DevicesResponse = await response.json()
      const cameraNames = body.cameras ?? []
      setDevices((body.devices ?? []).filter((name) => !cameraNames.includes(name)))
      setCameras(cameraNames)
      setError(null)
    } catch (cause) {
      setDevices([])
      setCameras([])
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  return { devices, cameras, error, loading, refresh }
}
