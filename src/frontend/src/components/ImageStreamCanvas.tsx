import { useImageSocket, type ImageSocketPath } from '../lib/hooks/useImageSocket'

const CANVAS_SIZES = { small: 256, medium: 512, large: 1024 } as const

export interface ImageStreamCanvasProps {
  path: ImageSocketPath
  /** The socket's init message; see useImageSocket. */
  init: Record<string, string>
  apiKey: string | null
  canvasSize?: keyof typeof CANVAS_SIZES
}

/**
 * Live JPEG stream from /camera-socket or /tiff-socket, with fps, connection
 * state and a log-scale toggle. Stands in for finch's CameraCanvas/TIFFCanvas
 * until those can authenticate.
 */
export default function ImageStreamCanvas({
  path,
  init,
  apiKey,
  canvasSize = 'medium',
}: ImageStreamCanvasProps) {
  const { canvasRef, status, error, fps, logScale, toggleLogScale, reconnect, disconnect } =
    useImageSocket({ path, init, apiKey })
  const size = CANVAS_SIZES[canvasSize]

  return (
    <div className="flex flex-col gap-2" style={{ width: size }}>
      <div className="relative border border-slate-200 bg-slate-300">
        <canvas
          ref={canvasRef}
          width={size}
          height={size}
          className={`block ${status === 'open' && !error ? '' : 'opacity-25'}`}
        />
        <span className="absolute left-2 top-1 rounded bg-white/70 px-1 font-mono text-xs text-slate-700">
          {fps.toFixed(1)} fps
        </span>
        {(status !== 'open' || error) && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 p-6 text-center">
            <p className="text-lg font-semibold text-slate-700">
              {status === 'connecting' ? 'Connecting…' : status === 'open' ? 'Stream error' : 'Disconnected'}
            </p>
            {error && <p className="text-sm text-rose-700">{error}</p>}
          </div>
        )}
      </div>

      <div className="flex items-center gap-2 text-xs">
        <button
          type="button"
          onClick={status === 'closed' ? reconnect : disconnect}
          className="rounded border border-slate-200 bg-white px-2 py-1 text-slate-600 hover:bg-slate-50"
        >
          {status === 'closed' ? 'Connect' : 'Disconnect'}
        </button>
        <button
          type="button"
          onClick={toggleLogScale}
          disabled={status !== 'open'}
          className="rounded border border-slate-200 bg-white px-2 py-1 text-slate-600 hover:bg-slate-50
            disabled:cursor-not-allowed disabled:opacity-40"
        >
          Log scale: {logScale ? 'on' : 'off'}
        </button>
        <span className="font-mono text-slate-400">/{path}</span>
      </div>
    </div>
  )
}
