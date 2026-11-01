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
