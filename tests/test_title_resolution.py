from subprocess import CompletedProcess

from hermes_pulse.title_resolution import DEFAULT_TITLE_SYNTH_MODEL, fetch_title_from_url


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


def test_default_title_synth_model_matches_supported_codex_model() -> None:
    assert DEFAULT_TITLE_SYNTH_MODEL == "gpt-5.4"
