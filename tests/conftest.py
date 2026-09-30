import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
FIXTURES = ROOT / "fixtures"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest


def load_fixture(name):
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return text.split("\n")


@pytest.fixture
def fixture_lines():
    return load_fixture
