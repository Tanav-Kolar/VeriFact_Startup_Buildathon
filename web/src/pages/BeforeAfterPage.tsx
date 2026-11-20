import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { fetchBeforeAfter, fetchEventDetail } from '../api/client'
import { Header } from '../components/Header'
import type { BeforeAfterResponse, EventDetail } from '../types/api'
import { formatCount, formatPlatformLabel, formatUtcTimestamp } from '../utils/format'

const PATTERN_DEFINITIONS: Record<string, string> = {
  faded: "VeriFact's definition: hourly volume fell substantially after the turning point.",
  sustained: "VeriFact's definition: hourly volume remained comparable before and after the turning point.",
  reframed: "VeriFact's definition: discussion shifted to new claims or terminology after the turning point.",
  'not addressed': "VeriFact's definition: the turning point did not reference this storyline.",
  'source of claims': "VeriFact's definition: the turning point itself originated claims discussed afterwards.",
  unchanged: "VeriFact's definition: no change in hourly rate recorded around this turning point."
}

function getPatternTooltip(pattern: string | null | undefined): string | undefined {
  if (!pattern) return undefined
  const key = pattern.trim().toLowerCase()
  return PATTERN_DEFINITIONS[key] ?? `VeriFact's definition: ${pattern}`
}

function HourlyBarsMini({
  hourlyVolumes
}: {
  hourlyVolumes: Array<{ hour: number; count: number }>
}) {
  if (hourlyVolumes.length === 0) return null
  const sorted = [...hourlyVolumes].sort((a, b) => a.hour - b.hour)
  const maxVal = Math.max(1, ...sorted.map((d) => d.count))
  const width = 120
  const height = 28
  const barW = Math.max(2, width / sorted.length - 1)

  return (
    <svg
      className="vf-ba-mini-svg"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label="Hourly storyline volume"
    >
      {sorted.map((pt, idx) => {
        const barH = pt.count > 0 ? Math.max(2, (pt.count / maxVal) * (height - 4)) : 1
        return (
          <rect
            key={pt.hour}
            x={idx * (barW + 1)}
            y={height - 2 - barH}
            width={barW}
            height={barH}
            fill="#38bdf8"
            fillOpacity={pt.count > 0 ? 0.8 : 0.2}
          >
            <title>{`T+${pt.hour}h: ${pt.count}`}</title>
          </rect>
        )
      })}
    </svg>
  )
}

export function BeforeAfterPage() {
  const { eventId = '' } = useParams<{ eventId: string }>()
  const [eventDetail, setEventDetail] = useState<EventDetail | null>(null)
  const [selectedTpId, setSelectedTpId] = useState<string>('')
  const [data, setData] = useState<BeforeAfterResponse | null>(null)
  const [loading, setLoading] = useState(true)
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
    setLoading(true)
    fetchBeforeAfter(eventId, selectedTpId || undefined)
      .then((res) => {
        if (active) {
          setData(res)
          setError(null)
        }
      })
      .catch((err: unknown) => {
        if (active) setError(err instanceof Error ? err.message : 'Unable to load comparison')
      })
      .finally(() => {
        if (active) setLoading(false)
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

  const maxRatePerHour = useMemo(() => {
    let max = 1
    for (const row of rows) {
      if (typeof row.volume_before_per_h === 'number' && row.volume_before_per_h > max) {
        max = row.volume_before_per_h
      }
      if (typeof row.volume_after_per_h === 'number' && row.volume_after_per_h > max) {
        max = row.volume_after_per_h
      }
    }
    return max
  }, [rows])

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
            <p className="vf-descriptive-note" role="note">
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

        <section className="vf-tp-summary-card" aria-label="Turning point status">
          <div>
            <strong>Response:</strong> {activeTp ? activeTp.label : 'No turning point recorded'}
          </div>
          <div className="vf-tp-badges">
            <span className="vf-badge vf-badge-self-reported">Self-reported</span>
            {confirmed ? (
              <span className="vf-tag">
                Confirmed{activeTp?.at_utc ? ` · ${formatUtcTimestamp(activeTp.at_utc)}` : ''}
              </span>
            ) : (
              <span className="vf-badge vf-badge-unconfirmed">time unconfirmed</span>
            )}
            <span className="vf-tag">{formText ?? 'form unconfirmed'}</span>
          </div>
        </section>

        {error ? (
          <div className="vf-error-banner" role="alert">
            {error}
          </div>
        ) : null}

        <section className="vf-table-card">
          {loading && rows.length === 0 ? (
            <p className="vf-muted-empty">Loading before/after comparison…</p>
          ) : rows.length === 0 ? (
            <p className="vf-muted-empty">
              No storyline before/after volumes are available yet for this turning point.
            </p>
          ) : (
            <table className="vf-table">
              <thead>
                <tr>
                  <th>Storyline</th>
                  <th>Before vs After (/h)</th>
                  <th>Addressed</th>
                  <th>Pattern</th>
                  <th>Platform path</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => {
                  const patternLabel = row.pattern ?? '—'
                  const tooltip = getPatternTooltip(row.pattern)
                  const beforeVal = row.volume_before_per_h
                  const afterVal = row.volume_after_per_h
                  const beforePct =
                    typeof beforeVal === 'number' && beforeVal > 0
                      ? Math.max(4, Math.round((beforeVal / maxRatePerHour) * 100))
                      : 0
                  const afterPct =
                    typeof afterVal === 'number' && afterVal > 0
                      ? Math.max(4, Math.round((afterVal / maxRatePerHour) * 100))
                      : 0

                  return (
                    <tr key={row.storyline_id}>
                      <td>
                        <Link
                          to={`/e/${encodeURIComponent(eventId)}?storyline=${encodeURIComponent(row.storyline_id)}`}
                          className="vf-deep-link"
                        >
                          {row.name ?? row.storyline_id}
                        </Link>
                        {row.hourly_volumes && row.hourly_volumes.length > 0 ? (
                          <div className="vf-ba-mini-wrap">
                            <HourlyBarsMini hourlyVolumes={row.hourly_volumes} />
                          </div>
                        ) : null}
                      </td>
                      <td>
                        <div className="vf-ba-bars" aria-label="Before and after hourly volume rates">
                          <div className="vf-ba-bar-row">
                            <span className="vf-ba-bar-label">Before</span>
                            <div className="vf-ba-bar-track">
                              <div
                                className="vf-ba-bar-fill vf-ba-fill-before"
                                style={{ width: `${beforePct}%` }}
                              />
                            </div>
                            <span className="vf-ba-bar-num">
                              {formatCount(beforeVal, '/h')}
                            </span>
                          </div>
                          <div className="vf-ba-bar-row">
                            <span className="vf-ba-bar-label">After</span>
                            <div className="vf-ba-bar-track">
                              <div
                                className="vf-ba-bar-fill vf-ba-fill-after"
                                style={{ width: `${afterPct}%` }}
                              />
                            </div>
                            <span className="vf-ba-bar-num">
                              {formatCount(afterVal, '/h')}
                            </span>
                          </div>
                        </div>
                      </td>
                      <td>
                        <span className={`vf-tag ${row.addressed ? 'vf-tag-addressed' : ''}`}>
                          {row.addressed ? 'Addressed' : 'Not addressed'}
                        </span>
                      </td>
                      <td>
                        <span
                          className="vf-pattern-pill"
                          title={tooltip}
                          aria-label={tooltip ? `${patternLabel}. ${tooltip}` : patternLabel}
                        >
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
