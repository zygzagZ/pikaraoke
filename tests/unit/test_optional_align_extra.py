"""The ML stack must stay confined to the ``align`` extra.

Downstream consumers reuse pikaraoke's search, metadata and lyrics-fetch
modules without installing several GB of torch. That only holds while no
module on those paths imports the ML stack at module level, which is easy to
break by adding one convenient top-level ``import numpy`` — so the contract is
pinned here rather than left to review.
"""

import importlib
import sys

import pytest

# Packages that arrive only with `pikaraoke[align]`.
ALIGN_ONLY = frozenset(
    {"torch", "librosa", "demucs", "whisperx", "soundfile", "numpy", "silero_vad", "tqdm"}
)

# Modules a consumer may import with the base install alone.
LIGHT_MODULES = [
    "pikaraoke.lib.lyrics",
    "pikaraoke.lib.music_metadata",
    "pikaraoke.lib.metadata_parser",
    "pikaraoke.lib.song_enricher",
    "pikaraoke.lib.events",
    "pikaraoke.lib.http_logging",
    "pikaraoke.lib.youtube_data_api",
    "pikaraoke.lib.youtube_dl",
    "pikaraoke.lib.lyrics_retry_backoff",
]


class _AlignBlocker:
    """Import hook that hides the align-extra packages."""

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ALIGN_ONLY:
            raise ImportError(f"No module named {name!r}")
        return None


@pytest.fixture
def without_align_extra(monkeypatch):
    """Run the body as if `pikaraoke[align]` had never been installed.

    Both the already-imported modules and the ones under test are dropped from
    ``sys.modules``, so the import machinery re-runs and actually hits the
    blocker instead of returning a cached module object.
    """
    for name in list(sys.modules):
        root = name.split(".")[0]
        if root in ALIGN_ONLY or name.startswith("pikaraoke.lib."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_AlignBlocker(), *sys.meta_path])
    yield


@pytest.mark.parametrize("module_name", LIGHT_MODULES)
def test_light_module_imports_without_align_extra(without_align_extra, module_name):
    assert importlib.import_module(module_name)


def test_demucs_processor_names_the_extra(without_align_extra):
    """The ML modules may require the extra, but must say so.

    Every caller imports demucs_processor from inside a function, often on a
    worker thread, where a bare "No module named 'torch'" gives the operator
    nothing to act on.
    """
    with pytest.raises(ImportError) as excinfo:
        importlib.import_module("pikaraoke.lib.demucs_processor")
    assert "pikaraoke[align]" in str(excinfo.value)
