# G1 — mutation re-test of the groups access-control code (2026-11-12)

Mandate (docs/tasks/g1.md PART 1, final step): rerun mutation testing on
the **group audience computation**, the **membership check**, the
**last-admin lock**, and the **group filter**; report survivors.

Harness: `scripts/mutation/run-g1-mutations.sh` — ten hand-written
mutants applied one at a time to the working tree (compose `test`
container, real `rag_test` Postgres), each run against the targeted
offline suites; the mutated file is restored byte-identical from git
afterwards and the restore is asserted. Baseline (unmutated selection):
**76 passed / 0 failed**.

| Mutation (security line broken) | Result | Killed by |
|---|---|---|
| **M-A1** — archived groups re-enter the audience (`Group.is_active.is_(True)` dropped from `audiences_for`) | **KILLED** (1 failed) | `test_archived_group_is_in_no_audience_and_readable_by_no_one` |
| **M-A2** — the `user:<self>` token dropped from `audiences_for` (own private docs unreachable) | **KILLED** (9 failed) | audience pin `test_audiences_for_zero_one_two_groups` + filter consumers |
| **M-A3** — the `"org"` token dropped (org docs unreachable) | **KILLED** (9 failed) | same pins |
| **M-B1** — `can_read` group branch inverted (`membership is not None` → `is None`) | **KILLED** (2 failed) | `test_can_read_mirrors_audiences_and_fails_closed` |
| **M-B2** — every member counts as an editor (`role == "editor"` dropped from `is_editor`) | **KILLED** (2 failed) | `test_membership_role_change_flips_only_editor_powers_c1` + members-route 403 pin |
| **M-B3** — `get_membership` matches ANY member of the group (user_id filter dropped) | **KILLED** (11 failed) | `test_members_route_404_outsider_403_member_editor_ok` (outsider → 403 instead of 404) + membership uniqueness surface |
| **M-C1** — the H3 race fix removed (`with_for_update()` dropped from `active_admin_count`) | **KILLED** (20 failed) | `tests/security/test_last_admin_race.py` went 20/20 red — the exact H3 red state |
| **M-C2** — DISABLED admins count toward the last-admin guard (`is_active` filter dropped) | **KILLED** (10 failed) | the B6 last-admin pins in `tests/test_admin_api.py` (demoting/disabling/deleting the sole ACTIVE admin must 409 even with a disabled admin present) |
| **M-D1** — `filter_for` returns a static org-only filter (groups + private tokens dropped) | **KILLED** (9 failed) | audience/filter pins + the full-chat isolation proofs |
| **M-D2** — chat wiring regresses to `ORG_ONLY_FILTER` (resolver bypassed) | **KILLED** (3 failed) | `test_authenticated_chat_sends_user_specific_filter` + resolver pins |

## Survivor analysis

**Zero survivors.** The first harness execution reported M-C1/M-C2 as
"survived", but that was a HARNESS BUG, not a real result: the two lock
mutants were invoked with an empty extra-test argument, so the race suite
(20 tests, ~14s alone) never ran — the 42-test count proved the selection
was the base set only. With the race + admin suites correctly included,
both mutants are killed (M-C1 20/20 red = the documented H3 red state;
M-C2 10 red = the B6 pins). The harness now hardcodes the correct extra
suites for the lock mutants.

## Verdict

**10/10 mutations killed, zero survivors.** Every mandated target — the
group audience computation, the membership check, the last-admin lock,
and the group filter — is covered by at least two independent killers
(a shape pin and a behavioral proof).
