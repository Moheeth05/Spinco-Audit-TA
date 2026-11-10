"""Tests skipped ONLY in PostgreSQL mode (SPINCO_TEST_DATABASE_URL_FILE), each for a reason that is specific to SQLite or to this harness - never because the application misbehaves.
Every one of them stays in, and passes in, the normal SQLite suite. See the test report for the PostgreSQL results.

 SQLITE_FILE   inspects or builds the SQLite database FILE itself (DB_PATH, sqlite3, sqlite_master, PRAGMA, EXPLAIN QUERY PLAN, legacy-schema bootstrap, schema-gate probes
               of a temporary SQLite engine), so it has no meaning on a server database.
 SQLITE_IDS    asserts SQLite's behaviour of handing a deleted row's id straight back (and the dangling-pointer policy built around it). PostgreSQL sequences never reuse ids and a
               dangling pointer is illegal there, so the application PARKS a verification instead (services/fk_safety.py); that path is covered by test_postgresql_only.py and by
               test_exception_engine.py::test_29_verification_survives_reprocessing, which passes on both databases.
 NEEDS_PG_DUMP the backup command's end-to-end test needs the PostgreSQL client tools (pg_dump) on PATH.
"""

SQLITE_FILE = {
    "tests/test_central_security.py::test_sqlite_is_tuned_for_concurrent_writers_only_in_the_central_modes",
    "tests/test_central_security.py::test_strict_mode_refuses_a_database_without_the_central_tables_and_does_not_create_them",
    "tests/test_central_security.py::test_the_default_mode_still_upgrades_the_schema_as_before",
    "tests/test_central_storage.py::test_duplicate_history_survives_a_restart",
    "tests/test_central_storage.py::test_serve_mode_never_substitutes_a_local_copy_when_the_central_copy_is_unavailable",
    "tests/test_drive_retry_budget.py::test_the_real_sweep_query_is_served_by_the_state_index_and_no_unused_retry_index_exists",
    "tests/test_drive_schema_gate.py::test_a_positive_answer_is_cached_briefly_and_a_refusal_is_rechecked_soon",
    "tests/test_drive_schema_gate.py::test_no_drive_tables_at_all_is_reported_as_absent_and_not_ready",
    "tests/test_drive_schema_gate.py::test_the_manager_dependency_refuses_before_anything_is_built_or_contacted",
    "tests/test_drive_schema_gate.py::test_the_three_reproduced_review_cases_and_a_wrong_default_are_unhealthy_for_the_gate[missing foreign keys]",
    "tests/test_legacy_bill_migration.py::test_legacy_rows_load_through_the_orm_with_null_evidence",
    "tests/test_legacy_bill_migration.py::test_legacy_rows_validate_through_the_raw_text_fallback_without_inventing_anything",
    "tests/test_legacy_bill_migration.py::test_negative_control_the_legacy_schema_is_unreadable_until_migrated",
    "tests/test_legacy_bill_migration.py::test_repeat_validation_of_legacy_rows_does_not_duplicate_results",
    "tests/test_legacy_bill_migration.py::test_run_light_migrations_alone_is_idempotent_on_a_fresh_full_schema",
    "tests/test_legacy_bill_migration.py::test_startup_migrates_a_legacy_database_additively_and_idempotently[all-three-missing]",
    "tests/test_legacy_bill_migration.py::test_startup_migrates_a_legacy_database_additively_and_idempotently[only-amount-missing]",
    "tests/test_legacy_bill_migration.py::test_startup_migrates_a_legacy_database_additively_and_idempotently[only-flags-missing]",
    "tests/test_legacy_bill_migration.py::test_the_application_database_file_is_never_touched_by_these_tests",
    "tests/test_phase1_hardening.py::test_bill_matched_expense_id_foreign_key_enforced_when_pragma_is_on",
    "tests/test_stale_evidence_recovery.py::test_backup_simulated_failure_after_the_deletion_commit_and_restore",
    "tests/test_stale_evidence_recovery.py::test_predicate_each_flag_column_on_its_own",
    "tests/test_stale_evidence_recovery.py::test_predicate_on_a_database_without_the_evidence_columns_never_queries_them",
    "tests/test_stale_evidence_recovery.py::test_predicate_on_a_migrated_schema_lists_exactly_the_new_format_reports",
    "tests/test_stale_evidence_recovery.py::test_predicate_on_a_partial_schema_does_not_reference_the_missing_column",
    "tests/test_stale_evidence_recovery.py::test_the_test_database_is_the_disposable_one",
    "tests/test_verification_evidence_policy.py::test_startup_migrates_a_legacy_verification_table_and_a_legacy_row_is_then_evaluated_correctly",
    "tests/test_verification_evidence_policy.py::test_the_test_database_is_the_disposable_one",
    "tests/test_verification_history.py::test_a_fresh_database_gets_the_history_table_from_startup",
    "tests/test_verification_history.py::test_run_light_migrations_alone_does_not_create_or_alter_the_history_table",
    "tests/test_verification_history.py::test_startup_adds_the_history_table_to_an_existing_database_without_touching_any_row_and_is_repeatable[False]",
    "tests/test_verification_history.py::test_startup_adds_the_history_table_to_an_existing_database_without_touching_any_row_and_is_repeatable[True]",
    "tests/test_verification_history.py::test_startup_does_not_recreate_or_empty_an_existing_history_table",
}

SQLITE_IDS = {
    "tests/test_verification_carryover.py::test_a_changed_claim_is_never_resolved_by_an_id_that_sqlite_would_reuse",
    "tests/test_verification_carryover.py::test_a_changed_fact_leaves_the_verification_historical_and_the_claim_open[amount]",
    "tests/test_verification_carryover.py::test_a_changed_fact_leaves_the_verification_historical_and_the_claim_open[category]",
    "tests/test_verification_carryover.py::test_a_changed_fact_leaves_the_verification_historical_and_the_claim_open[date]",
    "tests/test_verification_carryover.py::test_a_missing_fact_is_never_a_match[NULL-amount]",
    "tests/test_verification_carryover.py::test_a_missing_fact_is_never_a_match[NULL-category]",
    "tests/test_verification_carryover.py::test_a_missing_fact_is_never_a_match[NULL-date]",
    "tests/test_verification_carryover.py::test_a_missing_fact_is_never_a_match[NULL-s_no]",
    "tests/test_verification_carryover.py::test_a_second_reprocess_does_not_duplicate_anything",
    "tests/test_verification_carryover.py::test_control_reprocessing_the_newest_report_where_sqlite_reuses_ids",
    "tests/test_verification_carryover.py::test_no_row_for_the_s_no_leaves_the_verification_historical",
    "tests/test_verification_carryover.py::test_reserve_ids_only_changes_allocation_when_a_verification_stays_put",
    "tests/test_verification_carryover.py::test_the_test_database_is_the_disposable_one",
    "tests/test_verification_evidence_policy.py::test_reserved_expense_ids_keep_bill_matching_and_the_exclusion_intact",
    "tests/test_verification_flow_integrity.py::test_a_new_verification_reusing_an_old_verification_id_leaves_the_retired_history_untouched",
    "tests/test_verification_flow_integrity.py::test_a_report_reusing_the_ids_of_a_deleted_report_cannot_inherit_its_approval[legacy_unsigned]",
    "tests/test_verification_flow_integrity.py::test_a_report_reusing_the_ids_of_a_deleted_report_cannot_inherit_its_approval[signed]",
    "tests/test_verification_flow_integrity.py::test_report_level_verifications_are_retired_too",
    "tests/test_verification_flow_integrity.py::test_the_documented_orphan_queries_find_exactly_the_legacy_rows_and_are_read_only",
}

NEEDS_PG_DUMP = {
    "tests/test_release_candidate_server_config.py::test_the_backup_command_end_to_end_against_the_applications_own_database",
}

REASONS = {
    **{n: "SQLite-specific: inspects/builds the SQLite database file or its legacy schema" for n in SQLITE_FILE},
    **{n: "SQLite-specific: asserts SQLite id reuse / the dangling-pointer policy (PostgreSQL parks the verification instead)" for n in SQLITE_IDS},
    **{n: "needs the PostgreSQL client tools (pg_dump) on PATH" for n in NEEDS_PG_DUMP},
}