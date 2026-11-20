const IST_OFFSET_MS = 330 * 60 * 1000

function pad2(value: number): string {
  return String(value).padStart(2, '0')
}

export function formatHourOffset(hour: number): string {
  if (hour === 0) return 'T0'
  return hour > 0 ? `T+${hour}h` : `T${hour}h`
}

export function formatUtcTimestamp(isoUtc: string | null | undefined): string {
  if (!isoUtc) return '[time]'
  const date = new Date(isoUtc)
  if (Number.isNaN(date.getTime())) return '[time]'
  const year = date.getUTCFullYear()
  const month = pad2(date.getUTCMonth() + 1)
  const day = pad2(date.getUTCDate())
  const hours = pad2(date.getUTCHours())
  const minutes = pad2(date.getUTCMinutes())
  return `${year}-${month}-${day} ${hours}:${minutes} UTC`
}

export function formatIstTimestamp(isoUtc: string | null | undefined): string {
  if (!isoUtc) return '[time]'
  const date = new Date(isoUtc)
  if (Number.isNaN(date.getTime())) return '[time]'
  const ist = new Date(date.getTime() + IST_OFFSET_MS)
  const year = ist.getUTCFullYear()
  const month = pad2(ist.getUTCMonth() + 1)
  const day = pad2(ist.getUTCDate())
  const hours = pad2(ist.getUTCHours())
  const minutes = pad2(ist.getUTCMinutes())
  return `${year}-${month}-${day} ${hours}:${minutes} IST`
}

export function formatT0ShortIst(isoUtc: string | null | undefined, hourOffset = 0): string {
  if (!isoUtc) return '[time]'
  const date = new Date(isoUtc)
  if (Number.isNaN(date.getTime())) return '[time]'
  const shifted = new Date(date.getTime() + hourOffset * 3600 * 1000 + IST_OFFSET_MS)
  const hours = pad2(shifted.getUTCHours())
  const minutes = pad2(shifted.getUTCMinutes())
  const day = pad2(shifted.getUTCDate())
  const month = shifted.toLocaleString('en-US', { month: 'short', timeZone: 'UTC' })
  return `${day} ${month} ${hours}:${minutes} IST`
}

export function computeHourFromT0(t0Utc: string | null | undefined, targetUtc: string | null | undefined): number | null {
  if (!t0Utc || !targetUtc) return null
  const t0 = Date.parse(t0Utc)
  const target = Date.parse(targetUtc)
  if (Number.isNaN(t0) || Number.isNaN(target)) return null
  return Math.round((target - t0) / 3600000)
}

/**
 * Formats a nullable numeric metric using bracketed placeholder [n] when missing (P7.4).
 */
export function formatCount(value: number | null | undefined, suffix = ''): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return `[n]${suffix}`
  }
  return `${value}${suffix}`
}

export function formatPercent(share: number | null | undefined): string {
  if (share === null || share === undefined || Number.isNaN(share)) {
    return '[n]%'
  }
  const pct = share <= 1 ? Math.round(share * 100) : Math.round(share)
  return `${pct}%`
}

export function formatPlatformLabel(platform: string): string {
  switch (platform.toLowerCase()) {
    case 'x':
      return 'X'
    case 'hn':
      return 'HN'
    case 'youtube':
      return 'YouTube'
    case 'gdelt':
    case 'news':
      return 'News'
    case 'web':
    case 'firecrawl':
      return 'Web'
    default:
      return platform.toUpperCase()
  }
}

export function truncateExcerpt(text: string | null | undefined, maxChars = 300): string {
  if (!text) return ''
  if (text.length <= maxChars) return text
  return `${text.slice(0, Math.max(0, maxChars - 1))}…`
}

export function formatLagSeconds(lagSeconds: number | null | undefined): string {
  if (lagSeconds === null || lagSeconds === undefined || Number.isNaN(lagSeconds)) {
    return '[n]s'
  }
  if (lagSeconds < 60) return `${Math.round(lagSeconds)}s`
  const mins = Math.floor(lagSeconds / 60)
  const rem = Math.round(lagSeconds % 60)
  return rem > 0 ? `${mins}m ${rem}s` : `${mins}m`
}

export function formatBriefPlaceholder(value: string | null | undefined): string {
  if (!value || value.trim().length === 0) {
    return '[n]'
  }
  return value
}
