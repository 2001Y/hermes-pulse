PULSE_CODEX_MODEL = "gpt-5.3-codex-spark"


def require_pulse_codex_model(model: str) -> str:
    if model != PULSE_CODEX_MODEL:
        raise ValueError(
            f"Hermes Pulse requires {PULSE_CODEX_MODEL}; received {model!r}"
        )
    return model
