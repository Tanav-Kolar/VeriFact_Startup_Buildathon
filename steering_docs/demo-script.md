# Demo script (3 minutes)

One story in five beats, run from precomputed replay frames (`GRAPH_BACKEND=frames`), so nothing on stage depends on a live API call. Source: PRD, Three-minute demo script.

| Time | Beat | On screen | Say (gist, not a script to read) |
| --- | --- | --- | --- |
| 0:00–0:20 | Open | Deployed link opens on the Sarvam event at T0: the launch post (white diamond) and the official blog only | "Sarvam launched Token Factory. Here's how the story about it formed." |
| 0:20–1:10 | Replay | Press Replay. Storylines light up, the stream grows, platform badges light as storylines reach HN, YouTube and the press. Focus one storyline and show its first-seen post and platform path | Where it started, when it jumped, who amplified it, as a finding from the data (first seen on X in [n] of [m] storylines), not a premise |
| 1:10–1:50 | Response | "Jump to response". Gold diamond at about T+24h, "time unconfirmed" until confirmed. Inspector shows the GPU-subsidy claim card: contradicted by a company statement, labelled self-reported | "Evidence, not verdicts: here is what supports and contradicts it, and who said it." |
| 1:50–2:30 | Before/after | GPU-subsidy storyline (addressed) next to "Chinese models" (not addressed), with the "descriptive, not causal" note | What each storyline did after the response |
| 2:30–3:00 | Brief and trust | One-page brief, then the trust strip: coverage per source, gold-set accuracy, unassigned count | "A new event is one config file." |

**If time remains:** Check a claim with a pasted post (same claim card), then the extension's "See the whole story" deep link landing in the graph.

## Pre-flight checklist (day of the demo)
- [ ] Deployed link opens on the Sarvam event in under 5 s on the venue network, with no login.
- [ ] `GRAPH_BACKEND=frames`. Live ingestion off unless rehearsed live that day.
- [ ] Full replay at 30 fps or better on the judging laptop. Browser zoom 100%, other tabs closed.
- [ ] Response marker shows its current confirmation status, and the copy matches the latest facts.
- [ ] All numbers on screen come from data. No `[n]` placeholders left on the demo path.
- [ ] Brief regenerated from the final data and checked against the review rubric (`evaluation.md` §4).
- [ ] Backup: a screen recording of the full 3 minutes, available offline.
- [ ] Extension zip and demo video links work.
- [ ] Two full rehearsals timed under 3:00 without a stall (PRD ramp gate).

## Likely jury questions (have the evidence ready)
- How do you know a storyline is real? Show the gold-set accuracy and the Unassigned bucket.
- Is Sarvam's claim true? We show the evidence and that it is self-reported. We don't issue verdicts.
- Did the response work? We show what happened before and after, descriptively. We don't claim causation.
- What about bots? We show neutral timing signals and never label accounts.
- Does it generalise? Open a second event's config file.
