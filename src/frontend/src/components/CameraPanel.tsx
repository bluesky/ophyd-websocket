import { useOphydPVSocket } from '../lib/hooks/useOphydSocket'
import ImageStreamCanvas from './ImageStreamCanvas'
import SignalCard from './SignalCard'
import SocketNotice from './SocketNotice'

export interface CameraPanelProps {
  /** Detector PV prefix without the trailing colon, e.g. `SIMDET1`. */
  prefix: string
  apiKey: string | null
  canWrite: boolean
}

/**
 * Live image stream from the simulated areaDetector.
 *
 * The canvas opens its own `/camera-socket` connection and negotiates the
 * frame geometry from `<prefix>:cam1:{MinX,MinY,SizeX,SizeY,ColorMode,DataType}`,
 * the same init message finch's `CameraCanvas` sends. The cards beside it
 * drive the detector over the ordinary PV socket.
 */
export default function CameraPanel({ prefix, apiKey, canWrite }: CameraPanelProps) {
  const controlPvs = [
    `${prefix}:cam1:Acquire`,
    `${prefix}:cam1:SimMode`,
    `${prefix}:cam1:ColorMode`,
    `${prefix}:cam1:AcquirePeriod`,
    `${prefix}:cam1:ArrayRate_RBV`,
    `${prefix}:cam1:ArrayCounter_RBV`,
  ]

  const cameraInit = {
    imageArray_PV: `${prefix}:image1:ArrayData`,
    startX: `${prefix}:cam1:MinX`,
    startY: `${prefix}:cam1:MinY`,
    sizeX: `${prefix}:cam1:SizeX`,
    sizeY: `${prefix}:cam1:SizeY`,
    colorMode: `${prefix}:cam1:ColorMode`,
    dataType: `${prefix}:cam1:DataType`,
  }

  const { devices, handleSetValueRequest, authError, lastError } = useOphydPVSocket(controlPvs, {
    apiKey,
  })

  return (
    <div className="flex flex-wrap items-start gap-6">
      <ImageStreamCanvas path="camera-socket" init={cameraInit} apiKey={apiKey} />

      <div className="min-w-80 flex-1">
        <SocketNotice authError={authError} lastError={lastError} />
        <ul className="grid gap-4 sm:grid-cols-2">
          {controlPvs.map((pv) => (
            <SignalCard
              key={pv}
              name={pv}
              signal={devices[pv]}
              onSet={handleSetValueRequest}
              /* These cards are the detector's fixed controls, not an
                 ad-hoc subscription list, so removal is a no-op. */
              onRemove={() => {}}
              removable={false}
              canWrite={canWrite}
            />
          ))}
        </ul>
      </div>
    </div>
  )
}
