import { useEffect, useState, type FormEvent } from 'react'
import { useSearchParams } from 'react-router-dom'
import { fetchEventDetail, fetchEvents, submitCheck } from '../api/client'
import { ClaimCard } from '../components/ClaimCard'
import { Header } from '../components/Header'
import type { CheckResponse, EventDetail } from '../types/api'

type InputMode = 'text' | 'url' | 'image'

export function CheckPage() {
  const [searchParams] = useSearchParams()
  const requestedEvent = searchParams.get('event')

  const [activeEventId, setActiveEventId] = useState<string | null>(requestedEvent)
  const [eventDetail, setEventDetail] = useState<EventDetail | null>(null)

  const [mode, setMode] = useState<InputMode>('text')
  const [textInput, setTextInput] = useState('')
  const [urlInput, setUrlInput] = useState('')
  const [imageFile, setImageFile] = useState<File | null>(null)

  const [submitting, setSubmitting] = useState(false)
  const [result, setResult] = useState<CheckResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    if (requestedEvent) {
      setActiveEventId(requestedEvent)
      return
    }
    fetchEvents()
      .then((events) => {
        if (active && events.length > 0) {
          setActiveEventId(events[0].event_id)
        }
      })
      .catch(() => {
        // Check page works independently even if events list is unavailable
      })
    return () => {
      active = false
    }
  }, [requestedEvent])

  useEffect(() => {
    if (!activeEventId) return
    let active = true
    fetchEventDetail(activeEventId)
      .then((detail) => {
        if (active) setEventDetail(detail)
      })
      .catch(() => {
        // Ignore header metadata failure
      })
    return () => {
      active = false
    }
  }, [activeEventId])

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      const payload =
        mode === 'url'
          ? { url: urlInput.trim() }
          : mode === 'image'
            ? { image: imageFile ?? undefined }
            : { text: textInput.trim() }

      const response = await submitCheck(payload)
      setResult(response)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Check request failed')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="vf-page-shell">
      <Header
        eventId={activeEventId}
        eventName={eventDetail?.name ?? 'Check a claim'}
        t0Utc={eventDetail?.t0_utc ?? null}
        configFile={eventDetail?.config_file ?? null}
      />

      <main className="vf-subpage-main vf-check-main">
        <div className="vf-subpage-header">
          <div>
            <h2>Check a claim</h2>
            <p className="vf-muted-empty">
              Paste a factual claim, article URL, or screenshot to inspect matching evidence cards. No
              user identity is stored.
            </p>
          </div>
        </div>

        <section className="vf-check-card">
          <div className="vf-mode-tabs" role="tablist" aria-label="Check input type">
            <button
              type="button"
              role="tab"
              aria-selected={mode === 'text'}
              className={`vf-tab-btn ${mode === 'text' ? 'is-active' : ''}`}
              onClick={() => setMode('text')}
            >
              Text
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === 'url'}
              className={`vf-tab-btn ${mode === 'url' ? 'is-active' : ''}`}
              onClick={() => setMode('url')}
            >
              URL
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === 'image'}
              className={`vf-tab-btn ${mode === 'image' ? 'is-active' : ''}`}
              onClick={() => setMode('image')}
            >
              Screenshot
            </button>
          </div>

          <form onSubmit={handleSubmit} className="vf-check-form">
            {mode === 'text' ? (
              <textarea
                rows={4}
                value={textInput}
                onChange={(e) => setTextInput(e.target.value)}
                placeholder="Paste a factual claim or post text to match against collected evidence…"
                className="vf-textarea"
                required
              />
            ) : mode === 'url' ? (
              <input
                type="url"
                value={urlInput}
                onChange={(e) => setUrlInput(e.target.value)}
                placeholder="https://example.com/article"
                className="vf-input"
                required
              />
            ) : (
              <input
                type="file"
                accept="image/*"
                onChange={(e) => setImageFile(e.target.files?.[0] ?? null)}
                className="vf-file-input"
                required
              />
            )}

            <div className="vf-check-actions">
              <button type="submit" className="vf-ctrl-btn" disabled={submitting}>
                {submitting ? 'Checking…' : 'Check evidence'}
              </button>
            </div>
          </form>
        </section>

        {error ? (
          <div className="vf-error-banner" role="alert">
            {error}
          </div>
        ) : null}

        {result ? (
          <section className="vf-check-results" aria-label="Claim evidence cards">
            {result.notice ? <p className="vf-descriptive-note">{result.notice}</p> : null}
            {result.claims.length === 0 ? (
              <p className="vf-muted-empty">No matching factual claims found in this input.</p>
            ) : (
              result.claims.map((claim) => (
                <ClaimCard key={claim.claim_id} claim={claim} showDeepLink />
              ))
            )}
          </section>
        ) : null}
      </main>
    </div>
  )
}
