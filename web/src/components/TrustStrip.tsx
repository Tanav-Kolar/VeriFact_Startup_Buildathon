import type { TrustCoverageEntry, TrustResponse } from '../types/api'
import { formatCount, formatPercent, formatPlatformLabel } from '../utils/format'

export interface TrustStripProps {
  trust: TrustResponse | null
}

function renderCoverageItem(source: string, entry: TrustCoverageEntry | number | null | undefined): string {
  const label = formatPlatformLabel(source)
  if (entry === null || entry === undefined) {
    return `${label}: [n]`
  }
  if (typeof entry === 'number') {
    return `${label}: ${formatCount(entry)}`
  }
  const countStr = formatCount(entry.count)
  if (entry.cap !== null && entry.cap !== undefined) {
    return `${label}: ${countStr}/${entry.cap}`
  }
  return `${label}: ${countStr}`
}

export function TrustStrip({ trust }: TrustStripProps) {
  const coverageEntries = trust ? Object.entries(trust.coverage ?? {}) : []
  const goldAccuracy = trust?.gold?.assignment_accuracy
  const goldF1 = trust?.gold?.stance_macro_f1
  const goldDate = trust?.gold?.run_date

  return (
    <footer className="vf-trust-strip" aria-label="Coverage and evaluation trust strip">
      <div className="vf-trust-group">
        <span className="vf-trust-label">Coverage</span>
        {coverageEntries.length === 0 ? (
          <span className="vf-trust-value">[n]</span>
        ) : (
          coverageEntries.map(([source, val]) => (
            <span key={source} className="vf-trust-pill">
              {renderCoverageItem(source, val)}
            </span>
          ))
        )}
      </div>

      <div className="vf-trust-group">
        <span className="vf-trust-label">Gold set</span>
        <span className="vf-trust-value">
          Assignment {formatPercent(goldAccuracy)} · Macro-F1 {formatPercent(goldF1)}
          {goldDate ? ` (${goldDate})` : ''}
        </span>
      </div>

      <div className="vf-trust-group">
        <span className="vf-trust-label">Unassigned</span>
        <span className="vf-trust-value">{formatCount(trust?.unassigned_count)}</span>
      </div>

      <div className="vf-trust-group">
        <span className="vf-trust-label">Backfilled share</span>
        <span className="vf-trust-value">{formatPercent(trust?.backfilled_share)}</span>
      </div>
    </footer>
  )
}
