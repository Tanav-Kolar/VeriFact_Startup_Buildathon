# SEEDS: event configuration (event-agnostic)

The engine is event-agnostic. **A new event is set up by writing a config file, with no code changes.** This file defines the config format (Part 1), the first configured case, Sarvam Token Factory (Part 2), and a checklist for adding the next event (Part 3).

Seed content is a starting hint, not ground truth. Storylines are discovered by clustering, and seed labels only help name and sanity-check them. Facts about Sarvam below were researched on 20 Nov 2026 and need re-verifying against the collected data.

---

## Part 1: Event config schema

One file per event, for example `events/<event_id>.yaml`. Every collector, the enrichment worker and the front end read from it. Nothing about a specific event is hardcoded.

```yaml
event_id: sarvam-token-factory          # slug; every node and edge in the graph is tagged with it
name: "Sarvam Token Factory launch"

t0:
  utc: "2026-11-16T13:37:16Z"           # the first official post
  source_url: "https://x.com/SarvamAI/status/2122323115535720921"
  note: "Decoded from the X post ID"
window:
  pre_hours: 72                         # context before T0; teasers surfaced later
  post_hours: 240                       # how far past T0 to keep collecting
  live: true                            # keep ingesting new items until switched off

official:                               # the subject's own channels
  accounts: ["SarvamAI"]                # platform handles
  posts: ["2122323115535720921", "2122323152172883984"]
  urls: ["https://www.sarvam.ai/blogs/sarvam-token-factory",
         "https://docs.sarvam.ai/api/token-factory"]
  spokespeople: ["Pratyush Kumar", "Vivek Raghavan"]   # names; used to detect responses

entities:                               # things to recognise, with aliases
  org:      {Sarvam: ["Sarvam AI", "@SarvamAI"]}
  product:  {"Token Factory": ["Sarvam Token Factory", "Sarvam Inference"]}
  related:  [DeepSeek, Kimi, GLM, Gemma, "IndiaAI Mission", OpenRouter, "Together AI", Fireworks, DeepInfra]

keywords:
  core:     ["sarvam token factory", "\"token factory\" sarvam"]        # almost certainly on-topic
  context:  [inference, deepseek, chinese, neocloud, sovereign, subsidy, GPU, hosting]   # always combine with the org name
  exclude:  []                          # terms that signal off-topic matches

languages: [en, hi]                     # languages to expect; quality is lower outside English

sources:                                # one block per source; turn off what you don't use
  x:        {enabled: true,  max_posts: 1500, thread_ids: ["2122323115535720921"]}   # $10 hard limit set in the X developer console
  reddit:   {enabled: false, subreddits: [developersIndia, india, IndiaTech, indianstartups, LocalLLaMA]}
  hn:       {enabled: true}
  youtube:  {enabled: true,  max_videos: 25}
  news:     {enabled: true,  gdelt: true, google_news_rss: true}
  manual:   {enabled: false, platforms: [linkedin]}     # no API, captured by hand into a CSV

turning_points:                         # moments to mark on the timeline
  detect_official_response: true        # auto: an official account or spokesperson posts or is quoted
  manual: []                            # add known ones by hand once confirmed

seed_storylines: []                     # OPTIONAL hints only; clustering decides the final set
claims_to_check: []                     # OPTIONAL list of contested claims for the claim panel
conflicts_of_interest: []               # OPTIONAL: [{person, relationship}] to disclose next to quotes
known_coverage: []                      # OPTIONAL: URLs already found, to backfill and seed news nodes
```

**What each part drives**
| Config part | Used by |
|---|---|
| `t0`, `window` | Collectors (time bounds), `hours_from_t0`, the time slider's start |
| `official` | Marks the official nodes; anchors the graph at T0; detects responses |
| `entities` | Entity linking, claim extraction and graph labels |
| `keywords` | Query building for each source |
| `sources` | Which collectors run and their budgets |
| `turning_points` | Timeline markers and before/after storyline comparison |
| `seed_storylines`, `claims_to_check`, `conflicts_of_interest` | Naming and sanity checks, the claim panel, conflict disclosures |

**Limits to remember for any event**
- X recent search reaches back about 7 days; older events need full-archive access (extra cost) or must lean on Reddit, HN, YouTube and GDELT.
- LinkedIn is always manual.
- Clustering and stance are weaker for non-English content; test before claiming otherwise.

---

## Part 2: First configured case (values for the config above)

### Facts
- **T0:** @SarvamAI launch post, 2026-11-16 13:37:16 UTC (19:07 IST), ID 2122323115535720921. The "generally available" follow-up, ID 2122323152172883984, came 9 seconds later. Some outlets give Nov 18 as the launch date; use Sarvam's first post.
- **Before T0:** Sarvam previewed the service as "Sarvam Inference" at its Epoch event (date to find).
- **Official:** blog sarvam.ai/blogs/sarvam-token-factory; docs docs.sarvam.ai/api/token-factory (OpenAI-compatible API at api.sarvam.ai/v2).

### Seed storylines (hints for the `seed_storylines` field)
1. Official performance and price claims (self-reported)
2. "Mostly serving Chinese models" (six of eight launch models are Chinese)
3. "A hosting business, not a frontier lab" / "just another neocloud"
4. Subsidised government GPUs
5. Centralisation of GPU access
6. Market-logic defences
7. Data sovereignty and residency (positive)
8. Comparisons with OpenRouter, Together, Fireworks, DeepInfra
9. Context: the backlash followed Sarvam's AI deal with the Ministry of Defence's HQ IDS

### Public figures to watch (names only; look up handles from the data, never guess them)
- Critics: Sanchit Monga (RunAnywhere), Srikanth Lakshmanan (CashlessConsumer)
- Defenders: Nikhil Pahwa (MediaNama), Hemant Mohapatra (Lightspeed India)
- Company: Pratyush Kumar, Vivek Raghavan

### Conflicts of interest (for `conflicts_of_interest`)
- Hemant Mohapatra: partner at Lightspeed India, which invests in Sarvam (per MediaNama).

### Turning point (for `turning_points.manual`, once confirmed)
- **About T+24h: Sarvam's clarification that Token Factory runs on privately procured, privately funded GPUs, not subsidised government GPUs.**
  - Pratyush Kumar (BusinessLine, Bloomberg, per press): GPUs privately procured; subsidised IndiaAI compute ended in March 2026.
  - Vivek Raghavan (per BusinessWorld): infrastructure separate from model-training compute, financed entirely by private investors.
  - **Unconfirmed:** exact time, and whether this was an official notice or statements to journalists.
  - "Privately procured" and "privately financed" are different claims, and both are self-reported. Earlier reports say a government body was to take equity in Sarvam; check the current status.

### Claims to check (for `claims_to_check`)
| Claim | Reports | Check against |
|---|---|---|
| Sarvam 105B price | ₹10/₹25 vs ₹15/₹60 vs ₹14/₹55 (introductory) per million tokens | Docs pricing table, archived on collection date |
| Models at launch | 8 | Blog and docs |
| "6 Chinese models" | MediaNama | Model list |
| Gemma 4 31B: 5× faster first token, 1.6× output speed | Sarvam | Self-reported |
| 450B+ DeepSeek tokens/day | Sarvam via Unite.AI | Self-reported |
| Requests not forwarded overseas | Sarvam FAQ | Self-reported; unverifiable externally |
| Subsidised GPU access ended Mar 2026 | Kumar via BusinessLine | IndiaAI Mission records |
| Runs on privately procured / financed GPUs | Kumar, Raghavan | Self-reported; unverifiable externally |

### Known coverage (for `known_coverage`)
Inc42, Analytics Insight, MarkTechPost (Nov 17), Unite.AI, MediaNama (Nov 19), BusinessWorld, Careerindia, Newsbytes, TechFlow, marketcalls, Hermes, AlphaSignal, The Hindu BusinessLine, Bloomberg, tilnote (Korean).

---

## Part 3: Adding a new event (checklist)
1. Copy the config, set `event_id`, `name` and `t0` (the first official post; decode X post IDs to get an exact time).
2. Fill in `official`, `entities` and `keywords`; add `exclude` terms if early results are noisy.
3. Choose sources and budgets; check how many days of X history remain.
4. Leave `seed_storylines`, `claims_to_check`, `conflicts_of_interest` and `known_coverage` empty at first. Fill them in after the first look at the data.
5. Run a small collection, review the first clusters, then widen.
6. Rehearse the end-to-end flow on at least two events before showing a new one live.