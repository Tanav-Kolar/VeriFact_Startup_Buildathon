import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { fetchBeforeAfter, fetchEventDetail } from '../api/client'
import { Header } from '../components/Header'
import type { BeforeAfterResponse, EventDetail } from '../types/api'
import { formatCount, formatPlatformLabel, formatUtcTimestamp } from '../utils/format'

const PATTERN_DEFINITIONS: Record<string, string> = {
  Faded: "VeriFact's definition: hourly volume fell substantially after the turning point.",
  Sustained: "VeriFact's definition: hourly volume remained comparable before and after the turning point.",
  Reframed: "VeriFact's definition: discussion shifted to new claims or terminology after the turning point.",
  'Not addressed': "VeriFact's definition: the turning point did not reference this storyline.",
  'Source of claims': "VeriFact's definition: the turning point itself originated claims discussed afterwards."
}

export function BeforeAfterPage() {
  const { eventId = '' } = useParams<{ eventId: string }>()
  const [eventDetail, setEventDetail] = useState<EventDetail | null>(null)
  const [selectedTpId, setSelectedTpId] = useState<string>('')
  const [data, setData] = useState<BeforeAfterResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!eventId) return
    let active = true
    fetchEventDetail(eventId)
      .then((detail) => {
        if (!active) return
        setEventDetail(detail)
        if (detail.turning_points.length > 0) {
          setSelectedTpId(detail.turning_points[0].id)
        }
      })
      .catch((err: unknown) => {
        if (active) setError(err instanceof Error ? err.message : 'Unable to load event')
      })
    return () => {
      active = false
    }
  }, [eventId])

  useEffect(() => {
    if (!eventId) return
    let active = true
    fetchBeforeAfter(eventId, selectedTpId || undefined)
      .then((res) => {
        if (active) setData(res)
      })
      .catch((err: unknown) => {
        if (active) setError(err instanceof Error ? err.message : 'Unable to load comparison')
      })
    return () => {
      active = false
    }
  }, [eventId, selectedTpId])

  const rows = data?.storylines ?? data?.rows ?? []
  const activeTp =
    data?.turning_point ??
    eventDetail?.turning_points.find((tp) => tp.id === selectedTpId) ??
    eventDetail?.turning_points[0] ??
    null
  const confirmed = data?.confirmed ?? activeTp?.confirmed ?? false
  const formText = data?.form ?? activeTp?.form ?? null

  return (
    <div className="vf-page-shell">
      <Header
        eventId={eventId}
        eventName={eventDetail?.name ?? eventId}
        t0Utc={eventDetail?.t0_utc ?? null}
        configFile={eventDetail?.config_file ?? null}
      />

      <main className="vf-subpage-main">
        <div className="vf-subpage-header">
          <div>
            <h2>Before / after comparison</h2>
            {/* P8.1 mandatory disclaimer */}
            <p className="vf-descriptive-note">
              {data?.note || 'Descriptive comparison, not a causal estimate.'}
            </p>
          </div>

          {eventDetail && eventDetail.turning_points.length > 0 ? (
            <label className="vf-tp-select-label">
              <span>Turning point</span>
              <select
                value={selectedTpId}
                onChange={(e) => setSelectedTpId(e.target.value)}
                className="vf-select"
              >
                {eventDetail.turning_points.map((tp) => (
                  <option key={tp.id} value={tp.id}>
                    {tp.label}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
        </div>

        <section className="vf-tp-summary-card">
          <div>
            <strong>Response:</strong> {activeTp ? activeTp.label : 'No turning point recorded'}
          </div>
          <div className="vf-tp-badges">
            <span className="vf-badge vf-badge-self-reported">Self-reported</span>
            {!confirmed ? (
              <span className="vf-badge vf-badge-unconfirmed">time unconfirmed</span>
            ) : activeTp?.at_utc ? (
              <span className="vf-tag">{formatUtcTimestamp(activeTp.at_utc)}</span>
            ) : null}
            {formText ? <span className="vf-tag">{formText}</span> : null}
          </div>
        </section>

        {error ? (
          <div className="vf-error-banner" role="alert">
            {error}
          </div>
        ) : null}

        <section className="vf-table-card">
          {rows.length === 0 ? (
            <p className="vf-muted-empty">
              No storyline before/after volumes are available yet for this turning point.
            </p>
          ) : (
            <table className="vf-table">
              <thead>
                <tr>
                  <th>Storyline</th>
                  <th>Before (/h)</th>
                  <th>After (/h)</th>
                  <th>Addressed</th>
                  <th>Pattern</th>
                  <th>Platform path</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => {
                  const patternLabel = row.pattern ?? '—'
                  const tooltip = row.pattern
                    ? PATTERN_DEFINITIONS[row.pattern] ?? `VeriFact's definition: ${row.pattern}`
                    : undefined
                  return (
                    <tr key={row.storyline_id}>
                      <td>{row.name ?? row.storyline_id}</td>
                      <td>{formatCount(row.volume_before_per_h)}</td>
                      <td>{formatCount(row.volume_after_per_h)}</td>
                      <td>{row.addressed ? 'Addressed' : 'Not addressed'}</td>
                      <td>
                        <span className="vf-pattern-pill" title={tooltip}>
                          {patternLabel}
                        </span>
                      </td>
                      <td>
                        {row.platform_path && row.platform_path.length > 0
                          ? row.platform_path
                              .map((p) => formatPlatformLabel(p.platform))
                              .join(' → ')
                          : '—'}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </section>
      </main>
    </div>
  )
}
