# ClearCast offline evaluation results

Generated 2026-10-09T02:07:19+00:00 · Python 3.13.2 · 1.49 s · 43 scenarios

> Deterministic offline tests with synthetic weather and scripted model outputs. They measure validation, grounding, constraint, session, and contract behaviour of the software. They are not human evaluations of recommendation usefulness and are not evidence of campaign lift, production effectiveness, or general LLM accuracy.

| Metric | Value |
|---|---|
| Scenario pass rate | 100.0% |
| Schema-validity rate (returned plans) | 100.0% |
| Forecast-grounding consistency (30 windows, independent recheck) | 100.0% |
| Hard-constraint compliance (20 windows, independent recheck) | 100.0% |
| Invalid-output rejection rate | 100.0% |
| Repair success rate (invalid once, then valid) | 100.0% |
| Session-isolation correctness | 100.0% |
| API contract correctness (shared fixtures) | 100.0% |

| Scenario | Category | Outcome | Pass |
|---|---|---|---|
| coffee_rainy_mornings | coffee_shop | pending_review | yes |
| coffee_clear_week | coffee_shop | pending_review | yes |
| coffee_cold_snap | extreme_weather | pending_review | yes |
| coffee_poor_air_no_aqi_rule | air_quality | pending_review | yes |
| coffee_out_of_hours_choice_repaired | constraint_repair | pending_review | yes |
| fitness_mild_week | outdoor_fitness | pending_review | yes |
| fitness_rainy_mornings | rain | pending_review | yes |
| fitness_ineligible_choice_repaired | constraint_repair | pending_review | yes |
| fitness_heat_wave | extreme_weather | pending_review | yes |
| fitness_cold_snap | extreme_weather | validation_failed | yes |
| fitness_strong_wind | wind | validation_failed | yes |
| fitness_poor_air_quality | air_quality | validation_failed | yes |
| fitness_missing_weather_fields | missing_fields | pending_review | yes |
| general_storm_week | general | pending_review | yes |
| conflicting_constraints_unsatisfiable | conflicting_constraints | validation_failed | yes |
| conflicting_constraints_invalid_config | conflicting_constraints | error:invalid_request | yes |
| invalid_window_times_persistent | invalid_output | validation_failed | yes |
| elapsed_window_persistent | invalid_output | validation_failed | yes |
| unsupported_evidence_repaired | invalid_output | pending_review | yes |
| unsupported_evidence_persistent | invalid_output | validation_failed | yes |
| value_mismatch_persistent | invalid_output | validation_failed | yes |
| unit_confusion_repaired | invalid_output | pending_review | yes |
| invented_weather_number_persistent | invalid_output | validation_failed | yes |
| kpi_claim_persistent | invalid_output | validation_failed | yes |
| unhedged_hypothesis_repaired | invalid_output | pending_review | yes |
| malformed_output_repaired | malformed_output | pending_review | yes |
| malformed_output_persistent | malformed_output | validation_failed | yes |
| schema_missing_fields_persistent | malformed_output | validation_failed | yes |
| owm_unavailable | provider_failure | validation_failed | yes |
| owm_rate_limited | provider_failure | validation_failed | yes |
| owm_timeout | provider_failure | validation_failed | yes |
| owm_invalid_json | provider_failure | validation_failed | yes |
| geocode_not_found | provider_failure | validation_failed | yes |
| model_server_error | model_failure | error:model_provider_error | yes |
| model_timeout | model_failure | error:model_timeout | yes |
| model_skips_tools | model_failure | validation_failed | yes |
| agent_step_limit | model_failure | validation_failed | yes |
| session_isolation_interleaved | sessions | ok | yes |
| approval_then_revision_is_stale | review | ok | yes |
| rejection_is_distinct_from_approval | review | ok | yes |
| cross_session_review_blocked | sessions | ok | yes |
| validation_failed_cannot_be_approved | review | ok | yes |
| cache_reuse_across_clients | cache | ok | yes |
