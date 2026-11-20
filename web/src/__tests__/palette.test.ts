import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import {
  STORYLINE_PALETTE,
  UNASSIGNED_COLOUR,
  getClaimStatusPresentation,
  getStorylineColour,
  hexToRgba
} from '../utils/palette.ts'

function parseHexRgb(hex: string): { r: number; g: number; b: number } {
  const clean = hex.replace('#', '')
  assert.equal(clean.length, 6, `Expected 6-digit hex colour, got ${hex}`)
  return {
    r: Number.parseInt(clean.slice(0, 2), 16),
    g: Number.parseInt(clean.slice(2, 4), 16),
    b: Number.parseInt(clean.slice(4, 6), 16)
  }
}

describe('palette.ts', () => {
  it('uses a distinct categorical palette with no red/green pair', () => {
    assert.ok(STORYLINE_PALETTE.length >= 6)
    const unique = new Set(STORYLINE_PALETTE)
    assert.equal(unique.size, STORYLINE_PALETTE.length)

    let hasPureRed = false
    let hasPureGreen = false
    for (const hex of STORYLINE_PALETTE) {
      const { r, g, b } = parseHexRgb(hex)
      if (r > 200 && g < 100 && b < 100) hasPureRed = true
      if (g > 180 && r < 100 && b < 120) hasPureGreen = true
    }
    assert.equal(hasPureRed && hasPureGreen, false, 'Palette must never include a red/green pair')
    assert.equal(hasPureRed, false, 'Palette avoids alarm red')
    assert.equal(hasPureGreen, false, 'Palette avoids alarm/status green')
  })

  it('maps unassigned and missing storylines to neutral slate grey', () => {
    assert.equal(getStorylineColour(null), UNASSIGNED_COLOUR)
    assert.equal(getStorylineColour(undefined), UNASSIGNED_COLOUR)
    assert.equal(getStorylineColour(''), UNASSIGNED_COLOUR)
    assert.equal(getStorylineColour('unassigned'), UNASSIGNED_COLOUR)
  })

  it('maps storyline IDs deterministically by colourIndex or hash', () => {
    assert.equal(getStorylineColour('story-a', 0), STORYLINE_PALETTE[0])
    assert.equal(
      getStorylineColour('story-a', STORYLINE_PALETTE.length + 2),
      STORYLINE_PALETTE[2]
    )
    const hashed = getStorylineColour('story-deterministic')
    assert.ok((STORYLINE_PALETTE as readonly string[]).includes(hashed))
    assert.equal(getStorylineColour('story-deterministic'), hashed)
  })

  it('converts hex to rgba and falls back safely on invalid hex', () => {
    assert.equal(hexToRgba('#38bdf8', 0.5), 'rgba(56, 189, 248, 0.5)')
    assert.equal(hexToRgba('bad', 0.25), 'rgba(100, 116, 139, 0.25)')
    assert.equal(hexToRgba('#zzzzzz', 0.12), 'rgba(100, 116, 139, 0.12)')
  })

  it('returns neutral shape + text labels for every claim status', () => {
    const statuses = ['supported', 'contradicted', 'mixed', 'unverified'] as const
    const symbols = new Set<string>()
    for (const status of statuses) {
      const pres = getClaimStatusPresentation(status)
      assert.equal(pres.label, status)
      assert.ok(pres.symbol.length > 0)
      assert.doesNotMatch(pres.symbol, /[✓✗✔✘]/)
      assert.ok(pres.className.startsWith('status-'))
      symbols.add(pres.symbol)
    }
    assert.equal(symbols.size, 4)
  })
})
