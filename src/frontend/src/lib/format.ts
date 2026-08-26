/** Display helpers shared by the device and PV panels. */

export function formatValue(value: unknown, precision?: number | null): string {
  if (value === null || value === undefined || value === '') return '—'

  if (Array.isArray(value)) {
    const head = value
      .slice(0, 3)
      .map((entry) => formatValue(entry, precision))
      .join(', ')
    return `[${head}${value.length > 3 ? `, … ${value.length} pts` : ''}]`
  }

  if (typeof value === 'number') {
    if (Number.isInteger(value)) return String(value)
    return value.toFixed(precision ?? 3)
  }

  return String(value)
}

export function formatTimestamp(timestamp: number): string {
  if (!timestamp) return 'never'
  return new Date(timestamp * 1000).toLocaleTimeString()
}
