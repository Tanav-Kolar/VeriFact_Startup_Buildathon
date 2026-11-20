import type {
  ClaimCardData,
  ItemDetail,
  StorylineDetail,
  TimingSignal
} from '../types/api'
import {
  formatCount,
  formatHourOffset,
  formatIstTimestamp,
  formatPlatformLabel,
  formatUtcTimestamp
} from '../utils/format'
import { ClaimCard } from './ClaimCard'

export interface InspectorProps {
  selectedItem: ItemDetail | null
  selectedClaim: ClaimCardData | null
  focusedStoryline: StorylineDetail | null
  signals: TimingSignal[]
  loading?: boolean
  onSelectClaimId: (claimId: string) => void
  onSelectItemId: (itemId: string) => void
  onClearSelection: () => void
}

function isSafeHttpUrl(rawUrl: string | null | undefined): rawUrl is string {
  if (!rawUrl) return false
  try {
    const parsed = new URL(rawUrl)
    return parsed.protocol === 'http:' || parsed.protocol === 'https:'
  } catch {
    return false
  }
}

export function Inspector({
  selectedItem,
  selectedClaim,
  focusedStoryline,
  signals,
  loading = false,
  onSelectClaimId,
  onSelectItemId,
  onClearSelection
}: InspectorProps) {
  const relevantSignals = focusedStoryline
    ? signals.filter((s) => s.storyline_id === focusedStoryline.id)
    : signals

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

      {loading ? <p className="vf-muted-empty">Loading details…</p> : null}

      {!loading && selectedItem ? (
        <div className="vf-inspector-section">
          <div className="vf-inspector-badges">
            <span className="vf-tag">{formatPlatformLabel(selectedItem.platform)}</span>
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
              <dt>Collected</dt>
              <dd>
                {formatUtcTimestamp(selectedItem.collected_at)}
                {selectedItem.collection_lag_s !== null && selectedItem.collection_lag_s !== undefined
                  ? ` (+${selectedItem.collection_lag_s}s)`
                  : ''}
              </dd>
            </div>
            <div>
              <dt>Reach</dt>
              <dd>{formatCount(selectedItem.reach)}</dd>
            </div>
          </dl>

          <blockquote className="vf-item-excerpt">{selectedItem.excerpt}</blockquote>

          {selectedItem.conflict ? (
            <div className="vf-conflict-disclosure" role="note">
              <strong>Disclosed conflict:</strong> {selectedItem.conflict.person} —{' '}
              {selectedItem.conflict.relationship}
            </div>
          ) : null}

          {isSafeHttpUrl(selectedItem.url) ? (
            <a
              href={selectedItem.url}
              target="_blank"
              rel="noopener noreferrer"
              className="vf-external-link"
            >
              Open source item ↗
            </a>
          ) : null}

          {selectedItem.claims.length > 0 ? (
            <div className="vf-inspector-subblock">
              <h3>Matched claims</h3>
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
          <div className="vf-inspector-title">{focusedStoryline.name}</div>
          <dl className="vf-meta-grid">
            <div>
              <dt>Items at playhead</dt>
              <dd>{formatCount(focusedStoryline.count)}</dd>
            </div>
            <div>
              <dt>First seen</dt>
              <dd>
                {focusedStoryline.first_seen
                  ? `${formatPlatformLabel(focusedStoryline.first_seen.platform)} · ${formatHourOffset(
                      focusedStoryline.first_seen.hour
                    )}`
                  : 'Not yet seen'}
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

          {focusedStoryline.platforms_reached.length > 0 ? (
            <div className="vf-inspector-subblock">
              <h3>Platform path</h3>
              <div className="vf-path-chips">
                {focusedStoryline.platforms_reached.map((p) => (
                  <span key={`${p.platform}-${p.hour}`} className="vf-tag">
                    {formatPlatformLabel(p.platform)} ({formatHourOffset(p.hour)})
                  </span>
                ))}
              </div>
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
        </div>
      ) : null}

      {!loading && !selectedItem && !selectedClaim && !focusedStoryline ? (
        <div className="vf-inspector-empty">
          <p>
            Select a storyline on the left, click any node in the graph, or inspect a claim to view
            verbatim evidence spans and provenance.
          </p>
        </div>
      ) : null}

      {relevantSignals.length > 0 ? (
        <div className="vf-inspector-subblock vf-signals-block">
          <h3>Timing notes</h3>
          {relevantSignals.map((sig, idx) => (
            <p key={`${sig.storyline_id}-${idx}`} className="vf-signal-note">
              {sig.text}
            </p>
          ))}
        </div>
      ) : null}
    </aside>
  )
}
