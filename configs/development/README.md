# TradeXYZ development market API

`tradexyz-markets.json` is the response fixture for `markets.mode: "mock_api"`.
It contains 120 unique instruments: all 118 listed as active in the [TradeXYZ specification
index](https://docs.trade.xyz/perpetuals/specifications-and-schedules/specification-index)
plus the two **Live** entries in the [pre-IPO specification
index](https://docs.trade.xyz/perpetuals/specifications-and-schedules/pre-ipo-specification-index):
ANT (Anthropic) and OURA (Oura), retrieved on 2026-11-01. Both use `pre_ipo` as
their asset type. All eight **Converted** entries already exist in the standard
index; they remain single entries with their standard asset types.
`tradexyz-provenance.json` records both source hashes, date and reconciliation IDs.
This is the user-requested development universe, **not** the production Megastocks
allowlist and not a promise that the upstream list remains unchanged.

The mock uses an in-process HTTP transport with the production response parser,
ETag and schema checks. It opens no server port and requires no network. Every run
pins its own fixture copy, so later edits cannot change a running selection. Use
`configs/clip-only.mock-tradexyz.example.json` as a config template; replace the
Foundry endpoint/deployment and select appropriate identity access before running.

Stable IDs use `xyz:<instrument>`. Names and asset classes are normalized from the
specification descriptions; conservative aliases are explicit. Different listed
instruments on the same company remain distinct IDs. Shared/ambiguous aliases must
be disambiguated from source context; they are not invalid just because two markets
share an issuer. No index constituents or corporate relationships are inferred.
The fixture deliberately contains no trading limits, prices or leverage settings.

To refresh: retrieve both indexes, reconcile **every** instrument row (including
removals and lifecycle transitions), add only **Live** pre-IPO entries, and verify
**Converted** entries against the standard index without duplicating or reverting
them to `pre_ipo`. Use the explicit status, not an anticipated listing date, to
determine pre-IPO inclusion. Review normalized names/classes/aliases, update the snapshot version and
provenance, and update the coverage test. Do not silently refresh an in-flight run.
Switch to `markets.mode: "api"` with the real backend URL/auth reference when ready.

## Reviewed AI profiles (Phase D.1)

`ai-market-profiles.json` is a deliberately small, independently versioned knowledge
snapshot. It covers six entities (Anthropic, Amazon, Alphabet, NVIDIA, Microsoft and
Broadcom), overlapping model/cloud/accelerator roles, and two direct customer links.
The manufacturing category has no curated profiles yet. Uncovered markets stay eligible
for direct-name or macro matching; missing profiles are reported rather than invented.
This snapshot does not authorize any instrument. Final IDs always intersect the market
API response, including its enabled flags.

The factual summaries and relationship scopes were checked against these issuer pages
on 2026-11-01:

- [Anthropic's AWS compute agreement](https://www.anthropic.com/news/anthropic-amazon-compute)
- [Google/Broadcom compute agreement](https://www.anthropic.com/news/google-broadcom-partnership-compute)
- [Microsoft/NVIDIA partnership announcement](https://www.anthropic.com/news/microsoft-nvidia-anthropic-announce-strategic-partnerships)

These records describe announced agreements and business roles, not verified delivery,
realized revenue, investment returns or guaranteed present exposure. No prices, financial
weights or scale estimates are inferred. Each record states its applicability interval,
verification date and sources. Future delivery plans must not be presented as realized
capacity. Inference requires a source-grounded mechanism and qualitative materiality
assessment; a shared category or relationship alone cannot establish eligibility.

Refresh procedure: review the cited source and newer issuer disclosures; reconcile
changed identities, aliases, relationships and applicability dates; mark conflicting
claims `conflicted`; update the verification date only after checking the factual claim;
bump the snapshot revision. Keep a bounded applicability end date. Stale/conflicted or
out-of-period records cannot support inferred associations. Do not refresh an in-flight
run or infer a missing event date from the publication date. Initial freshness settings
are 365 days for memberships and 90 days for relationships; applicability is checked
separately. Scale is nullable, and the initial profiles deliberately leave it unknown.

`configs/enrichment.example.json` is a separate **synthetic** contract example for
fixtures. It is not a real issuer disclosure or production knowledge. The opt-in live
synthetic evaluator uses it without downloading or publishing any media.
