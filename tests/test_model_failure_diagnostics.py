"""Value-free failure metadata; no network, credentials or business execution."""
import copy
import json
import subprocess
import sys

import pytest

from trading_system.redline import LLMUnavailable
from trading_system.redline.model_diagnostics import (
    RequiredModelUnavailable, model_failure_diagnostic, validate_model_diagnostic,
)


def diagnostic(**extra):
    return {"schema": "tiger.model-failure.v1", "provider": "deepseek",
            "code": "OUTPUT_SCHEMA_MISMATCH", "schema_path": "/chains/2/heat",
            "schema_keyword": "maximum", **extra}


@pytest.mark.parametrize("path", ["", "/chains/2/heat", "/documents/0/*", "/a_b/c.d:e-f"])
def test_schema_location_is_closed_and_detached(path):
    original = diagnostic(schema_path=path)
    result = validate_model_diagnostic(original)
    assert result == original and result is not original
    result["schema_path"] = "/changed"
    assert original["schema_path"] == path


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(schema="unknown"),
    lambda d: d.update(provider="other"),
    lambda d: d.update(code="arbitrary failure fixture-secret"),
    lambda d: d.update(code=["OUTPUT_SCHEMA_MISMATCH"]),
    lambda d: d.update(content="fixture-secret"),
    lambda d: d.update(reasoning_content="fixture-secret"),
    lambda d: d.update(headers={"Authorization": "fixture-secret"}),
    lambda d: d.update(http_status=401),
    lambda d: d.update(schema_path="/chains/2/heat\nfixture-secret"),
    lambda d: d.update(schema_path="https://example.com/?token=fixture-secret"),
    lambda d: d.update(schema_path="/" + "a" * 513),
    lambda d: d.update(schema_keyword="arbitrary"),
    lambda d: d.update(schema_keyword=[]),
    lambda d: d.pop("schema_keyword"),
    lambda d: d.pop("schema_path"),
    lambda d: d.update(code="NETWORK_TIMEOUT"),
])
def test_untrusted_fields_types_and_locations_are_rejected_without_echo(mutate):
    value = diagnostic()
    mutate(value)
    with pytest.raises(ValueError) as error:
        validate_model_diagnostic(value)
    assert "fixture-secret" not in str(error.value)


@pytest.mark.parametrize("status", [100, 401, 429, 500, 599])
def test_http_status_has_a_known_code_and_preserves_safe_exception_label(status):
    error = RequiredModelUnavailable("HTTP_ERROR", http_status=status)
    assert isinstance(error, LLMUnavailable)
    assert str(error) == f"DeepSeek required: HTTP_{status}"
    assert model_failure_diagnostic(error) == {
        "schema": "tiger.model-failure.v1", "provider": "deepseek",
        "code": "HTTP_ERROR", "http_status": status,
    }


@pytest.mark.parametrize("status", [None, True, False, 99, 600, "401", 401.0, []])
def test_http_status_cannot_be_missing_or_a_coerced_number(status):
    with pytest.raises(ValueError):
        RequiredModelUnavailable("HTTP_ERROR", http_status=status)


def test_only_exact_trusted_exception_metadata_is_projected():
    generic = LLMUnavailable("fixture-secret")
    generic.diagnostic = diagnostic()
    assert model_failure_diagnostic(generic) is None
    class UntrustedSubclass(RequiredModelUnavailable):
        @property
        def diagnostic(self):
            raise AssertionError("Subclass property must never be accessed")
    assert model_failure_diagnostic(UntrustedSubclass("NETWORK_TIMEOUT")) is None
    trusted = RequiredModelUnavailable("NETWORK_TIMEOUT")
    trusted.content = "fixture-secret"
    first = model_failure_diagnostic(trusted)
    first["content"] = "fixture-secret"
    assert "fixture-secret" not in json.dumps(model_failure_diagnostic(trusted))
    trusted._diagnostic = {**copy.deepcopy(first), "content": "fixture-secret"}
    assert model_failure_diagnostic(trusted) is None


def test_cold_imports_have_no_transport_dependency_or_import_cycle():
    script = (
        "import sys; import trading_system.agent_evidence; "
        "from trading_system.redline.model_diagnostics import RequiredModelUnavailable; "
        "assert 'requests' not in sys.modules; "
        "assert RequiredModelUnavailable('NETWORK_TIMEOUT').diagnostic['code']=='NETWORK_TIMEOUT'"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
