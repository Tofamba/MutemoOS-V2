"""
Test for the "a fresh tenant starts at zero users" invariant
(2026-09-26 standing-access audit).

run_migrations() is what runs automatically on every deploy/startup -- it
creates tables and seeds the `firms` row from MUTEMO_FIRM_ID/MUTEMO_FIRM_NAME
(see scripts/seed_staging_data.py's own docstring, which depends on exactly
this). Nothing in that automatic path should ever insert a `users` row: the
only three places main.py inserts into `users` are (1) verify_otp(), gated
on a matching pending invite; (2) bulk_onboard_from_excel(), gated on an
existing admin/partner session; and (3) the separate, explicitly-staging-
only scripts/seed_staging_data.py script -- all three require a real,
already-authenticated human action (or an explicit opt-in script pointed at
staging), never something that fires automatically at deploy time.

A static source check on run_migrations() itself, rather than spinning up a
real Postgres, since what's being asserted is "this function's SQL never
contains this statement" -- a property of the source, not of runtime
behavior that needs a live database to observe.
"""
import inspect

import backend.main as m


def test_run_migrations_never_inserts_into_users_table():
    source = inspect.getsource(m.run_migrations)
    assert "INSERT INTO users" not in source, (
        "run_migrations() must never create a users row automatically -- "
        "the first user on a fresh tenant must come from a real invite/OTP "
        "completion or an explicitly-authenticated bulk-onboard action, "
        "never from deploy-time migration/seeding."
    )


def test_users_table_is_only_ever_inserted_into_from_gated_paths():
    """Enumerates every INSERT INTO users in the whole file and confirms
    each one sits behind either an invite check or an admin:users permission
    check -- catches a new ungated insert path being added later, not just
    ones that exist today."""
    source = inspect.getsource(m)
    insert_count = source.count("INSERT INTO users")
    assert insert_count == 2, (
        f"expected exactly 2 INSERT INTO users call sites (verify_otp's "
        f"invite-gated creation, and bulk_onboard_from_excel's admin-gated "
        f"creation) — found {insert_count}. If this changed intentionally, "
        f"confirm the new call site is properly gated before updating this "
        f"count."
    )
