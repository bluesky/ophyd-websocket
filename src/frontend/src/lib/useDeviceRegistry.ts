import { useCallback, useEffect, useState } from 'react'
import { ophydApiUrl } from './config'

interface DevicesResponse {
  devices: string[]
  count: number
  message: string
}

/** Polls the ophyd-websocket REST API for the names in its device registry. */
export function useDeviceRegistry() {
  const [devices, setDevices] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const response = await fetch(`${ophydApiUrl}/devices`)
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}`)
      const body: DevicesResponse = await response.json()
      setDevices(body.devices ?? [])
      setError(null)
    } catch (cause) {
      setDevices([])
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  return { devices, error, loading, refresh }
}
