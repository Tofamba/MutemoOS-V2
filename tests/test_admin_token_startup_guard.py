"""
Unit tests for the MUTEMO_ADMIN_TOKEN=unset startup guard in backend/main.py
(2026-09-26 standing-access audit).

require_admin_token() silently allows access when ADMIN_TOKEN is unset --
correct for a local dev import, but on a deployed service it would leave
every bare-require_admin_token() endpoint (admin reindex/reset-chromadb,
verify-chunk-hashes, bootstrap, etc.) reachable with no credential at all.
This guard refuses to start rather than silently serve those unauthenticated,
mirroring test_dev_auth_guard.py's own subprocess-import approach for the
same reason: the guard runs at module-import time, so it can't be exercised
by importing backend.main directly in a process that has already imported
and cached it.
"""
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every env var whose presence could influence AUTH_ENABLED, MUTEMO_ADMIN_TOKEN,
# or either startup guard -- stripped before each test sets exactly what it
# needs, so a developer's own local .env can't make these tests flaky.
_RELEVANT_VARS = [
    "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID",
    "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
    "RESEND_API_KEY", "SMTP_HOST",
    "MUTEMO_ALLOW_DEV_AUTH", "MUTEMO_ADMIN_TOKEN",
    "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_SERVICE_NAME",
]


def _run_import_in_subprocess(extra_env: dict) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in _RELEVANT_VARS}
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", "import backend.main"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_local_dev_with_no_railway_env_is_unaffected():
    """No RAILWAY_ENVIRONMENT_NAME at all (a developer's own machine) must
    keep working with no MUTEMO_ADMIN_TOKEN set -- this guard is only about
    deployed services, not local dev ergonomics."""
    result = _run_import_in_subprocess({"RESEND_API_KEY": "fake-key-for-test"})
    assert result.returncode == 0, f"expected clean import, got:\n{result.stderr}"


def test_railway_deployment_with_no_admin_token_fails_to_start():
    """The core guard: a Railway-hosted service with no MUTEMO_ADMIN_TOKEN
    configured must refuse to start, not silently leave every
    require_admin_token()-gated endpoint open with no credential at all."""
    result = _run_import_in_subprocess({
        "RAILWAY_ENVIRONMENT_NAME": "production",
        "RAILWAY_SERVICE_NAME": "MutemoOS-V2",
        "RESEND_API_KEY": "fake-key-for-test",
    })
    assert result.returncode != 0, "expected import to fail (raise RuntimeError), but it succeeded"
    assert "MUTEMO_ADMIN_TOKEN" in result.stderr


def test_railway_deployment_with_admin_token_set_starts_fine():
    """A properly configured deployment (MUTEMO_ADMIN_TOKEN set) must never
    be blocked by this guard."""
    result = _run_import_in_subprocess({
        "RAILWAY_ENVIRONMENT_NAME": "production",
        "RAILWAY_SERVICE_NAME": "MutemoOS-V2",
        "RESEND_API_KEY": "fake-key-for-test",
        "MUTEMO_ADMIN_TOKEN": "some-real-token-value",
    })
    assert result.returncode == 0, f"expected clean import, got:\n{result.stderr}"


def test_admin_token_guard_is_independent_of_the_auth_enabled_guard():
    """A Railway deployment can fail on EITHER guard independently -- here,
    AUTH_ENABLED is properly configured (real OTP channel) but
    MUTEMO_ADMIN_TOKEN is still missing, and that alone must still refuse
    to start."""
    result = _run_import_in_subprocess({
        "RAILWAY_ENVIRONMENT_NAME": "production",
        "RAILWAY_SERVICE_NAME": "MutemoOS-V2",
        "RESEND_API_KEY": "fake-key-for-test",
    })
    assert result.returncode != 0
    assert "MUTEMO_ADMIN_TOKEN" in result.stderr
    assert "AUTH_ENABLED" not in result.stderr  # the OTHER guard didn't also fire
