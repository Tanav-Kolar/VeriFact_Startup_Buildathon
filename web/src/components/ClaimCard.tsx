import { useState } from 'react'
import { Link } from 'react-router-dom'
import type { ClaimCardData, ClaimEvidence } from '../types/api'
import { formatPlatformLabel, formatUtcTimestamp, sanitizeHttpUrl } from '../utils/format'
import { getClaimStatusPresentation } from '../utils/palette'

export interface ClaimCardProps {
  claim: ClaimCardData
  showDeepLink?: boolean
  onSelectItem?: (itemId: string) => void
}

type StanceFilter = 'all' | 'supports' | 'contradicts' | 'neutral'

function formatSourceType(sourceType: string): string {
  return sourceType.replace(/_/g, ' ')
}

function matchesStanceFilter(evidence: ClaimEvidence, filter: StanceFilter): boolean {
  if (filter === 'all') return true
  const normalized = evidence.stance.toLowerCase()
  if (filter === 'supports') return normalized === 'supports' || normalized === 'supporting'
  if (filter === 'contradicts') return normalized === 'contradicts' || normalized === 'contradicting'
  return normalized === 'neutral'
}

export function ClaimCard({ claim, showDeepLink = false, onSelectItem }: ClaimCardProps) {
  const [stanceFilter, setStanceFilter] = useState<StanceFilter>('all')
  const [expanded, setExpanded] = useState(true)
  const status = getClaimStatusPresentation(claim.status)

  const filteredEvidence = claim.evidence.filter((item) => matchesStanceFilter(item, stanceFilter))

  const toggleFilter = (next: StanceFilter) => {
    if (stanceFilter === next && expanded) {
      setStanceFilter('all')
      return
    }
    setStanceFilter(next)
    setExpanded(true)
  }

  return (
    <article className="vf-claim-card" aria-label={`Claim ${claim.claim_id}`}>
      <div className="vf-claim-card-header">
        <span className={`vf-status-pill ${status.className}`}>
          <span aria-hidden="true">{status.symbol}</span>
          <span>{status.label}</span>
        </span>
        {claim.self_reported ? (
          <span className="vf-badge vf-badge-self-reported">Self-reported</span>
        ) : null}
      </div>

      <p className="vf-claim-text">{claim.text}</p>

      {claim.self_reported ? (
        <p className="vf-self-reported-note">
          {claim.self_reported_note || 'Company statement, not independently confirmed'}
        </p>
      ) : null}

      <div className="vf-claim-counts" role="group" aria-label="Evidence counts by stance">
        <button
          type="button"
          className={`vf-count-chip ${stanceFilter === 'supports' ? 'is-selected' : ''}`}
          onClick={() => toggleFilter('supports')}
        >
          Supporting ({claim.counts.supporting})
        </button>
        <button
          type="button"
          className={`vf-count-chip ${stanceFilter === 'contradicts' ? 'is-selected' : ''}`}
          onClick={() => toggleFilter('contradicts')}
        >
          Contradicting ({claim.counts.contradicting})
        </button>
        <button
          type="button"
          className={`vf-count-chip ${stanceFilter === 'neutral' ? 'is-selected' : ''}`}
          onClick={() => toggleFilter('neutral')}
        >
          Neutral ({claim.counts.neutral})
        </button>
        <button
          type="button"
          className="vf-count-toggle"
          onClick={() => setExpanded((prev) => !prev)}
        >
          {expanded ? 'Hide evidence' : `Show evidence (${claim.evidence.length})`}
        </button>
      </div>

      {expanded ? (
        <div className="vf-evidence-list">
          {filteredEvidence.length === 0 ? (
            <p className="vf-muted-empty">No evidence items recorded for this filter yet.</p>
          ) : (
            filteredEvidence.map((ev) => (
              <div key={`${ev.item_id}-${ev.stance}-${ev.created_at}`} className="vf-evidence-item">
                <div className="vf-evidence-meta">
                  <span className="vf-tag">{formatPlatformLabel(ev.platform)}</span>
                  <span className="vf-tag">{formatSourceType(ev.source_type)}</span>
                  <span className="vf-tag">{ev.stance}</span>
                  <span className="vf-evidence-time">{formatUtcTimestamp(ev.created_at)}</span>
                </div>

                <blockquote className="vf-evidence-quote">“{ev.span}”</blockquote>

                {ev.conflict ? (
                  <div className="vf-conflict-disclosure" role="note">
                    <strong>Disclosed conflict:</strong> {ev.conflict.person} — {ev.conflict.relationship}
                  </div>
                ) : null}

                <div className="vf-evidence-actions">
                  {onSelectItem ? (
                    <button
                      type="button"
                      className="vf-link-button"
                      onClick={() => onSelectItem(ev.item_id)}
                    >
                      Inspect item
                    </button>
                  ) : null}
                  {(() => {
                    const safeUrl = sanitizeHttpUrl(ev.url)
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
                </div>
              </div>
            ))
          )}
        </div>
      ) : null}

      {showDeepLink && claim.deep_link ? (
        <div className="vf-claim-card-footer">
          <Link to={claim.deep_link} className="vf-deep-link">
            See the whole story →
          </Link>
        </div>
      ) : null}
    </article>
  )
}
