# Design

What the screens look like and how they behave. Screen requirements are the FR IDs in `PRD.md` (Functional requirements). This file adds the visual system and copy rules. Reference mockup: [VeriFact narrative workspace mockup](https://claude.ai/artifact/WZ7Hg8BQ4VRtWjeYnnVDtn). Its numbers are placeholders, and its Reddit and LinkedIn items are out of date (see PRD, Mockup corrections).

## Direction
- **Nebula style:** a dark canvas, a WebGL point cloud with glow, storyline hubs with fanned sub-threads (in the spirit of Obsidian and Blackbird Constellation).
- **One claim card everywhere:** graph inspector, Check page, extension. One component, one data shape (`api.md`).
- **Calm and factual:** the interface reports evidence. It never alarms, accuses or delivers a verdict.

## Layout: workspace (`/e/{event_id}`)
```
┌ Header: event name · T0 UTC + IST · config file · nav (Graph | Before/after | Brief | Check a claim) ┐
│ Storyline rail │            Graph (Sigma.js)            │ Inspector         │
│ (FR-2)         │  clock top-right (FR-3.6)              │ storyline / item  │
│                │  legend bottom-right, settings (FR-3.5)│ / claim card (FR-5)│
├────────────────┴── Timeline streamgraph + markers + controls (FR-4) ─────────┤
└ Trust strip: coverage · gold-set accuracy · unassigned · backfilled share (FR-6) ┘
```
Before/after (FR-7) and Brief (FR-8, A4 page) are separate routes with the same header. The mockup's older styling on those two screens is restyled to match the workspace.

## Visual encoding
| Thing | Encoding |
| --- | --- |
| Storyline | One colour per storyline from a categorical palette with no red/green pair; Unassigned is neutral grey |
| Reach | Node size |
| Recency | Glow; items under 2 h old at the playhead are white (FR-3.2) |
| Official item | Diamond, with "self-reported" in the tooltip and inspector |
| The response | Gold diamond, with a gold dashed marker on the timeline |
| Disclosed conflict | Gold ring on the node, plus the disclosure text beside the quote |
| Reply, quote | Faint line |
| Platform jump | White dashed line, only for explicit links |
| Contradicts | Gold dotted line |
| New arrival | One ripple (FR-3.7) |

Evidence status uses a **text label plus a shape icon** (for example ● supported, ○ contradicted, ◐ mixed, ◌ unverified), drawn in neutral tones. Never red/green, never a ✓/✗ pair, never colour alone (P1.5).

## Interaction rules
- Every view reads one shared time value (`hour`) and one data source (FR-4.5). Moving the slider updates graph, timeline, rail, inspector, clock and trust strip together.
- Nothing later than the playhead is ever drawn or counted.
- Focusing a storyline dims everything else to 10–15% opacity. Clicking again clears focus.
- Keyboard: arrow keys move one hour, Space plays or pauses, R jumps to the response, Esc clears focus. Nodes have a visible focus ring.
- The live pulse appears only when live ingestion is on. In replay mode it is hidden, not shown as frozen.
- Empty states: "Not yet seen" (storyline before its first item); "Collecting since [time]" (new event with no data).

## Copy rules
- Say what the data shows: "First seen on X at T+1h", "Reached HN at T+6h", "Volume fell from [n]/h to [m]/h after the response".
- Unconfirmed things say so: "time unconfirmed", "self-reported", "time approximate".
- Before/after: always "Descriptive comparison, not a causal estimate". Pattern names (Faded, Sustained, Reframed, Not addressed, Source of claims) carry a tooltip: "VeriFact's definition: …".
- Timing signal, word for word (FR-5.4): "[n] near-identical posts within [m] minutes. This can reflect organic copy-paste or coordination; no account is labelled."
- Banned in UI copy: true, false, fake, debunked, bot, troll, misinformation (about a post), caused, led to, drove.
- Unknown numbers show as `[n]`, never as a guess.

## Brief (A4)
One page, printable and exportable as PDF. Sections in FR-8.2 order. Every summary sentence ends with linked item references. A footer gives the method, the limits, the coverage, `generated_at`, and the model and prompt versions.

## Extension
Right-click on selected text → a small overlay with the claim card → "See the whole story" opens `/e/{event_id}?storyline=…&claim=…&t=…`. The extension sends only the selected text or screenshot and the page URL to `check`. It never sends the user's identity.

## Performance
A few thousand points with glow at 60 fps in recent desktop Chrome (at least 30 fps on the judging laptop). Replay steps never block on network calls: frames are prefetched.
