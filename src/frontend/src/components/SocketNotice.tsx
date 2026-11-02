export interface SocketNoticeProps {
  /** Why the socket was refused or closed (wrong/missing key). */
  authError: string | null
  /** Latest refused request, e.g. a write without permission. */
  lastError: string | null
}

/** Auth and write errors reported by one of the ophyd sockets. */
export default function SocketNotice({ authError, lastError }: SocketNoticeProps) {
  if (authError) {
    return (
      <p className="mb-3 rounded bg-amber-50 p-2 text-xs text-amber-800">
        Socket not authenticated: {authError}. Set an API key in the sidebar.
      </p>
    )
  }
  if (lastError) {
    return <p className="mb-3 rounded bg-rose-50 p-2 text-xs text-rose-700">{lastError}</p>
  }
  return null
}
