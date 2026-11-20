import { useEffect, useState } from 'react'
import type {
  ClaimCardData,
  GraphNodeData,
  ItemDetail,
  StorylineDetail,
  StorylineRow,
  TimingSignal
} from '../types/api'
import {
  formatCount,
  formatHourOffset,
  formatIstTimestamp,
  formatLagSeconds,
  formatPlatformLabel,
  formatUtcTimestamp,
  sanitizeHttpUrl,
  truncateExcerpt
} from '../utils/format'
import { getStorylineColour } from '../utils/palette'
import { ClaimCard } from './ClaimCard'

export interface InspectorProps {
  selectedItem: ItemDetail | null
  selectedClaim: ClaimCardData | null
  focusedStoryline: StorylineDetail | null
  storylines: StorylineRow[]
  storylineItems?: GraphNodeData[]
  signals: TimingSignal[]
  currentHour: number
  loading?: boolean
  error?: string | null
  onSelectClaimId: (claimId: string) => void
  onSelectItemId: (itemId: string) => void
  onFocusStoryline?: (storylineId: string) => void
  onClearSelection: () => void
}

type InspectorTab = 'detail' | 'signals'

function VolumeSparkline({
  volumeByHour,
  currentHour,
  colour
}: {
  volumeByHour: Array<{ hour: number; count: number }>
  currentHour: number
  colour: string
}) {
  const filtered = [...volumeByHour]
    .filter((entry) => entry.hour <= currentHour)
    .sort((a, b) => a.hour - b.hour)

  if (filtered.length === 0) {
    return <p className="vf-muted-empty">No hourly volume recorded up to this hour.</p>
  }

  const maxCount = Math.max(1, ...filtered.map((d) => d.count))
  const width = 280
  const height = 52
  const padX = 4
  const padY = 6
  const barGap = 2
  const usableW = width - padX * 2
  const barW = Math.max(3, usableW / filtered.length - barGap)
  const plotH = height - padY * 2

  return (
    <div className="vf-sparkline-wrap">
      <svg
        className="vf-sparkline-svg"
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={`Hourly volume sparkline up to ${formatHourOffset(currentHour)}`}
      >
        <line
          x1={padX}
          y1={height - padY}
          x2={width - padX}
          y2={height - padY}
          stroke="rgba(148, 163, 184, 0.25)"
          strokeWidth={1}
        />
        {filtered.map((entry, idx) => {
          const barH = entry.count > 0 ? Math.max(2, (entry.count / maxCount) * plotH) : 1
          const x = padX + idx * (barW + barGap)
          const y = height - padY - barH
          return (
            <rect
              key={entry.hour}
              x={x}
              y={y}
              width={barW}
              height={barH}
              rx={1}
              fill={colour}
              fillOpacity={entry.count > 0 ? 0.85 : 0.2}
            >
              <title>{`${formatHourOffset(entry.hour)}: ${entry.count} items`}</title>
            </rect>
          )
        })}
      </svg>
      <div className="vf-sparkline-range">
        <span>{formatHourOffset(filtered[0].hour)}</span>
        <span>Peak {maxCount}/h</span>
        <span>{formatHourOffset(filtered[filtered.length - 1].hour)}</span>
      </div>
    </div>
  )
}

export function Inspector({
  selectedItem,
  selectedClaim,
  focusedStoryline,
  storylines,
  storylineItems = [],
  signals,
  currentHour,
  loading = false,
  error = null,
  onSelectClaimId,
  onSelectItemId,
  onFocusStoryline,
  onClearSelection
}: InspectorProps) {
  const [activeTab, setActiveTab] = useState<InspectorTab>('detail')
  const [itemsExpanded, setItemsExpanded] = useState(false)

  // Switch to detail view when user selects an item, claim, or focuses a storyline
  useEffect(() => {
    if (selectedItem || selectedClaim || focusedStoryline) {
      setActiveTab('detail')
    }
  }, [selectedItem, selectedClaim, focusedStoryline])

  useEffect(() => {
    setItemsExpanded(false)
  }, [focusedStoryline?.id, currentHour])

  const storylineLookup = new Map(storylines.map((s) => [s.id, s]))

  const resolveStorylineMeta = (storylineId: string | null | undefined) => {
    const id = storylineId || 'unassigned'
    const row = storylineLookup.get(id)
    return {
      id,
      name: row?.name ?? (id === 'unassigned' ? 'Unassigned' : id),
      colour: getStorylineColour(id, row?.colour_index)
    }
  }

  const relevantSignals = focusedStoryline
    ? signals.filter((s) => s.storyline_id === focusedStoryline.id)
    : signals

  const itemStoryline = selectedItem ? resolveStorylineMeta(selectedItem.storyline_id) : null
  const focusedColour = focusedStoryline
    ? getStorylineColour(focusedStoryline.id, focusedStoryline.colour_index)
    : '#38bdf8'

  return (
    <aside className="vf-inspector" aria-label="Evidence inspector">
      <div className="vf-panel-header">
        <h2>Inspector</h2>
        {selectedItem || selectedClaim ? (
          <button type="button" className="vf-clear-focus-btn" onClick={onClearSelection}>
            Back
          </button>
        ) : null}
      </div>

      <div className="vf-inspector-tabs" role="tablist" aria-label="Inspector sections">
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === 'detail'}
          className={`vf-inspector-tab ${activeTab === 'detail' ? 'is-active' : ''}`}
          onClick={() => setActiveTab('detail')}
        >
          Detail
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === 'signals'}
          className={`vf-inspector-tab ${activeTab === 'signals' ? 'is-active' : ''}`}
          onClick={() => setActiveTab('signals')}
        >
          Signals ({signals.length})
        </button>
      </div>

      {activeTab === 'signals' ? (
        <div className="vf-inspector-section" role="tabpanel" aria-label="Timing signals">
          {signals.length === 0 ? (
            <p className="vf-muted-empty">
              No timing or duplication signals recorded for this event.
            </p>
          ) : (
            <ul className="vf-signals-list">
              {signals.map((sig, idx) => {
                const stMeta = resolveStorylineMeta(sig.storyline_id)
                return (
                  <li key={`${sig.storyline_id}-${idx}`} className="vf-signal-card">
                    <div className="vf-signal-card-head">
                      {onFocusStoryline ? (
                        <button
                          type="button"
                          className="vf-storyline-badge-btn"
                          onClick={() => onFocusStoryline(stMeta.id)}
                        >
                          <span
                            className="vf-colour-dot"
                            style={{ backgroundColor: stMeta.colour }}
                            aria-hidden="true"
                          />
                          <span>{stMeta.name}</span>
                        </button>
                      ) : (
                        <span className="vf-tag">{stMeta.name}</span>
                      )}
                      <span className="vf-signal-window">
                        {formatCount(sig.n_items)} items · {formatCount(sig.window_minutes)}m
                      </span>
                    </div>
                    <p className="vf-signal-note">{sig.text}</p>
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      ) : (
        <>
          {loading ? <p className="vf-muted-empty">Loading details…</p> : null}

          {!loading && error ? (
            <div className="vf-error-banner" role="alert">
              {error}
            </div>
          ) : null}

          {!loading && selectedItem ? (
            <div className="vf-inspector-section">
              <div className="vf-inspector-badges">
                <span className="vf-tag">{formatPlatformLabel(selectedItem.platform)}</span>
                {itemStoryline ? (
                  onFocusStoryline ? (
                    <button
                      type="button"
                      className="vf-storyline-badge-btn"
                      onClick={() => onFocusStoryline(itemStoryline.id)}
                      title={`Focus storyline: ${itemStoryline.name}`}
                    >
                      <span
                        className="vf-colour-dot"
                        style={{ backgroundColor: itemStoryline.colour }}
                        aria-hidden="true"
                      />
                      <span>{itemStoryline.name}</span>
                    </button>
                  ) : (
                    <span className="vf-tag">{itemStoryline.name}</span>
                  )
                ) : null}
                {selectedItem.lang ? <span className="vf-tag">{selectedItem.lang}</span> : null}
                {selectedItem.is_official || selectedItem.self_reported ? (
                  <span className="vf-badge vf-badge-self-reported">Self-reported</span>
                ) : null}
              </div>

              {selectedItem.label ? (
                <div className="vf-inspector-title">{selectedItem.label}</div>
              ) : null}

              <dl className="vf-meta-grid">
                <div>
                  <dt>Posted (UTC)</dt>
                  <dd>{formatUtcTimestamp(selectedItem.created_at)}</dd>
                </div>
                <div>
                  <dt>Posted (IST)</dt>
                  <dd>{formatIstTimestamp(selectedItem.created_at)}</dd>
                </div>
                <div>
                  <dt>Collected (UTC)</dt>
                  <dd>{formatUtcTimestamp(selectedItem.collected_at)}</dd>
                </div>
                <div>
                  <dt>Collection lag</dt>
                  <dd>{formatLagSeconds(selectedItem.collection_lag_s)}</dd>
                </div>
                <div>
                  <dt>Reach</dt>
                  <dd>{formatCount(selectedItem.reach)}</dd>
                </div>
              </dl>

              <blockquote className="vf-item-excerpt">
                {truncateExcerpt(selectedItem.excerpt, 300)}
              </blockquote>

              {selectedItem.tags && selectedItem.tags.length > 0 ? (
                <div className="vf-path-chips" aria-label="Item tags">
                  {selectedItem.tags.map((tag) => (
                    <span key={tag} className="vf-tag">
                      {tag}
                    </span>
                  ))}
                </div>
              ) : null}

              {selectedItem.conflict ? (
                <div className="vf-conflict-disclosure" role="note">
                  <strong>Disclosed conflict:</strong> {selectedItem.conflict.person} —{' '}
                  {selectedItem.conflict.relationship}
                </div>
              ) : null}

              {(() => {
                const safeUrl = sanitizeHttpUrl(selectedItem.url)
                return safeUrl ? (
                  <a
                    href={safeUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="vf-external-link"
                  >
                    Open source item ↗
                  </a>
                ) : null
              })()}

              {selectedItem.claims.length > 0 ? (
                <div className="vf-inspector-subblock">
                  <h3>Matched claims ({selectedItem.claims.length})</h3>
                  <ul className="vf-linked-claims-list">
                    {selectedItem.claims.map((c) => (
                      <li key={`${c.claim_id}-${c.stance}`} className="vf-linked-claim-item">
                        <div className="vf-linked-claim-head">
                          <span className="vf-tag">{c.stance}</span>
                          <button
                            type="button"
                            className="vf-link-button"
                            onClick={() => onSelectClaimId(c.claim_id)}
                          >
                            Open claim card →
                          </button>
                        </div>
                        <p className="vf-evidence-quote">“{c.span}”</p>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </div>
          ) : null}

          {!loading && !selectedItem && selectedClaim ? (
            <div className="vf-inspector-section">
              <ClaimCard claim={selectedClaim} onSelectItem={onSelectItemId} />
            </div>
          ) : null}

          {!loading && !selectedItem && !selectedClaim && focusedStoryline ? (
            <div className="vf-inspector-section">
              <div className="vf-inspector-title-row">
                <span
                  className="vf-colour-dot"
                  style={{ backgroundColor: focusedColour }}
                  aria-hidden="true"
                />
                <span className="vf-inspector-title">{focusedStoryline.name}</span>
              </div>

              <dl className="vf-meta-grid">
                <div>
                  <dt>Items at playhead</dt>
                  <dd>
                    <button
                      type="button"
                      className="vf-count-expand-btn"
                      aria-expanded={itemsExpanded}
                      onClick={() => setItemsExpanded((prev) => !prev)}
                      title="Expand matching items at this playhead"
                    >
                      {formatCount(focusedStoryline.count)}{' '}
                      <span aria-hidden="true">{itemsExpanded ? '▴' : '▾'}</span>
                    </button>
                  </dd>
                </div>
                <div>
                  <dt>First seen</dt>
                  <dd>
                    {focusedStoryline.first_seen ? (
                      <button
                        type="button"
                        className="vf-link-button"
                        onClick={() => onSelectItemId(focusedStoryline.first_seen!.item_id)}
                      >
                        {formatPlatformLabel(focusedStoryline.first_seen.platform)} ·{' '}
                        {formatHourOffset(focusedStoryline.first_seen.hour)}
                      </button>
                    ) : (
                      'Not yet seen'
                    )}
                  </dd>
                </div>
                <div>
                  <dt>Prompt agreement</dt>
                  <dd>
                    {focusedStoryline.agreement !== null && focusedStoryline.agreement !== undefined
                      ? `${Math.round(focusedStoryline.agreement * 100)}%`
                      : '[n]%'}
                  </dd>
                </div>
              </dl>

              {itemsExpanded ? (
                <div className="vf-inspector-subblock">
                  <h3>Items at {formatHourOffset(currentHour)}</h3>
                  {storylineItems.length === 0 ? (
                    <p className="vf-muted-empty">
                      No frame items loaded for this storyline up to {formatHourOffset(currentHour)}.
                    </p>
                  ) : (
                    <ul className="vf-expandable-items">
                      {storylineItems.map((node) => (
                        <li key={node.id}>
                          <button
                            type="button"
                            className="vf-item-row-btn"
                            onClick={() => onSelectItemId(node.id)}
                          >
                            <span className="vf-tag">{formatPlatformLabel(node.platform)}</span>
                            <span className="vf-item-row-label">{node.label}</span>
                            <span className="vf-muted-num">{formatHourOffset(node.hour)}</span>
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              ) : null}

              <div className="vf-inspector-subblock">
                <h3>Volume by hour</h3>
                <VolumeSparkline
                  volumeByHour={focusedStoryline.volume_by_hour ?? []}
                  currentHour={currentHour}
                  colour={focusedColour}
                />
              </div>

              {focusedStoryline.platforms_reached.length > 0 ? (
                <div className="vf-inspector-subblock">
                  <h3>Platforms reached</h3>
                  <div className="vf-path-chips">
                    {focusedStoryline.platforms_reached.map((p) => (
                      <span key={`${p.platform}-${p.hour}`} className="vf-tag">
                        {formatPlatformLabel(p.platform)} ({formatHourOffset(p.hour)})
                      </span>
                    ))}
                  </div>
                </div>
              ) : null}

              {focusedStoryline.jumps && focusedStoryline.jumps.length > 0 ? (
                <div className="vf-inspector-subblock">
                  <h3>Cross-platform jumps ({focusedStoryline.jumps.length})</h3>
                  <ul className="vf-jumps-list">
                    {focusedStoryline.jumps.map((jump, idx) => (
                      <li key={`${jump.from_item}-${jump.to_item}-${idx}`} className="vf-jump-row">
                        <span className="vf-tag">{formatHourOffset(jump.hour)}</span>
                        <button
                          type="button"
                          className="vf-link-button"
                          onClick={() => onSelectItemId(jump.from_item)}
                        >
                          {jump.from_item}
                        </button>
                        <span aria-hidden="true">→</span>
                        <button
                          type="button"
                          className="vf-link-button"
                          onClick={() => onSelectItemId(jump.to_item)}
                        >
                          {jump.to_item}
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}

              {focusedStoryline.stance_mix ? (
                <div className="vf-inspector-subblock">
                  <h3>Stance mix</h3>
                  <div className="vf-path-chips">
                    {Object.entries(focusedStoryline.stance_mix).map(([stance, val]) => (
                      <span key={stance} className="vf-tag">
                        {stance}: {formatCount(val)}
                      </span>
                    ))}
                  </div>
                </div>
              ) : null}

              {focusedStoryline.top_amplifiers && focusedStoryline.top_amplifiers.length > 0 ? (
                <div className="vf-inspector-subblock">
                  <h3>Top public amplifiers</h3>
                  <ul className="vf-amplifier-list">
                    {focusedStoryline.top_amplifiers.map((amp) => (
                      <li key={amp.label}>
                        <span>{amp.label}</span>
                        <span className="vf-muted-num">{formatCount(amp.reach)}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}

              {relevantSignals.length > 0 ? (
                <div className="vf-inspector-subblock vf-signals-block">
                  <h3>Timing signals</h3>
                  {relevantSignals.map((sig, idx) => (
                    <p key={`${sig.storyline_id}-${idx}`} className="vf-signal-note">
                      {sig.text}
                    </p>
                  ))}
                </div>
              ) : null}
            </div>
          ) : null}

          {!loading && !selectedItem && !selectedClaim && !focusedStoryline ? (
            <div className="vf-inspector-empty">
              <p>
                Select a storyline on the left, click any node in the graph, or inspect a claim to
                view verbatim evidence spans and provenance.
              </p>
              {signals.length > 0 ? (
                <div className="vf-inspector-subblock vf-signals-block">
                  <h3>Timing signals ({signals.length})</h3>
                  {signals.map((sig, idx) => (
                    <p key={`${sig.storyline_id}-${idx}`} className="vf-signal-note">
                      {sig.text}
                    </p>
                  ))}
                </div>
              ) : null}
            </div>
          ) : null}
        </>
      )}
    </aside>
  )
}
