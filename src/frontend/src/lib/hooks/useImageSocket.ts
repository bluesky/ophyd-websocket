import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ophydWsUrl } from '../config'

/**
 * Auth-aware replacement for the socket logic inside finch's `CameraCanvas` /
 * `TIFFCanvas`, which send their init message first and so can't talk to an
 * auth-enabled server. Temporary until finch supports an API key.
 *
 * Protocol (/camera-socket and /tiff-socket): optional auth message, then one
 * JSON init message, then the server streams JPEG frames as binary messages.
 * Text messages are settings, `{"logNormalization": bool}`, or `{"error"}`.
 */
export type ImageSocketPath = 'camera-socket' | 'tiff-socket'
export type ImageSocketStatus = 'connecting' | 'open' | 'closed'

export interface UseImageSocketOptions {
  path: ImageSocketPath
  /** The socket's init message, e.g. `{ prefix }` for /tiff-socket. */
  init: Record<string, string>
  apiKey?: string | null
}

export function useImageSocket({ path, init, apiKey }: UseImageSocketOptions) {
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const memoizedInit = useMemo(() => init, [JSON.stringify(init)])
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const [status, setStatus] = useState<ImageSocketStatus>('closed')
  const [error, setError] = useState<string | null>(null)
  const [fps, setFps] = useState(0)
  const [logScale, setLogScale] = useState(true)
  // Bumped to reconnect on demand.
  const [generation, setGeneration] = useState(0)

  const reconnect = useCallback(() => setGeneration((n) => n + 1), [])
  const disconnect = useCallback(() => wsRef.current?.close(), [])
  const toggleLogScale = useCallback(() => {
    wsRef.current?.send(JSON.stringify({ toggleLogNormalization: !logScale }))
  }, [logScale])

  useEffect(() => {
    setStatus('connecting')
    setError(null)
    setFps(0)

    const ws = new WebSocket(`${ophydWsUrl}/${path}`)
    wsRef.current = ws
    let initSent = false
    const sendInit = () => {
      if (initSent) return
      initSent = true
      ws.send(JSON.stringify(memoizedInit))
    }

    // Decode off the message handler; draw only the newest frame per animation frame.
    let pending: ImageBitmap | null = null
    let frames = 0
    let windowStart = performance.now()
    let animation = requestAnimationFrame(function draw() {
      const canvas = canvasRef.current
      if (pending && canvas) {
        canvas.getContext('2d')?.drawImage(pending, 0, 0, canvas.width, canvas.height)
        pending.close()
        pending = null
      }
      animation = requestAnimationFrame(draw)
    })

    ws.onopen = () => {
      setStatus('open')
      if (apiKey) ws.send(JSON.stringify({ action: 'auth', api_key: apiKey }))
      else sendInit()
    }

    ws.onmessage = async (event) => {
      if (typeof event.data !== 'string') {
        try {
          const bitmap = await createImageBitmap(new Blob([event.data], { type: 'image/jpeg' }))
          pending?.close()
          pending = bitmap
        } catch (cause) {
          setError(`Could not decode frame: ${cause instanceof Error ? cause.message : cause}`)
          return
        }
        frames += 1
        const elapsed = (performance.now() - windowStart) / 1000
        if (elapsed >= 1) {
          setFps(frames / elapsed)
          frames = 0
          windowStart = performance.now()
        }
        return
      }

      let message: Record<string, unknown>
      try {
        message = JSON.parse(event.data)
      } catch {
        return
      }
      if (message.action === 'auth') {
        if (message.ok) sendInit()
        else setError(String(message.error ?? 'authentication failed'))
      } else if ('logNormalization' in message) {
        setLogScale(Boolean(message.logNormalization))
      } else if (typeof message.error === 'string') {
        setError(message.error)
      }
    }

    ws.onclose = (event) => {
      setStatus('closed')
      setFps(0)
      if (event.code === 1008 || event.code === 1013) {
        setError((current) => current ?? (event.reason || 'authentication failed'))
      }
    }

    return () => {
      cancelAnimationFrame(animation)
      pending?.close()
      ws.close()
      if (wsRef.current === ws) wsRef.current = null
    }
  }, [path, memoizedInit, apiKey, generation])

  return { canvasRef, status, error, fps, logScale, toggleLogScale, reconnect, disconnect }
}
