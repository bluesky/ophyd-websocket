import { TIFFCanvas, useOphydPVSocket } from '@blueskyproject/finch'
import SignalCard from './SignalCard'

export interface TIFFPanelProps {
  /** TIFF detector PV prefix without the trailing colon, e.g. `SIMTIFF1`. */
  prefix: string
}

/**
 * Most-recent-saved-TIFF viewer for the simulated TIFF-writer detector.
 *
 * `TIFFCanvas` opens its own `/tiff-socket` connection, sends `{ prefix }`, and
 * the server watches `<prefix>:TIFF1:FullFileName_RBV`. Whenever that path PV
 * changes, the server loads the file from disk, encodes it to JPEG, and pushes
 * it here — so all the canvas needs is the prefix. The cards beside it surface
 * the file-writer readbacks over the ordinary PV socket.
 */
export default function TIFFPanel({ prefix }: TIFFPanelProps) {
  const statusPvs = [
    `${prefix}:TIFF1:FullFileName_RBV`,
    `${prefix}:TIFF1:FileNumber_RBV`,
    `${prefix}:TIFF1:NumCaptured_RBV`,
  ]

  const { devices, handleSetValueRequest } = useOphydPVSocket(statusPvs)

  return (
    <div className="flex flex-wrap items-start gap-6">
      <TIFFCanvas prefix={prefix} canvasSize="medium" />

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
  )
}
