import { useMemo, type MouseEvent } from 'react'
import type { FrameData, StorylineRow, TurningPoint } from '../types/api'
import { computeHourFromT0, formatHourOffset } from '../utils/format'
import { getStorylineColour } from '../utils/palette'

export interface TimelineProps {
  t0Utc: string | null
  frames: FrameData[]
  storylines: StorylineRow[]
  turningPoints: TurningPoint[]
  minHour: number
  maxHour: number
  currentHour: number
  nowHour: number | null
  isPlaying: boolean
  onChangeHour: (hour: number) => void
  onTogglePlay: () => void
  onJumpToResponse: () => void
  onJumpToNow: () => void
}

const SVG_WIDTH = 960
const SVG_HEIGHT = 84
const PAD_LEFT = 16
const PAD_RIGHT = 16
const PAD_TOP = 18
const PAD_BOTTOM = 16

export function Timeline({
  t0Utc,
  frames,
  storylines,
  turningPoints,
  minHour,
  maxHour,
  currentHour,
  nowHour,
  isPlaying,
  onChangeHour,
  onTogglePlay,
  onJumpToResponse,
  onJumpToNow
}: TimelineProps) {
  const safeMaxHour = Math.max(minHour + 1, maxHour)
  const span = safeMaxHour - minHour

  const xForHour = (h: number): number => {
    const ratio = (h - minHour) / span
    return PAD_LEFT + Math.max(0, Math.min(1, ratio)) * (SVG_WIDTH - PAD_LEFT - PAD_RIGHT)
  }

  const orderedStorylines = useMemo(
    () => storylines.filter((s) => s.id !== 'unassigned'),
    [storylines]
  )

  const hourlySeries = useMemo(() => {
    const byHour = new Map<number, FrameData>()
    for (const f of frames) {
      byHour.set(f.hour, f)
    }

    const hours: number[] = []
    for (let h = minHour; h <= currentHour; h += 1) {
      hours.push(h)
    }

    let globalMaxTotal = 1
    const stackedByHour = hours.map((h) => {
      const frame = byHour.get(h)
      let running = 0
      const segments = orderedStorylines.map((s) => {
        const count = frame ? frame.nodes_added.filter((n) => n.storyline_id === s.id).length : 0
        const y0 = running
        running += count
        return { id: s.id, colourIndex: s.colour_index, y0, y1: running }
      })
      if (running > globalMaxTotal) globalMaxTotal = running
      return { hour: h, segments }
    })

    return { stackedByHour, globalMaxTotal }
  }, [frames, minHour, currentHour, orderedStorylines])

  const plotHeight = SVG_HEIGHT - PAD_TOP - PAD_BOTTOM
  const yForValue = (val: number): number => {
    const ratio = val / hourlySeries.globalMaxTotal
    return PAD_TOP + plotHeight - ratio * plotHeight
  }

  const handleSvgClick = (event: MouseEvent<SVGSVGElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    const clickX = ((event.clientX - rect.left) / rect.width) * SVG_WIDTH
    const ratio = (clickX - PAD_LEFT) / (SVG_WIDTH - PAD_LEFT - PAD_RIGHT)
    const clickedHour = Math.round(minHour + Math.max(0, Math.min(1, ratio)) * span)
    onChangeHour(clickedHour)
  }

  const primaryTurningPoint = turningPoints[0] ?? null
  const turningPointHour = primaryTurningPoint
    ? computeHourFromT0(t0Utc, primaryTurningPoint.at_utc)
    : null

  const playheadX = xForHour(currentHour)

  return (
    <section className="vf-timeline" aria-label="Narrative timeline">
      <div className="vf-timeline-controls">
        <button type="button" className="vf-ctrl-btn" onClick={onTogglePlay}>
          {isPlaying ? 'Pause' : 'Replay'}
        </button>

        <button
          type="button"
          className="vf-ctrl-btn vf-ctrl-response"
          onClick={onJumpToResponse}
          disabled={turningPointHour === null}
          title={
            primaryTurningPoint
              ? `${primaryTurningPoint.label}${primaryTurningPoint.confirmed ? '' : ' (time unconfirmed)'}`
              : 'No turning point configured'
          }
        >
          Jump to response
          {primaryTurningPoint && !primaryTurningPoint.confirmed ? (
            <span className="vf-unconfirmed-tag">time unconfirmed</span>
          ) : null}
        </button>

        <button
          type="button"
          className="vf-ctrl-btn"
          onClick={onJumpToNow}
          disabled={nowHour === null}
        >
          Now
        </button>

        <div className="vf-timeline-hour-readout">
          <span>{formatHourOffset(currentHour)}</span>
          <span className="vf-kbd-hint">←/→ ±1h · Space play · R response · Esc clear</span>
        </div>
      </div>

      <div className="vf-stream-wrap">
        <svg
          className="vf-stream-svg"
          viewBox={`0 0 ${SVG_WIDTH} ${SVG_HEIGHT}`}
          preserveAspectRatio="none"
          onClick={handleSvgClick}
          role="img"
          aria-label={`Hourly volume streamgraph up to ${formatHourOffset(currentHour)}`}
        >
          {/* Baseline */}
          <line
            x1={PAD_LEFT}
            y1={SVG_HEIGHT - PAD_BOTTOM}
            x2={SVG_WIDTH - PAD_RIGHT}
            y2={SVG_HEIGHT - PAD_BOTTOM}
            stroke="#334155"
            strokeWidth={1}
          />

          {/* Stacked stream bars drawn strictly up to currentHour */}
          {hourlySeries.stackedByHour.map((entry) => {
            const x = xForHour(entry.hour)
            const barWidth = Math.max(3, (SVG_WIDTH - PAD_LEFT - PAD_RIGHT) / span - 1)
            return (
              <g key={entry.hour}>
                {entry.segments.map((seg) => {
                  if (seg.y1 <= seg.y0) return null
                  const topY = yForValue(seg.y1)
                  const bottomY = yForValue(seg.y0)
                  return (
                    <rect
                      key={`${entry.hour}-${seg.id}`}
                      x={x - barWidth / 2}
                      y={topY}
                      width={barWidth}
                      height={Math.max(1, bottomY - topY)}
                      fill={getStorylineColour(seg.id, seg.colourIndex)}
                      fillOpacity={0.82}
                    />
                  )
                })}
              </g>
            )
          })}

          {/* T0 Marker */}
          {minHour <= 0 && safeMaxHour >= 0 ? (
            <g>
              <line
                x1={xForHour(0)}
                y1={6}
                x2={xForHour(0)}
                y2={SVG_HEIGHT - PAD_BOTTOM}
                stroke="#94a3b8"
                strokeDasharray="3 3"
                strokeWidth={1}
              />
              <text x={xForHour(0) + 4} y={13} fill="#94a3b8" fontSize="10">
                T0
              </text>
            </g>
          ) : null}

          {/* Turning point markers (gold dashed, P7.6 time unconfirmed when confirmed=false) */}
          {turningPoints.map((tp) => {
            const tpHour = computeHourFromT0(t0Utc, tp.at_utc)
            if (tpHour === null) return null
            const tx = xForHour(tpHour)
            const label = tp.confirmed ? tp.label : `${tp.label} · time unconfirmed`
            return (
              <g key={tp.id}>
                <line
                  x1={tx}
                  y1={6}
                  x2={tx}
                  y2={SVG_HEIGHT - PAD_BOTTOM}
                  stroke="#facc15"
                  strokeDasharray="4 3"
                  strokeWidth={1.5}
                />
                <text x={tx + 4} y={13} fill="#facc15" fontSize="10">
                  {label}
                </text>
              </g>
            )
          })}

          {/* Playhead */}
          <line
            x1={playheadX}
            y1={4}
            x2={playheadX}
            y2={SVG_HEIGHT - PAD_BOTTOM + 2}
            stroke="#f8fafc"
            strokeWidth={2}
          />
        </svg>

        {/* Accessible range input synchronized with streamgraph (FR-4.4) */}
        <input
          type="range"
          className="vf-accessible-slider"
          aria-label="Timeline hour scrubber"
          min={minHour}
          max={safeMaxHour}
          step={1}
          value={currentHour}
          onChange={(e) => onChangeHour(Number(e.target.value))}
        />
      </div>
    </section>
  )
}
