# Validation and replay

Forward windows include 15m,1h,4h,24h,3d,7d,14d,30d, with legacy 5m preserved. Valid quotes can measure returns without requiring complete indicators. Save MFE/MAE and sampled time-to-extrema, sample count and maximum gap; incomplete coverage stays explicit. Fixed fee/slippage paper result is not execution simulation.

Archived replay uses only snapshots and membership/ranking batches available as_of; no current top100 or future metadata replaces old universe. Snapshots/signals retain source, feature, rule, signal and model versions. Ranking/universe batches retain weight dictionaries; hardcoded rule thresholds are identified by rule version. Replay explicitly uses the current rule version, not an executable archive of every past rule. No retrospective wallet/narrative/market-cap labels. Delisted/unselected history remains stored. Rank Top5/10/20 forward cohorts can only be scored after future prices exist; Precision uses a declared return threshold and covered cohort denominator. Insufficient samples produce UNKNOWN, not zero success or validation passed.

Phase 1 provides reproducible archived replay and forward measurements. Full historical reconstruction, robust false-positive attribution, lead-time labels, walk-forward calibration and ML belong to later phases and are not claimed complete by a green test suite.
