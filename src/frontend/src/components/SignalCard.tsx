import { useState } from 'react'
import { formatTimestamp, formatValue } from '../lib/format'

/**
 * The subset of finch's `Device` / `OphydDevice` this card renders. Declared
 * locally because finch exports the PV-flavoured type but not the device one,
 * and the two are structurally identical for our purposes.
 */
export interface LiveSignal {
  value: string | number | boolean
  timestamp: number
  connected: boolean
  read_access: boolean
  write_access: boolean | null
  units?: string
  precision?: number
  enum_strs?: string[] | null
  min?: number | null
  max?: number | null
}

export interface SignalCardProps {
  /** Registry device name or PV name -- whatever the socket is keyed by. */
  name: string
  signal: LiveSignal | undefined
  onSet: (name: string, value: string) => void
  onRemove: (name: string) => void
  /** Hide the unsubscribe button for cards that are part of a fixed panel. */
  removable?: boolean
}

/**
 * One live signal: current value, connection state, and a setpoint input.
 * Shared by the device socket and the arbitrary-PV socket, since the finch
 * hooks hand back the same shape for both.
 */
export default function SignalCard({
  name,
  signal,
  onSet,
  onRemove,
  removable = true,
}: SignalCardProps) {
  const [draft, setDraft] = useState('')

  const connected = signal?.connected ?? false
  // Composite devices report write_access as null (it is a per-signal notion),
  // so only an explicit `false` locks the input down.
  const writable = signal != null && signal.write_access !== false
  const enumStrings = signal?.enum_strs ?? null

  // /pv-socket subscribes without string=True, so enum PVs arrive as their
  // integer index. Resolve it against enum_strs for display and highlighting.
  const rawValue = signal?.value
  const displayValue =
    enumStrings && typeof rawValue === 'number' ? (enumStrings[rawValue] ?? rawValue) : rawValue

  const submit = (value: string) => {
    if (value === '') return
    onSet(name, value)
    setDraft('')
  }

  return (
    <li className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate font-mono text-sm font-semibold text-slate-800">{name}</p>
          <p className="text-xs text-slate-400">updated {formatTimestamp(signal?.timestamp ?? 0)}</p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <span
            title={connected ? 'connected' : 'disconnected'}
            className={`h-2.5 w-2.5 rounded-full ${connected ? 'bg-emerald-500' : 'bg-slate-300'}`}
          />
          {removable && (
            <button
              type="button"
              onClick={() => onRemove(name)}
              className="rounded px-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
              aria-label={`Unsubscribe from ${name}`}
            >
              ✕
            </button>
          )}
        </div>
      </div>

      <p className="mt-3 flex items-baseline gap-1.5">
        <span className="font-mono text-2xl tabular-nums text-slate-900">
          {formatValue(displayValue, signal?.precision)}
        </span>
        {signal?.units && <span className="text-sm text-slate-500">{signal.units}</span>}
      </p>

      {/* EPICS reports 0/0 for "no limits configured"; don't render that. */}
      {signal?.min != null && signal?.max != null && signal.min !== signal.max && (
        <p className="mt-1 font-mono text-xs text-slate-400">
          limits {formatValue(signal?.min)} … {formatValue(signal?.max)}
        </p>
      )}

      {enumStrings && enumStrings.length > 0 ? (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {enumStrings.map((option: string) => (
            <button
              key={option}
              type="button"
              disabled={!writable}
              onClick={() => submit(option)}
              className={`rounded border px-2 py-1 text-xs transition
                ${
                  String(displayValue) === option
                    ? 'border-sky-500 bg-sky-50 text-sky-700'
                    : 'border-slate-200 text-slate-600 hover:bg-slate-50'
                }
                disabled:cursor-not-allowed disabled:opacity-40`}
            >
              {option}
            </button>
          ))}
        </div>
      ) : (
        <form
          className="mt-3 flex gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            submit(draft)
          }}
        >
          <input
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder={writable ? 'new value' : 'read only'}
            disabled={!writable}
            className="min-w-0 flex-1 rounded border border-slate-200 px-2 py-1 text-sm
              focus:border-sky-400 focus:outline-none disabled:bg-slate-50 disabled:text-slate-400"
          />
          <button
            type="submit"
            disabled={!writable}
            className="rounded bg-sky-600 px-3 py-1 text-sm font-medium text-white
              hover:bg-sky-700 disabled:cursor-not-allowed disabled:bg-slate-200"
          >
            Set
          </button>
        </form>
      )}
    </li>
  )
}
