"""The opt-in example makes one bounded provider request, even with a real floor."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace


async def test_live_example_limits_with_fake_provider(monkeypatch, tmp_path):
    from ofp_playground.session import SessionService

    spec = importlib.util.spec_from_file_location("asp_live", Path(__file__).parents[1] / "examples" / "asp_live.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    class FakeClient:
        def __init__(self, **options):
            assert options["timeout"] == 15 and options["max_retries"] == 0
            self.responses = self

        def create(self, **options):
            calls.append(options)
            return SimpleNamespace(output_text="A synthetic provider result.")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    async def no_monitor(self):
        pass

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("openai.OpenAI", FakeClient)
    monkeypatch.setattr(SessionService, "enable_status", no_monitor)
    await module.run("explicit-test-model")
    assert len(calls) == 1
    assert calls[0]["model"] == "explicit-test-model"
    assert calls[0]["max_output_tokens"] == 128
