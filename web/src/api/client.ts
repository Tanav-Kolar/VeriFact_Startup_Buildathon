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

export function fetchEvents(): Promise<EventSummary[]> {
  return requestJson<EventSummary[]>('/events')
}

export function fetchEventDetail(eventId: string): Promise<EventDetail> {
  return requestJson<EventDetail>(`/events/${encodeURIComponent(eventId)}`)
}

export function fetchFrames(eventId: string, fromHour?: number, toHour?: number): Promise<FrameData[]> {
  const params = new URLSearchParams()
  if (typeof fromHour === 'number') params.set('from_hour', String(fromHour))
  if (typeof toHour === 'number') params.set('to_hour', String(toHour))
  const qs = params.toString()
  return requestJson<FrameData[]>(`/events/${encodeURIComponent(eventId)}/frames${qs ? `?${qs}` : ''}`)
}

export function fetchStorylines(eventId: string, atHour?: number): Promise<StorylineRow[]> {
  const qs = typeof atHour === 'number' ? `?at_hour=${encodeURIComponent(String(atHour))}` : ''
  return requestJson<StorylineRow[]>(`/events/${encodeURIComponent(eventId)}/storylines${qs}`)
}

export function fetchStorylineDetail(
  eventId: string,
  storylineId: string,
  atHour?: number
): Promise<StorylineDetail> {
  const qs = typeof atHour === 'number' ? `?at_hour=${encodeURIComponent(String(atHour))}` : ''
  return requestJson<StorylineDetail>(
    `/events/${encodeURIComponent(eventId)}/storylines/${encodeURIComponent(storylineId)}${qs}`
  )
}

export function fetchItemDetail(eventId: string, itemId: string): Promise<ItemDetail> {
  return requestJson<ItemDetail>(
    `/events/${encodeURIComponent(eventId)}/items/${encodeURIComponent(itemId)}`
  )
}

export function fetchClaimCard(eventId: string, claimId: string, atHour?: number): Promise<ClaimCardData> {
  const qs = typeof atHour === 'number' ? `?at_hour=${encodeURIComponent(String(atHour))}` : ''
  return requestJson<ClaimCardData>(
    `/events/${encodeURIComponent(eventId)}/claims/${encodeURIComponent(claimId)}${qs}`
  )
}

export function fetchSignals(eventId: string): Promise<TimingSignal[]> {
  return requestJson<TimingSignal[]>(`/events/${encodeURIComponent(eventId)}/signals`)
}

export function fetchBeforeAfter(eventId: string, turningPointId?: string): Promise<BeforeAfterResponse> {
  const qs = turningPointId ? `?turning_point=${encodeURIComponent(turningPointId)}` : ''
  return requestJson<BeforeAfterResponse>(`/events/${encodeURIComponent(eventId)}/before-after${qs}`)
}

export function fetchBrief(eventId: string): Promise<BriefResponse> {
  return requestJson<BriefResponse>(`/events/${encodeURIComponent(eventId)}/brief`)
}

export function fetchTrust(eventId: string): Promise<TrustResponse> {
  return requestJson<TrustResponse>(`/events/${encodeURIComponent(eventId)}/trust`)
}

export function createLiveEventSource(eventId: string): EventSource {
  return new EventSource(`${getApiBaseUrl()}/events/${encodeURIComponent(eventId)}/live`)
}

export async function submitCheck(payload: { text?: string; url?: string; image?: File }): Promise<CheckResponse> {
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
