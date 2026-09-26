"""app/vault/limits.py: the destructive/pacing constants stay constants
(phase-8 plan section 3, decided as "not settings" in docs/decisions.md's
"8a -- limits that are constants, not settings").

A deploy must not be able to widen `Settings` with an env var of these
names and have it do anything -- there must be no field to widen.
"""

from __future__ import annotations

import datetime

from app.config import Settings
from app.vault import limits

NOT_SETTINGS = (
    "VAULT_DELETE_GRACE_S",
    "VAULT_SYNC_WARMUP_S",
    "VAULT_MASS_DELETE_MAX",
    "VAULT_HOLD_TTL_DAYS",
)


def test_settings_has_no_field_for_any_of_these():
    fields = Settings.model_fields
    for name in NOT_SETTINGS:
        assert name not in fields, f"{name} must stay a constant, not a Settings field"


def test_the_values_are_what_the_plan_says():
    assert limits.DELETE_GRACE_S == 600
    assert limits.SYNC_WARMUP_S == 300
    assert limits.MASS_DELETE_MAX == 3
    assert limits.MASS_DELETE_WINDOW == datetime.timedelta(hours=1)
    assert limits.HOLD_TTL_DAYS == 7
    assert limits.FACT_MAX_CHARS == 300
