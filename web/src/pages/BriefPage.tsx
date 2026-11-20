import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { fetchBrief, fetchEventDetail } from '../api/client'
import { ClaimCard } from '../components/ClaimCard'
import { Header } from '../components/Header'
import type { BriefResponse, EventDetail } from '../types/api'
import {
  formatBriefPlaceholder,
  formatCount,
  formatPlatformLabel,
  formatUtcTimestamp
} from '../utils/format'

function renderCoverageSummary(coverage: Record<string, unknown> | undefined): string {
  if (!coverage) return '[n]'
  const entries = Object.entries(coverage)
  if (entries.length === 0) return '[n]'
  return entries
    .map(([source, raw]) => {
      const label = formatPlatformLabel(source)
      if (typeof raw === 'number') {
        return `${label}: ${formatCount(raw)}`
      }
      if (raw && typeof raw === 'object') {
        const obj = raw as { count?: number | null; cap?: number | null }
        const countStr = formatCount(obj.count)
        if (typeof obj.cap === 'number') {
          return `${label}: ${countStr}/${obj.cap}`
        }
        return `${label}: ${countStr}`
      }
      return `${label}: [n]`
    })
    .join(' · ')
}

export function BriefPage() {
  const { eventId = '' } = useParams<{ eventId: string }>()
  const navigate = useNavigate()
  const [eventDetail, setEventDetail] = useState<EventDetail | null>(null)
  const [brief, setBrief] = useState<BriefResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!eventId) return
    let active = true
    setLoading(true)
    Promise.all([fetchEventDetail(eventId), fetchBrief(eventId).catch(() => null)])
      .then(([detail, briefData]) => {
        if (!active) return
        setEventDetail(detail)
        setBrief(briefData)
      })
      .catch((err: unknown) => {
        if (active) setError(err instanceof Error ? err.message : 'Unable to load brief')
      })
      .finally(() => {
        if (active) setLoading(false)
      })

    return () => {
      active = false
    }
  }, [eventId])

  const handleInspectItem = (itemId: string) => {
    navigate(`/e/${encodeURIComponent(eventId)}?item=${encodeURIComponent(itemId)}`)
  }

  const generatedLabel =
    brief?.generated_at && brief.generated_at.trim().length > 0
      ? formatUtcTimestamp(brief.generated_at)
      : '[n]'
  const modelLabel = formatBriefPlaceholder(brief?.model_id)
  const promptLabel = formatBriefPlaceholder(brief?.prompt_version)
  const coverageLabel = renderCoverageSummary(brief?.coverage)

  return (
    <div className="vf-page-shell">
      <div className="vf-no-print">
        <Header
          eventId={eventId}
          eventName={eventDetail?.name ?? eventId}
          t0Utc={eventDetail?.t0_utc ?? null}
          configFile={eventDetail?.config_file ?? null}
        />
      </div>

      <main className="vf-subpage-main">
        <div className="vf-subpage-header vf-no-print">
          <div>
            <h2>One-page brief</h2>
            <p className="vf-muted-empty">A4 layout ready for print or PDF export.</p>
          </div>
          <button type="button" className="vf-ctrl-btn" onClick={() => window.print()}>
            Print / Export PDF
          </button>
        </div>

        {error ? (
          <div className="vf-error-banner vf-no-print" role="alert">
            {error}
          </div>
        ) : null}

        <article className="vf-brief-a4">
          <header className="vf-brief-head">
            <h1>{eventDetail?.name ?? eventId} — Narrative Brief</h1>
            <p className="vf-brief-sub">
              T0: {formatUtcTimestamp(eventDetail?.t0_utc)} · Config:{' '}
              {eventDetail?.config_file ?? '[n]'}
            </p>
          </header>

          {loading ? (
            <p className="vf-muted-empty">Loading brief…</p>
          ) : !brief ? (
            <div className="vf-brief-empty-body">
              <p className="vf-muted-empty">
                Brief has not been generated for this event yet.
              </p>
              <section className="vf-brief-section">
                <h2>Method and limits</h2>
                <p>Descriptive graph summary; not a causal estimate.</p>
              </section>
            </div>
          ) : (
            <>
              <section className="vf-brief-section">
                <h2>1. Summary</h2>
                {brief.summary.length === 0 ? (
                  <p className="vf-muted-empty">No summary sentences yet.</p>
                ) : (
                  <ol className="vf-brief-list">
                    {brief.summary.map((item, idx) => (
                      <li key={idx}>
                        <span>{item.text}</span>{' '}
                        <span className="vf-item-refs">
                          [
                          {item.item_ids.length > 0 ? (
                            item.item_ids.map((id, idIdx) => (
                              <span key={id}>
                                {idIdx > 0 ? ', ' : ''}
                                <Link
                                  to={`/e/${encodeURIComponent(eventId)}?item=${encodeURIComponent(id)}`}
                                  className="vf-item-ref-link"
                                >
                                  {id}
                                </Link>
                              </span>
                            ))
                          ) : (
                            'no linked items'
                          )}
                          ]
                        </span>
                      </li>
                    ))}
                  </ol>
                )}
              </section>

              <section className="vf-brief-section">
                <h2>2. Storylines</h2>
                {brief.storylines.length === 0 ? (
                  <p className="vf-muted-empty">No storylines recorded yet.</p>
                ) : (
                  <table className="vf-brief-table">
                    <thead>
                      <tr>
                        <th>Storyline</th>
                        <th>Volume</th>
                        <th>Origin &amp; spread</th>
                        <th>After response</th>
                        <th>Top public amplifier</th>
                      </tr>
                    </thead>
                    <tbody>
                      {brief.storylines.map((row) => (
                        <tr key={row.id}>
                          <td>
                            <Link
                              to={`/e/${encodeURIComponent(eventId)}?storyline=${encodeURIComponent(row.id)}`}
                              className="vf-item-ref-link"
                            >
                              {row.name}
                            </Link>
                          </td>
                          <td>{formatCount(row.volume)}</td>
                          <td>{row.origin_and_spread || '—'}</td>
                          <td>{row.after_response || '—'}</td>
                          <td>{row.top_amplifier ?? '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </section>

              <section className="vf-brief-section">
                <h2>3. Claims and evidence</h2>
                {brief.claims.length === 0 ? (
                  <p className="vf-muted-empty">No claims recorded yet.</p>
                ) : (
                  <div className="vf-brief-claims">
                    {brief.claims.map((claim) => (
                      <ClaimCard
                        key={claim.claim_id}
                        claim={claim}
                        onSelectItem={handleInspectItem}
                      />
                    ))}
                  </div>
                )}
              </section>

              <section className="vf-brief-section">
                <h2>4. What to watch</h2>
                {brief.watch.length === 0 ? (
                  <p className="vf-muted-empty">No watch items listed.</p>
                ) : (
                  <ul className="vf-brief-list">
                    {brief.watch.map((w, idx) => (
                      <li key={idx}>{w}</li>
                    ))}
                  </ul>
                )}
              </section>

              <section className="vf-brief-section">
                <h2>5. Evidence you could cite</h2>
                {brief.evidence_to_cite.length === 0 ? (
                  <p className="vf-muted-empty">No citable evidence items listed yet.</p>
                ) : (
                  <ul className="vf-brief-list">
                    {brief.evidence_to_cite.map((ev, idx) => (
                      <li key={idx}>
                        <span>{ev.text}</span>{' '}
                        <span className="vf-item-refs">
                          [
                          {ev.item_ids.length > 0 ? (
                            ev.item_ids.map((id, idIdx) => (
                              <span key={id}>
                                {idIdx > 0 ? ', ' : ''}
                                <Link
                                  to={`/e/${encodeURIComponent(eventId)}?item=${encodeURIComponent(id)}`}
                                  className="vf-item-ref-link"
                                >
                                  {id}
                                </Link>
                              </span>
                            ))
                          ) : (
                            'no linked items'
                          )}
                          ]
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </section>

              <section className="vf-brief-section">
                <h2>6. Method and limits</h2>
                <p>{brief.method_and_limits || 'Descriptive graph summary; not a causal estimate.'}</p>
              </section>
            </>
          )}

          <footer className="vf-brief-footer">
            <span>Coverage: {coverageLabel}</span>
            <span>Generated: {generatedLabel}</span>
            <span>Model: {modelLabel}</span>
            <span>Prompt: {promptLabel}</span>
          </footer>
        </article>
      </main>
    </div>
  )
}
