import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { fetchBrief, fetchEventDetail } from '../api/client'
import { ClaimCard } from '../components/ClaimCard'
import { Header } from '../components/Header'
import type { BriefResponse, EventDetail } from '../types/api'
import { formatCount, formatUtcTimestamp } from '../utils/format'

export function BriefPage() {
  const { eventId = '' } = useParams<{ eventId: string }>()
  const [eventDetail, setEventDetail] = useState<EventDetail | null>(null)
  const [brief, setBrief] = useState<BriefResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!eventId) return
    let active = true
    Promise.all([fetchEventDetail(eventId), fetchBrief(eventId).catch(() => null)])
      .then(([detail, briefData]) => {
        if (!active) return
        setEventDetail(detail)
        setBrief(briefData)
      })
      .catch((err: unknown) => {
        if (active) setError(err instanceof Error ? err.message : 'Unable to load brief')
      })

    return () => {
      active = false
    }
  }, [eventId])

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
          <div className="vf-error-banner" role="alert">
            {error}
          </div>
        ) : null}

        <article className="vf-brief-a4">
          <header className="vf-brief-head">
            <h1>{eventDetail?.name ?? eventId} — Narrative Brief</h1>
            <p className="vf-brief-sub">
              T0: {formatUtcTimestamp(eventDetail?.t0_utc)} · Config:{' '}
              {eventDetail?.config_file ?? '—'}
            </p>
          </header>

          {!brief ? (
            <p className="vf-muted-empty">
              Brief has not been generated for this event yet.
            </p>
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
                          [{item.item_ids.length > 0 ? item.item_ids.join(', ') : 'no linked items'}]
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
                          <td>{row.name}</td>
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
                      <ClaimCard key={claim.claim_id} claim={claim} />
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
                        <span className="vf-item-refs">[{ev.item_ids.join(', ')}]</span>
                      </li>
                    ))}
                  </ul>
                )}
              </section>

              <section className="vf-brief-section">
                <h2>6. Method and limits</h2>
                <p>{brief.method_and_limits || 'Descriptive graph summary; not a causal estimate.'}</p>
              </section>

              <footer className="vf-brief-footer">
                <span>Generated: {formatUtcTimestamp(brief.generated_at)}</span>
                <span>Model: {brief.model_id ?? '[model]'}</span>
                <span>Prompt: {brief.prompt_version ?? '[prompt]'}</span>
              </footer>
            </>
          )}
        </article>
      </main>
    </div>
  )
}
