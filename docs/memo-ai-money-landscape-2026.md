# Memo: The AI money landscape for a technical builder — and what it means for Royal

**For:** the team of instances working with the human via ZetaDB (Code / Desktop / Cowork)
**From:** a Claude Code web session (research role — proposing the persona **Cartographer** for this thread; adopt or discard)
**Date:** 2026-06-07
**Suggested origin tag:** `royal-strategy`
**Status:** landscape research, verified by cross-source convergence. Royal-specific tuning still pending the context export (the human is embedded in grocery retail, implementing AI org-wide — see §7).

---

## Why this exists

The human asked, in effect: *is there real money in building with AI as a technical person, or is it saturated?* I ran a five-angle deep-research pass (solo-builder revenue, MCP/agent-tooling, AI services, saturation/moats, distribution). This memo is the durable distillation so no future instance re-runs it from scratch. **Verify before acting on any specific figure** — I've flagged confidence throughout, and the discipline doc is right that memory is a hint, not ground truth.

---

## 1. Bottom line

It is **saturated where it's easy and open where it's hard.** The thin "AI wrapper" layer is a red ocean; the bottleneck has moved from *building* (now commoditized) to *domain access + distribution + trust*. For our human this is unusually good news, because — unlike the indie builders in the research who are scrambling to acquire domain depth — **he already has it** (Royal). The scarce resource, he has. The commodity resource (building), he finds easy. That asymmetry is the whole opportunity.

## 2. The core finding: the bottleneck moved

When everyone can build the thing overnight, building is worth ~nothing. The credible anchor: [McKinsey 2025](https://www.mckinsey.com/capabilities/quantumblack/our-insights/from-ai-table-stakes-to-ai-advantage-building-competitive-moats) — **79% of orgs say competitors make similar AI investments; only 23% believe they're building durable advantage.** [a16z](https://a16z.com/vsaas-vertical-saas-ai-opens-new-markets/) frames the same shift: scarcity moved from model → proprietary data, workflow integration, distribution. **(High confidence — primary VC/consulting sources.)**

## 3. Saturation scorecard

| Layer | Saturation | Realistic solo outcome |
|---|---|---|
| Thin AI wrappers / horizontal tools | 🔴 red ocean | most $0; 60–85% die in 1–2 yrs |
| MCP servers *as a product* | 🟠 no market | market price ≈ **$0**; ~95% of authors earn nothing |
| Vertical "boring-niche" micro-SaaS | 🟢 still open | $1–5k MRR realistic; $20k+ rare; 12–18 mo |
| AI implementation/automation services for SMBs | 🟡 real demand, hype-fogged | 1–2 retainers @ $1–3k/mo + build fees |

Notes that matter:
- **Thin-wrapper graveyard is documented**, not vibes: [SimpleClosure's *State of Shutdowns 2025*](https://techstartups.com/2025/12/09/top-ai-startups-that-shut-down-in-2025-what-founders-can-learn/) names the dominant failure pattern as commoditized application-layer tools "without deep defensive moats." Named deaths (Tune AI, CodeParrot) died when model providers shipped the feature natively. AI apps also [churn ~30% faster](https://a16z.com/ai-retention-benchmarks/) than traditional apps. **(med-high)**
- **MCP servers:** 11,000+ exist, [Smithery alone hosts 7,000+, <5% monetized](https://mcpize.com/developers/monetize-mcp-servers); one honest piece is titled ["Why we charge $19/mo when the market average is $0."](https://dev.to/whoffagents/pricing-an-mcp-server-in-2026-why-we-charge-19mo-when-the-market-average-is-0-nig) Anthropic's [official registry (Sept 2025)](https://blog.modelcontextprotocol.io/posts/2025-09-08-mcp-registry-preview/) is discovery-only, no payment layer; MCP was [donated to a neutral foundation](https://www.anthropic.com/news/donating-the-model-context-protocol-and-establishing-of-the-agentic-ai-foundation) = commodity standard. **An MCP server is a thin edge on a real product, never the product.** (Relevant to us — ZetaDB itself is the cautionary example done *right*: it's infrastructure for the human, not a thing we're trying to sell.)
- **Services demand is genuinely real:** [Upwork's own 2026 report](https://investors.upwork.com/news-releases/news-release-details/upworks-demand-skills-2026-demand-top-ai-skills-more-doubles-ai) — AI-skill demand **+109% YoY**; [AI-skilled freelancers earn ~40%+ more/hr](https://www.axios.com/2025/06/30/ai-job-vibe-coding-upwork). **(high — company data.)** But the [Brookings/AEA peer-reviewed study](https://www.brookings.edu/articles/is-generative-ai-a-job-killer-evidence-from-the-freelance-market/) warns commodity AI-adjacent freelancing is being *compressed*, hitting experienced high-price sellers hardest. Money is in *selling the AI capability to businesses*, not competing as a cheaper producer.

## 4. Where durable advantage actually lives

Every credible source ([a16z](https://a16z.com/vsaas-vertical-saas-ai-opens-new-markets/), [YC's "7 moats"](https://www.ycombinator.com/library/Mx-the-7-most-powerful-moats-for-ai-startup), [Insight](https://www.insightpartners.com/ideas/building-a-moat-in-the-age-of-ai/), [Insignia](https://review.insignia.vc/2025/03/10/ai-moat/)) converges on:
1. **Domain expertise** — a16z notes the reversal: it used to be domain-experts hiring engineers; now it's engineers needing to *acquire* domain depth, and most won't. **This is the gap.**
2. **Proprietary data / workflow lock-in** — strongest in messy, regulated, unstandardized verticals.
3. **Distribution & trust** — owning a channel customers already inhabit.
4. **Speed** — the solo/small-team edge vs. incumbents.

The "thick wrappers" that survived model-provider competition (Cursor, Perplexity, Harvey) all had a data/workflow flywheel + distribution position a raw model couldn't replicate.

## 5. Honest money bands (don't inflate these to the human)

- **Services:** first small engagement in weeks–2 months; at part-time hours, **1–2 retainers @ $1–3k/mo + occasional build fees** is the grounded ceiling. The "$10k/mo in 6 months" framing is guru fiction.
- **Vertical micro-SaaS:** believable band **$1–5k MRR over 6–18 months**, high early-abandonment rate. $20k+ is outlier territory (Pieter Levels at $130k+ MRR had ~70 prior failures — do not anchor on him).

## 6. Distribution truths + traps

- **Pick ONE channel, run it 90 days** before adding a second. ([Every scaled indie case](https://prems.ai/blog/indie-hacker-marketing-playbook-2026) started with one dominant channel.)
- **For an off-Twitter vertical, the channel is direct/cold outreach + integrating into a tool the customer already uses — NOT build-in-public.** BIP worked for [Base44](https://www.lennysnewsletter.com/p/the-base44-bootstrapped-startup-success-story-maor-shlomo) and [SuperX](https://www.indiehackers.com/post/tech/hitting-23k-mrr-in-six-months-after-five-failures-4d64o9ev4AXXhX9ogHQQ) only because they sold *to the BIP audience*; for grocery retail it [traps you building for other indie hackers](https://www.indiehackers.com/post/why-indie-founders-fail-the-uncomfortable-truths-beyond-build-in-public-b51fd6509b).
- **Pre-sell before building. Win the first ~10 customers by hand. Marketplaces (Chrome/Shopify/GPT Store) are for discovery, not revenue.**
- **Traps:** thin wrappers; selling "an MCP server"; the "AI automation agency" course-hype (identical recycled numbers across content-farm blogs — the fingerprint); building before pre-selling; spreading across channels.

## 7. What this means for Royal (the part that's ours to develop)

The generic research says *"find a boring, budgeted vertical and acquire domain depth."* **The human is already inside one.** Grocery retail is precisely the high-volume, thin-margin, workflow-heavy, "still runs on spreadsheets and copy-pasted emails" vertical the a16z/YC sources flag as underserved. He's implementing AI at almost every level — which means he's generating exactly the proprietary-workflow knowledge and data-flywheel that §4 calls the durable moat.

**Open questions for whoever has the Royal context** (resolve these before strategizing concretely):
- His relationship to Royal (operator / consultant / owner-family) — determines whether the play is *productize & spin out*, *capture more value in-place*, or has *IP/conflict* constraints.
- Which workflows the AI is going into (ordering/inventory, scheduling, pricing/markdowns, supplier comms, customer service, back-office) — the repeatable pattern across those is the candidate product/service line.
- Whether he wants an *owned asset* (product/equity) or *value capture* on work he's already doing.

**The synthesis play** (best-supported, bridges his "need income" + "patient build"): the implementation work he's *already doing* at Royal is simultaneously (a) the cash, (b) the domain moat, and (c) the proof-of-concept for a productized vertical tool he could later sell to *other* grocery/retail operators. That's the "services → services-as-software" ladder [a16z](https://a16z.com/vsaas-vertical-saas-ai-opens-new-markets/) describes, except he's already on the first rung. The grocery-retail vertical (multi-store independents, regional chains) is a plausible TAM once one workflow is proven inside Royal.

## 8. Sourcing & confidence

- **Solid (rely on):** McKinsey 23%/79%, Upwork +109%, Brookings/AEA compression finding, a16z moat/vertical thesis, YC moats, MCP-market-is-$0, shutdown-pattern reporting.
- **Flagged (directional only):** all "median MRR / 95% profitable / $X-in-90-days" stats — they trace to SEO/content-farm/affiliate blogs recycling identical unsourced numbers. Pieter Levels' revenue is real (he publishes it) but he's an extreme outlier. Several primary pages (a16z, McKinsey) 403'd on direct fetch, so a few figures come from indexed summaries that quote them — re-verify before quoting externally.

---

## Suggested ZetaDB filing (write-back block — for a Desktop instance to execute)

```
ADD memory (cat:work, imp:4) (by_human) nickname:AIMONEY origin:royal-strategy
  summary: Research finding — AI build tools are commoditized; durable advantage moved to domain access + proprietary workflow data + distribution + trust, not building. Saturation is real for thin wrappers/MCP-servers-as-product; vertical "boring-niche" tools and SMB AI-implementation services still open. Realistic solo bands: services 1-2 retainers @ $1-3k/mo; vertical micro-SaaS $1-5k MRR over 6-18mo. Guru "$X in 90 days" content is recycled fiction.
  body: See full memo (committed to zeta-db repo as docs artifact / branch claude/runtime-environment-VFhMt). Royal angle: human is already embedded in the exact kind of underserved vertical the research says wins; the implementation work doubles as cash + domain moat + PoC for a productizable grocery-retail tool. Open questions: his relationship to Royal, which workflows, owned-asset vs value-capture.
  tags: ai-strategy, monetization, research, vertical-saas, moats, royal, grocery-retail
```

(Adjust importance/tags to taste; clear the nickname if `AIMONEY` collides with anything.)

— Cartographer
