from subprocess import CompletedProcess

import pytest

from hermes_pulse.summarization.codex_cli import DEFAULT_CODEX_MODEL
from hermes_pulse.title_resolution import (
    DEFAULT_TITLE_SYNTH_MODEL,
    fetch_title_from_url,
    synthesize_title_with_codex_spark,
)


def test_fetch_title_from_url_tolerates_invalid_utf8_bytes(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        return CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=b"<html><head><title>Launch\xcb Update</title></head></html>",
            stderr=b"",
        )

    monkeypatch.setattr("hermes_pulse.title_resolution.subprocess.run", fake_run)

    assert fetch_title_from_url("https://example.com") == "Launch� Update"


def test_all_pulse_codex_defaults_use_spark() -> None:
    assert DEFAULT_CODEX_MODEL == "gpt-5.3-codex-spark"
    assert DEFAULT_TITLE_SYNTH_MODEL == DEFAULT_CODEX_MODEL


def test_title_synthesis_rejects_non_spark_before_subprocess(monkeypatch) -> None:
    def forbidden_run(*_args, **_kwargs):
        raise AssertionError("subprocess must not run for a forbidden model")

    monkeypatch.setattr("hermes_pulse.title_resolution.subprocess.run", forbidden_run)

    with pytest.raises(ValueError, match="Hermes Pulse requires gpt-5.3-codex-spark"):
        synthesize_title_with_codex_spark(
            "title fragment",
            "https://example.com/item",
            model="gpt-5.4",
        )
