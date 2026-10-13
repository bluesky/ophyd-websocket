import { useEffect, useState, type FormEvent } from 'react'
import type { StreamSettings } from '../lib/streamSettings'

export interface StreamField {
  name: string
  label: string
  placeholder?: string
  /** One-line hint shown under the input. */
  hint?: string
}

export interface StreamSettingsFormProps {
  fields: StreamField[]
  settings: StreamSettings
  onApply: (next: StreamSettings) => void
  onReset: () => void
}

/**
 * PV inputs for a stream panel. Edits are drafts until "Connect" so the
 * canvas doesn't reconnect on every keystroke.
 */
export default function StreamSettingsForm({
  fields,
  settings,
  onApply,
  onReset,
}: StreamSettingsFormProps) {
  const [draft, setDraft] = useState<StreamSettings>(settings)

  // Follow external changes (e.g. "Reset to sample").
  useEffect(() => setDraft(settings), [settings])

  const dirty = fields.some((f) => (draft[f.name] ?? '') !== (settings[f.name] ?? ''))

  const submit = (event: FormEvent) => {
    event.preventDefault()
    const next: StreamSettings = {}
    for (const field of fields) next[field.name] = (draft[field.name] ?? '').trim()
    onApply(next)
  }

  return (
    <form
      onSubmit={submit}
      className="mb-4 flex flex-wrap items-end gap-3 rounded border border-slate-200 bg-white p-3"
    >
      {fields.map((field) => (
        <label key={field.name} className="flex min-w-40 flex-1 flex-col gap-1 text-xs text-slate-500">
          {field.label}
          <input
            type="text"
            value={draft[field.name] ?? ''}
            placeholder={field.placeholder}
            onChange={(event) => setDraft((d) => ({ ...d, [field.name]: event.target.value }))}
            spellCheck={false}
            autoComplete="off"
            className="rounded border border-slate-300 px-2 py-1 font-mono text-sm text-slate-900 focus:border-sky-500 focus:outline-none"
          />
          {field.hint && <span className="text-[11px] text-slate-400">{field.hint}</span>}
        </label>
      ))}
      <div className="flex gap-2">
        <button
          type="submit"
          className={`rounded px-3 py-1 text-sm font-medium text-white ${
            dirty ? 'bg-sky-600 hover:bg-sky-700' : 'bg-slate-400 hover:bg-slate-500'
          }`}
          title={dirty ? 'Reconnect with these PVs' : 'Reconnect'}
        >
          Connect
        </button>
        <button
          type="button"
          onClick={onReset}
          className="rounded border border-slate-200 px-3 py-1 text-sm text-slate-600 hover:bg-slate-50"
          title="Back to the sample caproto IOC defaults"
        >
          Reset to sample
        </button>
      </div>
    </form>
  )
}
