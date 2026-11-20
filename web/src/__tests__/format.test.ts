import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import {
  DEFAULT_UNCONFIRMED_TURNING_POINT_HOUR,
  computeHourFromT0,
  formatBriefPlaceholder,
  formatCount,
  formatHourOffset,
  formatIstTimestamp,
  formatLagSeconds,
  formatPercent,
  formatT0ShortIst,
  formatUtcTimestamp,
  isSafeHttpUrl,
  resolveInitialPlayheadHour,
  resolveTurningPointHour,
  sanitizeHttpUrl,
  truncateExcerpt
} from '../utils/format.ts'

describe('format.ts', () => {
  it('formats UTC and IST T0 timestamps accurately including day rollover', () => {
    const iso = '2026-11-16T20:00:00Z'
    assert.equal(formatUtcTimestamp(iso), '2026-11-16 20:00 UTC')
    assert.equal(formatIstTimestamp(iso), '2026-11-17 01:30 IST')
    assert.equal(formatT0ShortIst(iso, 0), '17 Nov 01:30 IST')
    assert.equal(formatT0ShortIst(iso, 2), '17 Nov 03:30 IST')

    assert.equal(formatUtcTimestamp(null), '[time]')
    assert.equal(formatIstTimestamp('not-a-date'), '[time]')
    assert.equal(formatT0ShortIst(undefined), '[time]')
  })

  it('formats hour offsets and computes rounded hours from T0', () => {
    assert.equal(formatHourOffset(0), 'T0')
    assert.equal(formatHourOffset(24), 'T+24h')
    assert.equal(formatHourOffset(-6), 'T-6h')

    assert.equal(computeHourFromT0('2026-11-16T10:00:00Z', '2026-11-17T10:00:00Z'), 24)
    assert.equal(computeHourFromT0('2026-11-16T10:00:00Z', null), null)
    assert.equal(computeHourFromT0(null, '2026-11-17T10:00:00Z'), null)
  })

  it('truncates excerpts strictly to <= 300 chars', () => {
    assert.equal(truncateExcerpt(null), '')
    assert.equal(truncateExcerpt('short excerpt'), 'short excerpt')

    const exact300 = 'a'.repeat(300)
    assert.equal(truncateExcerpt(exact300), exact300)
    assert.equal(truncateExcerpt(exact300).length, 300)

    const long500 = 'b'.repeat(500)
    const truncated = truncateExcerpt(long500)
    assert.equal(truncated.length, 300)
    assert.ok(truncated.endsWith('…'))
  })

  it('renders [n] bracketed placeholders for missing numeric/brief values', () => {
    assert.equal(formatCount(null), '[n]')
    assert.equal(formatCount(undefined, '/h'), '[n]/h')
    assert.equal(formatCount(Number.NaN), '[n]')
    assert.equal(formatCount(42, '/h'), '42/h')

    assert.equal(formatPercent(null), '[n]%')
    assert.equal(formatPercent(0.84), '84%')
    assert.equal(formatPercent(84), '84%')

    assert.equal(formatLagSeconds(undefined), '[n]s')
    assert.equal(formatLagSeconds(45), '45s')
    assert.equal(formatLagSeconds(125), '2m 5s')

    assert.equal(formatBriefPlaceholder(null), '[n]')
    assert.equal(formatBriefPlaceholder('   '), '[n]')
    assert.equal(formatBriefPlaceholder('gemini-2.5-flash-lite'), 'gemini-2.5-flash-lite')
  })

  it('allows only http/https URLs and rejects javascript:/data: URLs', () => {
    assert.equal(isSafeHttpUrl('https://example.com/post/1'), true)
    assert.equal(isSafeHttpUrl('http://example.com/post/1'), true)
    assert.equal(sanitizeHttpUrl('  https://example.com/a  '), 'https://example.com/a')

    const unsafeInputs = [
      'javascript:alert(1)',
      'JAVASCRIPT:alert(1)',
      '  javascript:alert(1)',
      'data:text/html,<script>alert(1)</script>',
      'vbscript:msgbox(1)',
      'file:///etc/passwd',
      '/relative/path',
      '',
      '   ',
      null,
      undefined
    ]
    for (const bad of unsafeInputs) {
      assert.equal(isSafeHttpUrl(bad), false, `Expected unsafe URL to be rejected: ${String(bad)}`)
      assert.equal(sanitizeHttpUrl(bad), null, `Expected sanitizeHttpUrl to return null: ${String(bad)}`)
    }
  })

  it('resolves turning point hour cleanly including unconfirmed at_utc=null fallback', () => {
    assert.equal(resolveTurningPointHour('2026-11-16T10:00:00Z', null), null)
    assert.equal(
      resolveTurningPointHour('2026-11-16T10:00:00Z', { at_utc: '2026-11-17T10:00:00Z' }),
      24
    )
    assert.equal(
      resolveTurningPointHour('2026-11-16T10:00:00Z', { at_utc: null }, 18),
      18
    )
    assert.equal(
      resolveTurningPointHour('2026-11-16T10:00:00Z', { at_utc: null }, null),
      DEFAULT_UNCONFIRMED_TURNING_POINT_HOUR
    )
  })

  it('defaults initial playhead to T0 (0) when frames cover T0 and honours ?t= param', () => {
    assert.equal(resolveInitialPlayheadHour(null, [{ hour: -2 }, { hour: 0 }, { hour: 24 }]), 0)
    assert.equal(resolveInitialPlayheadHour(null, []), 0)
    assert.equal(resolveInitialPlayheadHour('24', [{ hour: 0 }, { hour: 48 }]), 24)
    assert.equal(resolveInitialPlayheadHour('bad', [{ hour: -1 }, { hour: 10 }]), 0)
    assert.equal(resolveInitialPlayheadHour(null, [{ hour: 5 }, { hour: 12 }]), 5)
  })
})
