from src.utils.secret_redaction import redact_secrets


def test_redacts_api_key_query_parameter_and_keeps_error_context():
    result = redact_secrets("request failed for ?key=AIza1234567890abcdef")

    assert result.startswith("request failed for ?key=")
    assert "AIza1234567890abcdef" not in result
    assert "[REDACTED]" in result


def test_redacts_bearer_and_openai_style_tokens():
    result = redact_secrets("Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz")

    assert "sk-abcdefghijklmnopqrstuvwxyz" not in result
    assert "[REDACTED]" in result


def test_preserves_non_secret_error_details():
    assert redact_secrets("boom") == "boom"
