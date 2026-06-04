"""pytest shim for util/test_splices.py.

Its test functions take a positional ``tmp`` directory, supplied by ``main()``'s
``tempfile.TemporaryDirectory`` when the suite runs as ``python util/test_splices.py``.
Under pytest, this fixture supplies the same thing so the tests collect cleanly
instead of erroring on a missing ``tmp`` fixture.
"""
import tempfile

import pytest


@pytest.fixture
def tmp():
    with tempfile.TemporaryDirectory() as d:
        yield d
