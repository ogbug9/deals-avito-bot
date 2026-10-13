import pytest

from gpuflip import normalizer


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch):
    """Тесты не читают пользовательские списки слов и настройки из папки проекта."""
    monkeypatch.setenv("GPUFLIP_HOME", str(tmp_path_factory.mktemp("home")))
    normalizer.reload_words()
    yield
    normalizer.reload_words()
