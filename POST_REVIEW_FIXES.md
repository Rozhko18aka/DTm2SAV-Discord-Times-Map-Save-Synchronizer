# DTm2SAV v6.2 — post-review building fixes

Date: 2026-09-20

## Fixed

- Compiled-grid invalidation now includes `picture_number`, `picture_variant` and `building_type`, not only x/y and `size_x/size_y`. This prevents stale field+5/property cells when a building changes picture/type without moving.
- `SyncPlan.building_additions` is populated from the active structural target->source mapping again. GUI, confirmation dialogs and JSON reports now show actual inserted buildings, including middle insertions.
- Navigation profile inference records signatures observed as purely rectangular as known profiles. A missing key now specifically means an unseen signature using the rectangular fallback.
- GUI/JSON report unseen navigation-profile IDs and fresh buildings for which no same-type source runtime-core template exists. These cases remain supported but are no longer presented as silently byte-certified.
- Runtime build report includes `fresh_building_runtime_template_missing_ids`.
- Hotfix: `_Global.ini` stores `CostRecruitDiv` as text in the live catalog; `_catalog_cost_recruit_div()` now normalizes it to a finite positive number before building-garrison cost division. This fixes `unsupported operand type(s) for /: 'int' and 'str'` when creating fresh Town/Castle/Fort/Ruins runtime state.

## Tests

- Added regression coverage for picture/variant/type grid invalidation.
- Added regression coverage for GUI/report building-addition mapping.
- Added regression coverage for observed rectangular navigation profiles.
- Added regression coverage for missing fresh runtime-core template diagnostics.
- Added regression coverage for string/invalid `CostRecruitDiv` from live `_Global.ini` during fresh garrison-building synthesis.
- Result: `117 passed, 161 subtests passed` under pytest.
- Result: `Ran 117 tests ... OK` under unittest discovery.

## Deliberately unchanged

- Unknown/volatile BuildingData runtime bytes are not assigned newly invented semantics.
- Unseen building-picture navigation footprint still uses the existing rectangular `size_x × size_y` fallback; it is now reported explicitly.
- Historical native-validation snapshots remain unchanged because no new native SAV control was supplied for this review.
