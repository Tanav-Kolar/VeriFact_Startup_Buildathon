import type {
  ApiErrorBody,
  BeforeAfterResponse,
  BriefResponse,
  CheckResponse,
  ClaimCardData,
  EventDetail,
  EventSummary,
  FrameData,
  ItemDetail,
  StorylineDetail,
  StorylineRow,
  TimingSignal,
  TrustResponse
} from '../types/api'

const DEFAULT_API_BASE_URL = 'http://localhost:8000'
const staticJsonCache = new Map<string, Promise<unknown>>()

export function isStaticMode(): boolean {
  return String(import.meta.env.VITE_STATIC_MODE || '').toLowerCase() === 'true'
}

function getStaticApiBase(): string {
  const base = String(import.meta.env.BASE_URL || '/')
  const normalized = base.endsWith('/') ? base : `${base}/`
  return `${normalized}static-api`
}

export function getApiBaseUrl(): string {
  const configured = import.meta.env.VITE_API_BASE_URL
  if (typeof configured === 'string' && configured.trim().length > 0) {
    return configured.trim().replace(/\/+$/, '')
  }
  return DEFAULT_API_BASE_URL
}

export function getCheckBaseUrl(): string {
  const configured = import.meta.env.VITE_CHECK_BASE_URL
  if (typeof configured === 'string' && configured.trim().length > 0) {
    return configured.trim().replace(/\/+$/, '')
  }
  return getApiBaseUrl()
}

export class ApiClientError extends Error {
  status: number
  code: string

  constructor(status: number, code: string, message: string) {
    super(message)
    this.name = 'ApiClientError'
    this.status = status
    this.code = code
  }
}

async function requestJson<T>(path: string, init?: RequestInit, baseUrl = getApiBaseUrl()): Promise<T> {
  const url = `${baseUrl}${path}`
  const response = await fetch(url, init)
  if (!response.ok) {
    let code = `http_${response.status}`
    let message = response.statusText || 'Request failed'
    try {
      const parsed = (await response.json()) as ApiErrorBody
      if (parsed?.error?.code) code = parsed.error.code
      if (parsed?.error?.message) message = parsed.error.message
    } catch {
      // Non-JSON error body; keep status fallback.
    }
    throw new ApiClientError(response.status, code, message)
  }
  return (await response.json()) as T
}

function fetchStaticJson<T>(relPath: string): Promise<T> {
  const clean = relPath.replace(/^\/+/, '')
  const existing = staticJsonCache.get(clean)
  if (existing) return existing as Promise<T>
  const promise = requestJson<T>(`/${clean}`, undefined, getStaticApiBase()).catch((err) => {
    staticJsonCache.delete(clean)
    throw err
  })
  staticJsonCache.set(clean, promise)
  return promise
}

export function fetchEvents(): Promise<EventSummary[]> {
  if (isStaticMode()) return fetchStaticJson<EventSummary[]>('events.json')
  return requestJson<EventSummary[]>('/events')
}

export function fetchEventDetail(eventId: string): Promise<EventDetail> {
  if (isStaticMode()) {
    return fetchStaticJson<EventDetail>(`events/${encodeURIComponent(eventId)}/detail.json`)
  }
  return requestJson<EventDetail>(`/events/${encodeURIComponent(eventId)}`)
}

export async function fetchFrames(
  eventId: string,
  fromHour?: number,
  toHour?: number
): Promise<FrameData[]> {
  if (isStaticMode()) {
    const all = await fetchStaticJson<FrameData[]>(`events/${encodeURIComponent(eventId)}/frames.json`)
    return all.filter((f) => {
      if (typeof fromHour === 'number' && f.hour < fromHour) return false
      if (typeof toHour === 'number' && f.hour > toHour) return false
      return true
    })
  }
  const params = new URLSearchParams()
  if (typeof fromHour === 'number') params.set('from_hour', String(fromHour))
  if (typeof toHour === 'number') params.set('to_hour', String(toHour))
  const qs = params.toString()
  return requestJson<FrameData[]>(`/events/${encodeURIComponent(eventId)}/frames${qs ? `?${qs}` : ''}`)
}

export async function fetchStorylines(eventId: string, atHour?: number): Promise<StorylineRow[]> {
  if (isStaticMode()) {
    const [baseRows, frames] = await Promise.all([
      fetchStaticJson<StorylineRow[]>(`events/${encodeURIComponent(eventId)}/storylines.json`),
      fetchStaticJson<FrameData[]>(`events/${encodeURIComponent(eventId)}/frames.json`)
    ])
    if (typeof atHour !== 'number') return baseRows

    const stats = new Map<
      string,
      {
        count: number
        firstSeen: { platform: string; hour: number; item_id: string } | null
        platforms: Map<string, number>
      }
    >()

    for (const frame of frames) {
      if (frame.hour > atHour) continue
      for (const node of frame.nodes_added) {
        const sid = node.storyline_id || 'unassigned'
        let entry = stats.get(sid)
        if (!entry) {
          entry = { count: 0, firstSeen: null, platforms: new Map() }
          stats.set(sid, entry)
        }
        entry.count += 1
        if (!entry.firstSeen || node.hour < entry.firstSeen.hour) {
          entry.firstSeen = { platform: node.platform, hour: node.hour, item_id: node.id }
        }
        const prevPlatHour = entry.platforms.get(node.platform)
        if (prevPlatHour === undefined || node.hour < prevPlatHour) {
          entry.platforms.set(node.platform, node.hour)
        }
      }
    }

    return baseRows.map((row) => {
      const entry = stats.get(row.id)
      if (!entry) {
        return {
          ...row,
          count: 0,
          first_seen: null,
          platforms_reached: []
        }
      }
      return {
        ...row,
        count: entry.count,
        first_seen: entry.firstSeen,
        platforms_reached: Array.from(entry.platforms.entries())
          .map(([platform, hour]) => ({ platform, hour }))
          .sort((a, b) => a.hour - b.hour)
      }
    })
  }
  const qs = typeof atHour === 'number' ? `?at_hour=${encodeURIComponent(String(atHour))}` : ''
  return requestJson<StorylineRow[]>(`/events/${encodeURIComponent(eventId)}/storylines${qs}`)
}

export async function fetchStorylineDetail(
  eventId: string,
  storylineId: string,
  atHour?: number
): Promise<StorylineDetail> {
  if (isStaticMode()) {
    const map = await fetchStaticJson<Record<string, StorylineDetail>>(
      `events/${encodeURIComponent(eventId)}/storyline_details.json`
    )
    const detail = map[storylineId]
    if (!detail) {
      throw new ApiClientError(404, 'not_found', `Storyline ${storylineId} not found`)
    }
    return detail
  }
  const qs = typeof atHour === 'number' ? `?at_hour=${encodeURIComponent(String(atHour))}` : ''
  return requestJson<StorylineDetail>(
    `/events/${encodeURIComponent(eventId)}/storylines/${encodeURIComponent(storylineId)}${qs}`
  )
}

export async function fetchItemDetail(eventId: string, itemId: string): Promise<ItemDetail> {
  if (isStaticMode()) {
    const map = await fetchStaticJson<Record<string, ItemDetail>>(
      `events/${encodeURIComponent(eventId)}/items.json`
    )
    const item = map[itemId]
    if (!item) {
      throw new ApiClientError(404, 'not_found', `Item ${itemId} not found`)
    }
    return item
  }
  return requestJson<ItemDetail>(
    `/events/${encodeURIComponent(eventId)}/items/${encodeURIComponent(itemId)}`
  )
}

export async function fetchClaimCard(
  eventId: string,
  claimId: string,
  atHour?: number
): Promise<ClaimCardData> {
  if (isStaticMode()) {
    const map = await fetchStaticJson<Record<string, ClaimCardData>>(
      `events/${encodeURIComponent(eventId)}/claims.json`
    )
    const card = map[claimId]
    if (!card) {
      throw new ApiClientError(404, 'not_found', `Claim ${claimId} not found`)
    }
    return card
  }
  const qs = typeof atHour === 'number' ? `?at_hour=${encodeURIComponent(String(atHour))}` : ''
  return requestJson<ClaimCardData>(
    `/events/${encodeURIComponent(eventId)}/claims/${encodeURIComponent(claimId)}${qs}`
  )
}

export function fetchSignals(eventId: string): Promise<TimingSignal[]> {
  if (isStaticMode()) {
    return fetchStaticJson<TimingSignal[]>(`events/${encodeURIComponent(eventId)}/signals.json`)
  }
  return requestJson<TimingSignal[]>(`/events/${encodeURIComponent(eventId)}/signals`)
}

export function fetchBeforeAfter(eventId: string, turningPointId?: string): Promise<BeforeAfterResponse> {
  if (isStaticMode()) {
    return fetchStaticJson<BeforeAfterResponse>(
      `events/${encodeURIComponent(eventId)}/before_after.json`
    )
  }
  const qs = turningPointId ? `?turning_point=${encodeURIComponent(turningPointId)}` : ''
  return requestJson<BeforeAfterResponse>(`/events/${encodeURIComponent(eventId)}/before-after${qs}`)
}

export function fetchBrief(eventId: string): Promise<BriefResponse> {
  if (isStaticMode()) {
    return fetchStaticJson<BriefResponse>(`events/${encodeURIComponent(eventId)}/brief.json`)
  }
  return requestJson<BriefResponse>(`/events/${encodeURIComponent(eventId)}/brief`)
}

export function fetchTrust(eventId: string): Promise<TrustResponse> {
  if (isStaticMode()) {
    return fetchStaticJson<TrustResponse>(`events/${encodeURIComponent(eventId)}/trust.json`)
  }
  return requestJson<TrustResponse>(`/events/${encodeURIComponent(eventId)}/trust`)
}

export function createLiveEventSource(eventId: string): EventSource {
  if (isStaticMode()) {
    const noop = () => undefined
    return {
      close: noop,
      addEventListener: noop,
      removeEventListener: noop,
      dispatchEvent: () => false,
      onopen: null,
      onmessage: null,
      onerror: null,
      readyState: 2,
      url: '',
      withCredentials: false,
      CONNECTING: 0,
      OPEN: 1,
      CLOSED: 2
    } as unknown as EventSource
  }
  return new EventSource(`${getApiBaseUrl()}/events/${encodeURIComponent(eventId)}/live`)
}

export async function submitCheck(payload: { text?: string; url?: string; image?: File }): Promise<CheckResponse> {
  const configuredCheck = import.meta.env.VITE_CHECK_BASE_URL
  if (isStaticMode() && (!configuredCheck || configuredCheck.trim().length === 0)) {
    throw new ApiClientError(
      503,
      'static_mode',
      'Live claim checking requires a running Check service backend.'
    )
  }
  const checkBase = getCheckBaseUrl()
  if (payload.image) {
    const formData = new FormData()
    formData.append('image', payload.image)
    return requestJson<CheckResponse>(
      '/check',
      {
        method: 'POST',
        body: formData
      },
      checkBase
    )
  }
  return requestJson<CheckResponse>(
    '/check',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload.url ? { url: payload.url } : { text: payload.text ?? '' })
    },
    checkBase
  )
}
