import { TIFFCanvas, useOphydPVSocket } from '@blueskyproject/finch'
import SignalCard from './SignalCard'
import StreamSettingsForm, { type StreamField } from './StreamSettingsForm'
import { fileWriterPvs, stripColon, streamWsUrl, useStreamSettings } from '../lib/streamSettings'

export interface TIFFPanelProps {
  /** Default TIFF detector PV prefix without the trailing colon, e.g. `SIMTIFF1`. */
  defaultPrefix: string
}

const FIELDS: StreamField[] = [
  { name: 'prefix', label: 'Detector prefix', placeholder: '13PIL1' },
  { name: 'plugin', label: 'TIFF plugin', placeholder: 'TIFF1' },
  {
    name: 'pv',
    label: 'Exact file path PV (optional)',
    placeholder: '13PIL1:TIFF1:FullFileName_RBV',
    hint: 'Overrides prefix + plugin',
  },
]

/**
 * Most-recent-saved-TIFF viewer.
 *
 * `TIFFCanvas` opens a `/tiff-socket` connection; the PV settings ride on the
 * socket URL's query string and the server watches
 * `<prefix>:<plugin>:FullFileName_RBV` (or the exact PV). Whenever that path
 * changes, the server decodes the file and pushes it here as JPEG. The cards
 * beside it surface the file-writer readbacks over the ordinary PV socket.
 */
export default function TIFFPanel({ defaultPrefix }: TIFFPanelProps) {
  const { settings, generation, apply, reset } = useStreamSettings('tiff', {
    prefix: defaultPrefix,
    plugin: 'TIFF1',
    pv: '',
  })

  const statusPvs = fileWriterPvs(settings)
  const { devices, handleSetValueRequest } = useOphydPVSocket(statusPvs)

  return (
    <div>
      <StreamSettingsForm fields={FIELDS} settings={settings} onApply={apply} onReset={reset} />

      <div className="flex flex-wrap items-start gap-6">
        <TIFFCanvas
          key={generation}
          prefix={stripColon(settings.prefix)}
          canvasSize="medium"
          wsUrl={streamWsUrl('tiff-socket', settings)}
        />

        <ul className="grid min-w-80 flex-1 gap-4 sm:grid-cols-2">
          {statusPvs.map((pv) => (
            <SignalCard
              key={pv}
              name={pv}
              signal={devices[pv]}
              onSet={handleSetValueRequest}
              /* Read-only file-writer status, not an ad-hoc subscription list. */
              onRemove={() => {}}
              removable={false}
            />
          ))}
        </ul>
      </div>
    </div>
  )
}
