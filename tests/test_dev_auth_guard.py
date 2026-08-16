"""
Unit tests for the AUTH_ENABLED=False startup guard in backend/main.py.

The guard runs at module-import time (right after AUTH_ENABLED is
computed), so it can't be exercised by importing backend.main directly in
this process — main.py is already imported and cached by the rest of the
test suite, and re-importing an ~8000 line module via importlib.reload for
one test would be fragile. Each case instead spawns a real subprocess with
a controlled environment and checks how it exits, the same way the fix was
verified by hand during development.
"""
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every env var whose presence could influence AUTH_ENABLED or the guard —
# stripped from the child's environment before each test sets exactly what
# it needs, so a developer's own local .env can't make these tests flaky.
_RELEVANT_VARS = [
    "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID",
    "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
    "RESEND_API_KEY", "SMTP_HOST",
    "MUTEMO_ALLOW_DEV_AUTH",
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
    keep working with AUTH_ENABLED=False and no flag — this guard is only
    about deployed services, not local dev ergonomics."""
    result = _run_import_in_subprocess({})
    assert result.returncode == 0, f"expected clean import, got:\n{result.stderr}"


def test_railway_deployment_with_no_auth_and_no_flag_fails_to_start():
    """The core guard: a Railway-hosted service with AUTH_ENABLED=False and
    no explicit opt-in must refuse to start, not silently serve traffic
    under a synthetic dev user."""
    result = _run_import_in_subprocess({
        "RAILWAY_ENVIRONMENT_NAME": "production",
        "RAILWAY_SERVICE_NAME": "MutemoOS-V2",
    })
    assert result.returncode != 0, "expected import to fail (raise RuntimeError), but it succeeded"
    assert "MUTEMO_ALLOW_DEV_AUTH" in result.stderr


def test_railway_deployment_with_explicit_dev_auth_flag_starts_fine():
    """Staging (or any Railway service) that legitimately wants the dev-auth
    fallback must be able to opt in explicitly."""
    result = _run_import_in_subprocess({
        "RAILWAY_ENVIRONMENT_NAME": "production",
        "RAILWAY_SERVICE_NAME": "mutemoos-staging",
        "MUTEMO_ALLOW_DEV_AUTH": "true",
    })
    assert result.returncode == 0, f"expected clean import, got:\n{result.stderr}"


def test_railway_deployment_with_real_auth_configured_needs_no_flag():
    """A properly configured production service (real OTP channel, so
    AUTH_ENABLED=True) must never need the dev-auth flag at all."""
    result = _run_import_in_subprocess({
        "RAILWAY_ENVIRONMENT_NAME": "production",
        "RAILWAY_SERVICE_NAME": "MutemoOS-V2",
        "RESEND_API_KEY": "fake-key-for-test",
    })
    assert result.returncode == 0, f"expected clean import, got:\n{result.stderr}"
