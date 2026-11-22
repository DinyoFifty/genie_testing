# Data dictionary

CSV uses UTF-8, a header row, and empty cells for missing values. Never enter zero to stand in for missing data. Units in normalized column names describe this tool's contract, not unverified Analyzer source units. Enter literal `none` only in descriptive fields when absence is confirmed. Numeric missing values must remain empty.

## Run metadata

| Group | Columns and interpretation |
| --- | --- |
| Identity and design | `run_id` unique per capture/trial; `trial_number`; `test_date` calendar date at site; `test_location`; `condition_id`; `changed_variable`; `baseline_condition_id`; `run_purpose` evaluation/calibration/validation; `calibration_id` when applicable |
| Hardware | `initiator_id`, `reflector_id` (serial/address plus identity convention); `initiator_board_model`, `reflector_board_model`; each `_board_revision` |
| Software | Each board's `_firmware_name` and `_firmware_version`; `sdk_version`; `analyzer_version`. Record unknown versions as missing, not the workspace version guessed as flashed firmware |
| Tape truth | `actual_distance_m`; `initiator_reference_point`, `reflector_reference_point`; actual measured separation between those two points |
| Geometry | Each board's `_height_m`, `_roll_deg`, `_pitch_deg`, `_yaw_deg`; `orientation_reference_frame` defines axes, zero, signs, height reference |
| Conditions | `environment`; `visibility` (LOS/obstructed with description); `obstacle_material`, `obstacle_thickness_m` if known, `obstacle_placement` relative to the boards |
| Supply | `initiator_power_source`, `reflector_power_source` (describe USB, battery, supply and measurement boundary) |
| TX settings | `planned_initiator_tx_power_dbm`, `planned_reflector_tx_power_dbm` are proposals only; `configured_initiator_tx_power_dbm`, `configured_reflector_tx_power_dbm` are verified configured values; `reported_initiator_tx_power_dbm`, `reported_reflector_tx_power_dbm` are reported actual values when available |
| Setting evidence | `settings_verification_evidence`; `unsupported_settings` with requested/rejected/actual outcome; never assume a board applied a requested power or mode |
| Ranging | `ranging_mode`, `algorithm_mode`, `channel_map` (original representation), `channel_count`, `antenna_configuration`; `connection_interval_ms`, `procedure_interval_ms` with verified conversion |
| Processing | `filtering_settings`, `calibration_settings` (include algorithm/parameters/where applied, or confirmed `none`) |
| Recording | `planned_duration_s`; `recording_start_utc`, `recording_end_utc` ISO 8601 with timezone; `actual_duration_s`; `duration_evidence`. Actual duration can be derived from both recording boundaries |
| Accuracy | `accuracy_tolerance_m` team-agreed absolute-error tolerance; blank prevents a tolerance percentage |
| Attempt observability | `attempt_accounting` empty, `attempt_events_complete`, or `terminal_events_complete`; `attempt_accounting_evidence` describes why every attempt is observable |
| Connection observability | `connection_events_complete` literal `true` only with evidence; `connection_events_evidence`; `initial_connection_state` connected/disconnected |
| Startup timing | `power_on_timestamp_utc`, `power_on_evidence` for synchronized external timestamp; `startup_capture_complete=true` and `startup_capture_evidence` document complete capture from before power-on |
| Notes | `observations`, including movement, disconnects, configuration changes and unknown metadata |

`test_plan.csv` holds planned separation/yaw/obstacle separately from these observed fields. Use its run ID to link a planned condition to the actual run record. Planned and configured settings are not interchangeable.

## Measurement/event CSV

One normalized row per source event, including nonmeasurement events. A reviewed adapter may be needed for exports containing arrays/multiple estimates per event; this implementation will not expand them by guesswork.

| Column | Meaning |
| --- | --- |
| `run_id` | Foreign key into run metadata |
| `timestamp_utc` | Verified wall-clock event timestamp, normalized to UTC |
| `elapsed_s` | Seconds since the **recording** start, not since first estimate; missing when unmeasurable |
| `clock_id` | Verified clock/session identity; required to correlate timing |
| `source_timestamp_raw` | Original timestamp field value when UTC conversion is mapped; other raw values remain in the original log |
| `event_kind` | measurement/attempt/request/connection_open/connection_closed/power_on/command/diagnostic/display/unmapped |
| `measurement_id` | Source's estimate identifier when exposed |
| `procedure_id` | Source procedure identifier; not automatically a unique attempt identifier |
| `attempt_id` | Documented unique attempt correlation ID within peer + connection/session |
| `request_id` | Unique request-to-terminal-result correlation ID within peer + connection/session |
| `reflector_id` | Identity of the evaluated reflector |
| `connection_id` | Identity distinguishing connections, reconnections and counter resets |
| `cs_distance_m` | Channel Sounding distance converted from a documented source unit to meters |
| `rtt_distance_m` | Separate RTT-based distance in meters, only when exposed |
| `rssi_distance_m` | Separate RSSI-derived distance in meters, only when exposed |
| `rssi_dbm` | Actual measured RSSI in dBm, only when exposed; never inferred from RSSI distance |
| `quality_score` | Original numeric likeliness/quality; no normalization to percentage |
| `quality_scale` | Documented scale/range/meaning, including whether higher/lower is better if known |
| `status` | valid/invalid/failed as supported by documented source semantics; missing remains missing |
| `error_code` | Original error identifier, including string/hex representation when exposed |
| `source_sha256`, `source_file`, `source_line` | Preserved original bytes' hash/path and 1-based line number |
| `mapping_sha256` | Exact mapping version used for import |

Analyzed measurements add `included_in_accuracy`, `exclusion_reason`, `signed_error_m`, `absolute_error_m`, and `request_to_result_latency_s`. All original normalized event rows remain. `exclusions.csv` requires `(run_id, source_sha256, source_line, reason)`; no arbitrary numeric thresholds are hidden in the tool.

## Run summary

`run_summary.csv` preserves run/trial/condition/purpose and `data_status`, actual distance, tolerance, actual duration, included valid distance count and observed valid/failed/invalid result counts before accuracy exclusions. It reports attempted measurements, failed-or-invalid results, unresolved attempts, and successful-measurement percentage only with the documented accounting contract.

Accuracy fields are `mean_estimated_distance_m`, `mean_signed_error_m`, `mean_absolute_error_m`, `rmse_m`, `sample_stddev_distance_m`, `p95_absolute_error_m`, and `valid_estimates_within_tolerance_pct`.

Timing fields distinguish `inter_estimate_median_s`, `inter_estimate_p95_s`, `inter_estimate_max_s`, `valid_estimate_rate_hz`, `power_on_to_first_valid_s`, `request_to_result_median_s`, `request_to_result_p95_s`, and `paired_request_result_count`. Connection fields include `disconnect_count`, `disconnected_duration_s` and `disconnect_intervals_json`. `limitations` explains unavailable calculations; `accuracy_excluded_terminal_count` includes invalid/failed/missing and explicit accuracy exclusions, not just manually excluded valid rows.

## Instrument power data

`power_samples.csv`: `power_run_id`, `time_s`, `voltage_v`, `current_a`, `state`. State may be active/idle/sleep or missing; it labels the following interval. Document how state was determined. Each power-run sample stream must have increasing times and cover its analysis window. Supply voltage is a measured input, not a guessed nominal battery voltage.

`power_metadata.csv` includes link IDs/board role, instrument model/serial, sampling settings/rate/bandwidth/current range, peak limits, measurement location, optional original export path/hash and normalization method, analysis window in seconds, measurement frequency, state-classification method, battery capacity in mAh, usable-capacity fraction, daily state hours and battery-life assumptions. Unknown instrument specifications stay missing and limitations must describe them.

`power_summary.csv` records window duration, integrated average current (A), sampled peak (A), complete time-weighted voltage (V), maximum sample gap (s), separate state currents (A), state duty cycles (%), unclassified duration (s), and optional modeled battery life (days). Original instrument exports/samples are separate from BLE measurement attempts and their event clocks.
