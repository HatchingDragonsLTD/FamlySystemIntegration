"""The test package. Importing it installs the suite-wide guard (see network_guard.py): the real credentials are
blanked and any real outbound request raises. `python -m unittest discover -s tests` loads
test_network_guard.py (which imports it), and every module that imports `tests.<anything>` imports this first."""

from tests import network_guard  # noqa: F401
