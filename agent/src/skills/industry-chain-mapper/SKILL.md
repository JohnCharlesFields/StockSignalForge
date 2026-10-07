---
name: industry-chain-mapper
description: Map a market event, earnings surprise, policy theme, or technology catalyst into an investable equity industry chain with upstream/midstream/downstream links, beneficiary strength, and false-positive exclusions.
---

# Industry Chain Mapper

Use this skill when the task starts from a hotspot, event, earnings report, policy, or technology theme and needs to identify investable equity targets.

## Core Workflow

1. Define the event precisely:
   - event type: earnings, policy, product cycle, capex cycle, supply shock, demand shock, regulation, macro liquidity
   - affected geography and time horizon
   - direct economic variable: revenue, margin, orders, utilization, pricing power, funding cost, risk premium

2. Split the chain:
   - upstream: raw materials, key equipment, components, infrastructure
   - midstream: platforms, integrators, manufacturers, distribution channels
   - downstream: end demand, applications, customers, monetization layer
   - adjacent beneficiaries: software, services, financing, logistics, suppliers

3. Rank beneficiary links:
   - directness: whether the event changes the company's own revenue/margin
   - timing: immediate, 1-3 months, 3-12 months
   - operating leverage: whether incremental demand converts to earnings
   - market recognition: whether the market has already priced the link
   - liquidity and tradability

4. Exclude false beneficiaries:
   - small revenue exposure to the theme
   - wrong customer segment
   - low pricing power
   - adverse second-order effect
   - already fully priced after a large move
   - leader-to-laggard mismatch where a small niche company should not be used to infer a mega-cap move

## Required Output

Return a structured report with:

- Event definition
- Industry chain map
- Beneficiary links ranked high / medium / low
- Candidate company list with ticker, role, exposure rationale, and exclusion risks
- Narrow peer group matching rules
- Final watchlist for quantitative screening

## Scoring Template

Use a 0-100 score:

- 30 direct exposure
- 20 earnings transmission
- 15 timing fit
- 15 market underreaction
- 10 liquidity / execution
- 10 risk penalty adjustment

Companies below 55 should not enter the primary candidate list.
