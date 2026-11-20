# VeriFact Narrative Intelligence Prototype: Context

*Trimmed to what matters for building the current prototype. Facts were researched in Nov 2026; re-verify prices, API rules and anything said about Sarvam before relying on them.*

---

## 1. Problem

People and organisations can't easily see how a story forms online: where it started, how it changed as it spread, who amplified it, and whether its core claims hold up. Stories now move across X, Reddit, YouTube, forums and news within hours, often mutating along the way. Comms, risk and security teams usually find out about a hostile narrative only after it has gone viral, and then piece the evidence together by hand.

**What we're building:** a live, time-stamped narrative graph that reconstructs how a story evolved from the first official post onward. It is grounded in evidence (every node links to its source) rather than verdicts.

---

## 2. Market: what exists today

"Narrative intelligence" is now an analyst category of its own; Gartner published an Emerging Market Quadrant for Narrative Intelligence in June 2026.

| Player | What it does | Gap we exploit |
|---|---|---|
| **Blackbird.AI** (Constellation) | Narrative discovery, risk scoring, bot detection, cohort and coordination analysis; RAV3N risk LLM; API; human research team | Enterprise-only and expensive; tells you a narrative is spreading, light on whether its claims are true |
| **Cyabra** | Connects actors, behaviour and content across platforms; authenticity and coordination models; Narrative Alerts. Public via SPAC (Mar 2026), about $8.1M ARR mid-2026 | Focus is on bots and fake engagement, not on evidence for each claim |
| **Alethea, Signal AI, PeakMetrics** | Narrative and reputation monitoring | Dashboards; similar gap on claim-level evidence |
| **GDELT** | Free global news event and article data | An input source, not a competitor; covers news, not the social and messaging layer |

**Common industry model:** content + actors + behaviour → narratives → risk score → alert and brief.

**Our difference:** besides showing that a narrative is spreading and who pushes it, we show **the timeline from the first post, how the wording mutated across platforms, whether its core claims are true (with sources), and how the company's response changed the story.**

---

## 3. The case: Sarvam Token Factory

- **What it is:** Sarvam's India-hosted inference platform. It serves Sarvam 105B and open-weight models (DeepSeek, Kimi, GLM, Gemma) through one OpenAI-compatible API, priced in rupees.
- **T0:** @SarvamAI launch post on X, **2026-11-16 13:37:16 UTC (19:07 IST)**, post ID 2122323115535720921 (decoded from the ID). The "generally available" follow-up, post ID 2122323152172883984, came 9 seconds later. Some outlets give the launch date as Nov 18; use Sarvam's first post as T0.
- **Before T0:** Sarvam previewed the service as "Sarvam Inference" at its Epoch event. Surface this later.
- **Official sources:** blog sarvam.ai/blogs/sarvam-token-factory; docs docs.sarvam.ai/api/token-factory.

### Storylines seen so far (seed labels; let clustering confirm, split or merge them)
1. Official performance and price claims (self-reported).
2. "Mostly serving Chinese models": six of eight launch models are Chinese. Sanchit Monga (RunAnywhere) on X, per MediaNama.
3. "A hosting business, not a frontier lab" / "just another neocloud".
4. Subsidised government GPUs: were IndiaAI Mission GPUs used to serve foreign models?
5. Centralisation of GPU access: Srikanth Lakshmanan (CashlessConsumer).
6. Market-logic defences: Nikhil Pahwa (MediaNama); Hemant Mohapatra (Lightspeed, **a Sarvam investor**, so disclose the conflict).
7. Data sovereignty and residency (positive).
8. Comparisons with OpenRouter, Together, Fireworks, DeepInfra.
9. Context: the backlash came days after Sarvam's AI deal with the Ministry of Defence's HQ IDS.

### Inflection point: Sarvam's response (~T+24h)
- Pratyush Kumar (co-founder): Token Factory runs on GPUs privately procured by Sarvam; subsidised IndiaAI compute ended in March 2026.
- Vivek Raghavan (co-founder, per BusinessWorld): the infrastructure is separate from the model-training compute and was financed entirely by private investors.
- **Still unconfirmed:** the exact time, and whether this was an official notice (X post, blog, press note) or statements to journalists (BusinessLine, Bloomberg).
- **Nuances to model:** "privately procured" and "financed by private investors" are different claims, and both are self-reported. The rebuttal may answer a narrower charge than critics made (Monga's point was about priorities, not specifically Token Factory's GPUs). Earlier reports say a government body was set to take equity in Sarvam, so check the current status before calling the funding purely private.
- **Demo question:** what did each storyline do after the response? Did it drop, reframe (for example to "Chinese models"), or double down? Did news lead with the clarification or the backlash?

### Claims with conflicting reports (good material for the claim panel)
| Claim | What reports say | Check against |
|---|---|---|
| Sarvam 105B price | ₹10/₹25 vs ₹15/₹60 vs ₹14/₹55 introductory, per million tokens | Docs pricing table, archived on collection date |
| Models at launch | 8 | Blog and docs |
| "6 Chinese models" | MediaNama | Model list |
| Gemma 4 31B 5× faster first token, 1.6× output speed | Sarvam | Self-reported |
| 450B+ DeepSeek tokens/day | Sarvam via Unite.AI | Self-reported |
| Requests not forwarded overseas | Sarvam FAQ | Self-reported; can't verify externally |
| Subsidised GPU access ended Mar 2026 | Kumar via BusinessLine | IndiaAI Mission records |

### Known news coverage
Inc42, Analytics Insight, MarkTechPost (Nov 17), Unite.AI, MediaNama (Nov 19), BusinessWorld, Careerindia, Newsbytes, TechFlow, marketcalls, Hermes, AlphaSignal, The Hindu BusinessLine, Bloomberg, and tilnote (Korean, an example of cross-language spread).

---

## 3b. Event-agnostic design

The prototype is a general engine; Sarvam Token Factory is the first configured case, not a special case in the code.
- **A new event = a config file**, no code changes. The schema (T0, official accounts and posts, spokespeople, entities, keywords, sources and budgets, turning points, optional seed hints) and the Sarvam values are in `SEEDS.md`.
- **Generic by design:** storylines come from clustering (seed labels are optional hints), claims are extracted automatically, responses by official accounts or named spokespeople are detected and marked on the timeline, and every node and edge is tagged with an `event_id`.
- **Limits:** X recent search only reaches back about 7 days (older events need full-archive access or other sources); LinkedIn is manual for every event; clustering and stance are weaker for non-English content.
- **Demo idea:** after the Sarvam walkthrough, start a second event from a config in front of the jury. Rehearse on at least two other events first.

---

## 4. Data access (the main constraint)

| Source | Access | Notes |
|---|---|---|
| X | API v2 pay-per-use, about $0.005 per post read | **Recent search reaches back ~7 days: the T0 window closes around 22-23 Nov.** Not covered by GCP credits; cap reads. Since Feb 2026 API replies need a mention or quote, and AI reply bots need X's written approval (relevant only if we ever post). |
| Reddit | Reddit API | Fine for research; commercial use needs an agreement |
| Hacker News | Algolia API | Free, no key |
| YouTube | Data API v3 | Free quota of 10,000 units/day; a search costs 100 |
| News | GDELT DOC API / BigQuery public dataset; Google News RSS | GDELT is free for commercial use with citation and a link |
| LinkedIn | No API | Capture key founder and VC posts by hand, with screenshots |
| Meta, TikTok | Restricted | CrowdTangle shut down in 2024; research APIs are for researchers only. Skip. |

Save raw data immediately (posts get deleted) and keep it append-only.

---

## 5. Chosen architecture

```
Cloud Scheduler -> Cloud Run collectors (X, Reddit, HN, YouTube, news)
   -> Pub/Sub (raw items)
      -> Pub/Sub BigQuery subscription -> BigQuery (permanent raw record, analytics)
      -> Cloud Run enrichment worker: dedupe, Gemini embeddings, language, stance,
         storyline assignment (vector nearest-neighbour; new storyline if no match)
         -> Spanner Graph (live graph, GQL + SQL, built-in vector search)
   -> Cloud Run API (server-sent events) -> web front end
      (Cytoscape.js / Sigma.js / react-force-graph; Obsidian / Constellation look)
```

- Google's guidance: BigQuery Graph is for deep analysis at scale, Spanner Graph for real-time operations. The graph must update live, so Spanner Graph.
- Collection is polling every few minutes, not a true push stream (X's filtered-stream availability on our plan is unverified).
- Dataflow is not needed at this volume.
- GCP credits cover Google Cloud costs; still set a budget alert.

### Graph model
- **Nodes:** Event, Post, Account, Article, Outlet, Storyline, Claim (Media later). Every node and edge carries an `event_id`.
- **Edges:** REPLIED_TO, QUOTED, LINKS_TO, BELONGS_TO (storyline), AUTHORED_BY, SUPPORTS / CONTRADICTS (claim).
- **Every node and edge carries `created_at`.** The time slider filters `created_at <= t` to replay the story from T0.
- **Per storyline:** first-seen post, volume by hour, platform of origin, cross-platform jump times, top public amplifiers, stance, and status before vs after Sarvam's response.

### Normalised item schema
`id, platform, kind, url, author, author_followers, created_at (UTC), text, lang, parent_id, quote_of, engagement{likes,reposts,replies,views,score}, hours_from_t0`

---

## 6. Principles (apply to every output)
- Evidence, not verdicts. Every claim links to a source item. Claim status: supported, contradicted, misleading, or unverified.
- Label company statements as self-reported unless independently confirmed.
- Disclose conflicts of interest next to the quote.
- Never label an individual a bot. Show timing and duplication patterns as neutral signals only.
- Public data only, within each platform's terms. Show public figures and high-reach accounts; don't profile private individuals.
- Never help suppress accurate reporting.

---

## 7. Demo outputs
1. The live graph with a time slider, T0 to now.
2. Storylines coloured, with a stream view of volume over time.
3. The Sarvam response marked on the timeline, with each storyline's before/after shown.
4. Claim panel on graph nodes for the contested claims.
5. A one-page brief of the kind Sarvam's head of comms would have wanted on the morning of Nov 17.

## 8. Open decisions
- Exact time and form of Sarvam's clarification.
- X read budget.
- Front-end graph library.
- Whether to include Telegram or Indic-language outlets.
- How live the demo is: real live ingestion during judging, or a recorded replay with live ingestion shown separately.