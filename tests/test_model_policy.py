import pytest

from hermes_pulse.model_policy import PULSE_CODEX_MODEL, require_pulse_codex_model
from hermes_pulse.summarization.codex_cli import CodexCliInvocation, CodexCliSummarizer


def test_require_pulse_codex_model_accepts_spark() -> None:
    assert require_pulse_codex_model(PULSE_CODEX_MODEL) == PULSE_CODEX_MODEL


def test_require_pulse_codex_model_rejects_non_spark() -> None:
    with pytest.raises(ValueError, match="Hermes Pulse requires gpt-5.3-codex-spark"):
        require_pulse_codex_model("gpt-5.4")


def test_codex_execution_boundaries_reject_non_spark() -> None:
    with pytest.raises(ValueError, match="Hermes Pulse requires gpt-5.3-codex-spark"):
        CodexCliInvocation(model="gpt-5.4")
    with pytest.raises(ValueError, match="Hermes Pulse requires gpt-5.3-codex-spark"):
        CodexCliSummarizer(model="gpt-5.4")
