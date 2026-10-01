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
- very low-overlap broad-topic pairs resolve directly to DIFFERENT without an LLM call;
- remaining ambiguous pairs may use providers; if providers fail, deterministic arbitration remains conservative;
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

From dedupe version 2026-10-01.3:
- different course formats from the same evidenced school account collapse to one school campaign;
- cross-account school ads collapse only when they share strong rare campaign markers or the preserved proven historical campaign fingerprint;
- generic combinations such as individual + mini-group + group are not enough across accounts;
- generic Spanish wording alone is insufficient;
- individual tutors (for example ads explicitly describing the author as a `репетитор`) are excluded from the school fingerprint;
- unrelated Spanish schools remain separate unless the strong campaign fingerprint is satisfied.


## Final editorial QA correction pass

After the first technically valid September preview, human editorial review found
content errors that schema/HTML validation could not detect. The correction pass is
grounded in those real messages rather than broader heuristics.

Observed failures:
- a post sent as the source channel itself (Jardín Musical) entered the digest;
- a Torrevieja rental studio was classified as HVAC because its amenities mention
  an air conditioner;
- Guardamar addresses containing "Alicante" as the province were displayed as if
  the listing were in Alicante city;
- a naturopath post became "Антибактериальная обработка салона";
- a designer seeking first client orders was rendered as somebody seeking a designer;
- a parcel request was rendered as a transport offer;
- kittens seeking a family were placed under "Куплю";
- flowers were mixed into the food category;
- image-dependent texts with no named product produced meaningless titles;
- repeated monthly campaigns remained for the same fitness trainer, naturopath,
  body-shop vacancy, caregiver and confectioner.

Rules introduced by the correction pass:
- prefilter version 2026-10-01.1 excludes the source-channel sender identity derived
  from the Telegram source chat id and high-confidence image-dependent text stubs;
- dedupe version 2026-10-01.4 adds only narrow same-author monthly campaign
  fingerprints for the evidenced business/service classes and a product+price rule
  for the repeated laptop case;
- classifier version 2026-10-01.2 separates flowers from food, strengthens rental
  recognition across descriptive punctuation and treats Alicante province context
  separately from Alicante city;
- editorial version 2026-10-01.1 adds intent rules for client-order seeking,
  transport requests and pet adoption, plus stable titles for naturopath, fitness,
  caregiving, translator, flowers and car rental entries;
- transport requests render under their own subsection "Ищу перевозку".

Explicit non-goal:
- message 7480 (stolen backpack/documents) remains outside the current commercial/
  service digest because adding a "Потери и находки" product section is a separate
  scope decision, not a correction of the existing pipeline.
