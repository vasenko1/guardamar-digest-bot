# September 2026 dedupe audit

Date: 2026-10-01

## Real-data result

September Telegram Desktop export contained 411 text/caption messages.
After deterministic prefilter, 266 remained eligible.
Initial dedupe version 2026-08-03.3 excluded 172 duplicates:
- 135 exact same-author duplicates;
- 37 semantic same-author duplicates.

Semantic arbitration produced 79 candidate pairs:
- 12 same decisions from Gemini with high confidence;
- 66 same decisions from deterministic rule `same_author_safe_campaign`;
- 1 different decision from a protected intent conflict.

## Failure found

`same_author_safe_campaign` treated a shared broad commercial topic as sufficient
offer identity. Real September data disproved that assumption. Examples included:
- SMM training merged with WordPress/SMM client services;
- apartment rental merged with sale of household appliances;
- HVAC installation/repair merged with air-duct cleaning;
- multiple language-school products/formats merged only because they were Spanish lessons.

The candidate-generation layer was useful: these pairs are worth examining. The
error was promoting broad topic equality directly to an automatic SAME decision.

## Decision

From dedupe version 2026-10-01.1:
- broad commercial-topic overlap remains a candidate-discovery signal;
- it is no longer automatic offer identity;
- protected factual conflicts still force DIFFERENT;
- cross-author Ukrainian Spanish-campaign fingerprint remains a narrow hard rule;
- exact same-author duplicates remain deterministic;
- semantic candidate pairs use high-confidence provider decisions when available;
- if providers fail, low-similarity same-topic pairs fall back conservatively to DIFFERENT;
- sufficiently high textual/containment overlap can still deduplicate without an LLM.

## Regression coverage

Tests cover:
- SMM training versus website/SMM service from the same author;
- apartment rental versus appliance sale from the same author;
- HVAC installation versus duct cleaning from the same author;
- repeated promotional copy with real overlap;
- the existing narrow cross-author Spanish campaign rule.

## Operational recovery

No raw September data must be re-imported. Re-running `dedupe --period 2026-09 --semantic`
with the new rule version reopens non-manual prior decisions and rebuilds duplicate
clusters from the preserved 411 raw entries.

## Multi-account Spanish school

The same September export also showed a separate requirement: one Spanish-language
school advertises through multiple Telegram accounts and rewrites the copy heavily.
A real-data fingerprint identified 36 school advertisements across eight accounts.
Rare linking markers include the 1 € lecture course, Wednesday/Friday 20:00 schedule,
356–459 UAH mini-group pricing, 50%/30% format discounts, A0–C1/C2 levels,
modern-learning/platform language, free trial and repeated course-format catalogues.

From dedupe version 2026-10-01.2:
- different course formats from the same evidenced school account collapse to one school campaign;
- cross-account school ads collapse only when they share strong rare campaign markers;
- generic Spanish wording alone is insufficient;
- individual tutors (for example ads explicitly describing the author as a `репетитор`) are excluded from the school fingerprint;
- unrelated Spanish schools remain separate unless the strong campaign fingerprint is satisfied.
