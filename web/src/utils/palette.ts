import type { ClaimStatus } from '../types/api'

/**
 * Categorical storyline palette with no red/green pair (P1.5 / design.md).
 * Unassigned always maps to neutral slate grey.
 */
export const STORYLINE_PALETTE = [
  '#38bdf8', // Sky cyan
  '#a78bfa', // Violet
  '#f59e0b', // Amber
  '#2dd4bf', // Teal
  '#f472b6', // Rose pink
  '#60a5fa', // Cobalt blue
  '#fb923c', // Tangerine
  '#c084fc'  // Lavender
] as const

export const UNASSIGNED_COLOUR = '#64748b'
export const RECENT_NODE_COLOUR = '#f8fafc'
export const RESPONSE_GOLD_COLOUR = '#facc15'
export const DIMMED_NODE_COLOUR = '#182235'
export const DIMMED_EDGE_COLOUR = 'rgba(100, 116, 139, 0.12)'

export function getStorylineColour(storylineId: string | null | undefined, colourIndex?: number): string {
  if (!storylineId || storylineId === 'unassigned') {
    return UNASSIGNED_COLOUR
  }
  const idx = typeof colourIndex === 'number' && colourIndex >= 0
    ? colourIndex % STORYLINE_PALETTE.length
    : hashString(storylineId) % STORYLINE_PALETTE.length
  return STORYLINE_PALETTE[idx]
}

export function hashString(input: string): number {
  let hash = 2166136261
  for (let i = 0; i < input.length; i += 1) {
    hash ^= input.charCodeAt(i)
    hash = Math.imul(hash, 16777619)
  }
  return hash >>> 0
}

export interface StatusPresentation {
  symbol: string
  label: string
  className: string
}

/**
 * Neutral shape + text status encoding (P1.3, P1.5).
 * Never uses red/green or checkmark/cross pairs.
 */
export function getClaimStatusPresentation(status: ClaimStatus): StatusPresentation {
  switch (status) {
    case 'supported':
      return { symbol: '●', label: 'supported', className: 'status-supported' }
    case 'contradicted':
      return { symbol: '○', label: 'contradicted', className: 'status-contradicted' }
    case 'mixed':
      return { symbol: '◐', label: 'mixed', className: 'status-mixed' }
    case 'unverified':
    default:
      return { symbol: '◌', label: 'unverified', className: 'status-unverified' }
  }
}
