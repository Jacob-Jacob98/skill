"""Run the packaged offline tests without relying on a developer PYTHONPATH."""
from pathlib import Path
import sys
import unittest


if __name__ == '__main__':
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here.parent / 'scripts'))
    suite = unittest.defaultTestLoader.discover(str(here), pattern='test_*.py')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
