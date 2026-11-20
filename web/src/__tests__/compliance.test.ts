import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { describe, it } from 'node:test'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const SRC_ROOT = resolve(__dirname, '..')

function collectSourceFiles(dir: string): string[] {
  const results: string[] = []
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const fullPath = join(dir, entry.name)
    if (entry.isDirectory()) {
      if (entry.name === '__tests__') continue
      results.push(...collectSourceFiles(fullPath))
    } else if (entry.isFile() && /\.(ts|tsx|js|jsx|css)$/.test(entry.name)) {
      results.push(fullPath)
    }
  }
  return results
}

function readSrc(relPath: string): string {
  return readFileSync(join(SRC_ROOT, relPath), 'utf8')
}

describe('Static UI compliance & demo-script contracts', () => {
  it('ClaimCard.tsx uses neutral shape+text statuses, Self-reported badge, conflict disclosure, and safe URLs', () => {
    const claimCard = readSrc('components/ClaimCard.tsx')
    const palette = readSrc('utils/palette.ts')

    assert.match(claimCard, /getClaimStatusPresentation/)
    for (const status of ['supported', 'contradicted', 'mixed', 'unverified']) {
      assert.match(palette, new RegExp(`['"]${status}['"]`))
    }
    assert.match(claimCard, /Self-reported/)
    assert.match(claimCard, /Disclosed conflict:/)
    assert.match(claimCard, /sanitizeHttpUrl/)
  })

  it('BeforeAfterPage.tsx enforces descriptive non-causal copy and VeriFact definitions', () => {
    const beforeAfter = readSrc('pages/BeforeAfterPage.tsx')
    assert.match(beforeAfter, /Descriptive comparison, not a causal estimate\./)
    assert.match(beforeAfter, /VeriFact's definition:/)
  })

  it('Timeline.tsx and WorkspacePage.tsx enforce keyboard controls, time unconfirmed label, and T0 / response jumps', () => {
    const timeline = readSrc('components/Timeline.tsx')
    const workspace = readSrc('pages/WorkspacePage.tsx')

    assert.match(timeline, /time unconfirmed/)
    assert.match(timeline, /Jump to response/)
    assert.match(timeline, /resolveTurningPointHour/)

    assert.match(workspace, /ArrowLeft/)
    assert.match(workspace, /ArrowRight/)
    assert.match(workspace, /event\.key === ' '/)
    assert.match(workspace, /'r'.*'R'|'R'.*'r'/)
    assert.match(workspace, /Escape/)
    assert.match(workspace, /resolveInitialPlayheadHour/)
    assert.match(workspace, /resolveTurningPointHour/)
  })

  it('contains zero innerHTML / dangerouslySetInnerHTML across web/src/', () => {
    const forbiddenDomPattern = new RegExp(
      [['inner', 'HTML'].join(''), ['dangerously', 'SetInnerHTML'].join('')].join('|')
    )
    for (const file of collectSourceFiles(SRC_ROOT)) {
      const content = readFileSync(file, 'utf8')
      assert.doesNotMatch(
        content,
        forbiddenDomPattern,
        `Forbidden DOM injection pattern found in ${relative(SRC_ROOT, file)}`
      )
    }
  })

  it('contains zero event-specific literals and zero banned status/account/causal/suppression words across web/src/', () => {
    const eventLiteral = new RegExp(['sar', 'vam'].join(''), 'i')
    const bannedTerms = [
      ['ver', 'dict'].join(''),
      ['is_', 'true'].join(''),
      ['is_', 'false'].join(''),
      ['fa', 'ke'].join(''),
      ['truth_', 'score'].join(''),
      ['credibility_', 'score'].join(''),
      ['risk_', 'score'].join(''),
      `\\b${['bo', 't'].join('')}s?\\b`,
      ['tro', 'll'].join(''),
      ['sock', 'puppet'].join(''),
      ['inauth', 'entic'].join(''),
      ['cau', 'sed'].join(''),
      ['led ', 'to'].join(''),
      ['resulted ', 'in'].join(''),
      ['take', 'down'].join(''),
      ['flag_', 'content'].join('')
    ]
    const bannedPattern = new RegExp(bannedTerms.join('|'), 'i')

    for (const file of collectSourceFiles(SRC_ROOT)) {
      const content = readFileSync(file, 'utf8')
      const rel = relative(SRC_ROOT, file)
      assert.doesNotMatch(content, eventLiteral, `Event-specific literal found in ${rel}`)
      assert.doesNotMatch(content, bannedPattern, `Banned term found in ${rel}`)
    }
  })
})
