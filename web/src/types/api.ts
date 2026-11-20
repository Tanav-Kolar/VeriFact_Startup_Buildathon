export interface ApiErrorBody {
  error?: {
    code?: string
    message?: string
  }
}

export interface EventSummary {
  event_id: string
  name: string
  t0_utc: string
}

export interface TurningPoint {
  id: string
  label: string
  at_utc: string | null
  confirmed: boolean
  form: string | null
}

export interface EventDetail {
  event_id?: string
  name: string
  t0_utc: string
  config_file: string
  sources: string[]
  turning_points: TurningPoint[]
  now_hour: number | null
}

export interface ConflictDisclosure {
  person: string
  relationship: string
}

export type NodeType = 'post' | 'article' | 'account' | 'outlet' | 'storyline' | 'claim'

export interface GraphNodeData {
  id: string
  type: NodeType
  platform: string
  kind: string
  created_at: string
  hour: number
  storyline_id: string | null
  reach: number | null
  is_official: boolean
  self_reported: boolean
  conflict: ConflictDisclosure | null
  lang: string | null
  label: string
  is_response?: boolean
}

export type EdgeType =
  | 'replied_to'
  | 'quoted'
  | 'links_to'
  | 'authored_by'
  | 'belongs_to'
  | 'supports'
  | 'contradicts'

export interface GraphEdgeData {
  source: string
  target: string
  type: EdgeType
  created_at: string
  hour: number
}

export interface FrameData {
  hour: number
  nodes_added: GraphNodeData[]
  edges_added: GraphEdgeData[]
  storyline_counts: Record<string, number>
}

export interface FirstSeen {
  item_id: string
  platform: string
  hour: number
}

export interface PlatformReached {
  platform: string
  hour: number
}

export interface PublicAmplifier {
  label: string
  reach: number
}

export interface StorylineRow {
  id: string
  name: string
  colour_index: number
  count: number | null
  first_seen: FirstSeen | null
  platforms_reached: PlatformReached[]
  stance_mix?: Record<string, number> | null
  agreement?: number | null
  top_amplifiers?: PublicAmplifier[]
}

export interface StorylineDetail extends StorylineRow {
  volume_by_hour?: Array<{ hour: number; count: number }>
  jumps?: Array<{ from_item: string; to_item: string; hour: number }>
}

export interface ItemClaimLink {
  claim_id: string
  stance: string
  span: string
}

export interface ItemDetail {
  item_id?: string
  created_at: string
  collected_at: string
  collection_lag_s: number | null
  platform: string
  url: string | null
  excerpt: string
  storyline_id: string | null
  claims: ItemClaimLink[]
  reach: number | null
  tags: string[]
  is_official?: boolean
  self_reported?: boolean
  conflict?: ConflictDisclosure | null
  lang?: string | null
  label?: string
}

export type ClaimStatus = 'supported' | 'contradicted' | 'mixed' | 'unverified'

export interface ClaimEvidence {
  item_id: string
  url: string | null
  platform: string
  source_type: string
  stance: string
  span: string
  created_at: string
  conflict: ConflictDisclosure | null
}

export interface ClaimCardData {
  claim_id: string
  text: string
  status: ClaimStatus
  counts: {
    supporting: number
    contradicting: number
    neutral: number
  }
  self_reported: boolean
  self_reported_note?: string | null
  evidence: ClaimEvidence[]
  storyline_id: string | null
  deep_link?: string | null
}

export interface TimingSignal {
  storyline_id: string
  n_items: number
  window_minutes: number
  text: string
}

export interface BeforeAfterRow {
  storyline_id: string
  name?: string
  volume_before_per_h: number | null
  volume_after_per_h: number | null
  pattern: string | null
  addressed: boolean
  platform_path?: PlatformReached[]
  hourly_volumes?: Array<{ hour: number; count: number }>
}

export interface BeforeAfterResponse {
  turning_point?: TurningPoint | null
  confirmed: boolean
  form: string | null
  note: string
  storylines?: BeforeAfterRow[]
  rows?: BeforeAfterRow[]
}

export interface BriefSummarySentence {
  text: string
  item_ids: string[]
}

export interface BriefStorylineRow {
  id: string
  name: string
  volume: number | null
  origin_and_spread: string
  after_response: string
  top_amplifier: string | null
}

export interface BriefResponse {
  summary: BriefSummarySentence[]
  storylines: BriefStorylineRow[]
  claims: ClaimCardData[]
  watch: string[]
  evidence_to_cite: BriefSummarySentence[]
  method_and_limits: string
  coverage: Record<string, unknown>
  generated_at: string | null
  model_id: string | null
  prompt_version: string | null
}

export interface TrustCoverageEntry {
  count: number | null
  cap?: number | null
}

export interface TrustGoldMetrics {
  assignment_accuracy: number | null
  stance_macro_f1: number | null
  run_date: string | null
}

export interface TrustResponse {
  coverage: Record<string, TrustCoverageEntry | number | null>
  gold: TrustGoldMetrics | null
  unassigned_count: number | null
  backfilled_share: number | null
}

export interface CheckResponse {
  report_id: string
  claims: ClaimCardData[]
  matched_event_id: string | null
  notice?: string | null
}
