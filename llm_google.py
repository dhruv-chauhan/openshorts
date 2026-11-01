"""Lazy compatibility handles for legacy Gemini code, never needed by Foundry."""
import importlib

from clip_pipeline.profile import clip_only_mode


class _LazyGoogleModule:
    def __init__(self, name):
        object.__setattr__(self, "_module_name", name)

    def _load(self):
        if clip_only_mode():
            raise RuntimeError("Gemini is disabled in the clip-only profile")
        return importlib.import_module(self._module_name)

    def __getattr__(self, name):
        return getattr(self._load(), name)

    def __setattr__(self, name, value):
        # Preserve existing monkeypatching of main.genai.Client in legacy tests.
        setattr(self._load(), name, value)


genai = _LazyGoogleModule("google.genai")
genai_types = _LazyGoogleModule("google.genai.types")
