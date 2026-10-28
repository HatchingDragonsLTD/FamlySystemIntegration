"""Where the five GENERATED reference files live. The one place that knows their names and their location.

The five files are written by the pull-* commands (and by `reference-refresh`):

    session_catalogue, session_titles, groups_catalogue, products_catalogue, institution_defaults

Resolution, per file, first match wins:

  1. the file's own variable (SESSION_CATALOGUE_FILE, SESSION_TITLES_FILE, GROUPS_CATALOGUE_FILE,
     PRODUCTS_CATALOGUE_FILE, INSTITUTION_DEFAULTS_FILE), exactly as before;
  2. REFERENCE_DIR/<file name>, when REFERENCE_DIR is set (the droplet: /home/drew/famly-reference);
  3. the repo's reference/ directory (unchanged behaviour when nothing is set).

`catalogue.json` (the sites) and `funding_rules.json` are HAND-MAINTAINED, stay in the repo's reference/
and are not resolved here. Every reader and every pull of the five files goes through `path_for`; a test
fails if one builds the path itself. Every loader re-reads its file on every call (nothing is cached in
process memory), so a refreshed file is picked up without a restart.
"""

import os
from pathlib import Path

NAMES = ("session_catalogue", "session_titles", "groups_catalogue", "products_catalogue", "institution_defaults")

ENV_VARS = {
    "session_catalogue": "SESSION_CATALOGUE_FILE",
    "session_titles": "SESSION_TITLES_FILE",
    "groups_catalogue": "GROUPS_CATALOGUE_FILE",
    "products_catalogue": "PRODUCTS_CATALOGUE_FILE",
    "institution_defaults": "INSTITUTION_DEFAULTS_FILE",
}

REFERENCE_DIR_VAR = "REFERENCE_DIR"
REPO_DIR = Path(__file__).resolve().parent.parent / "reference"


def file_name(name: str) -> str:
    return f"{name}.json"


def default_path(name: str) -> Path:
    """The file inside the repo's reference/ directory (what nothing-set resolves to)."""
    return REPO_DIR / file_name(name)


def reference_dir() -> Path | None:
    """REFERENCE_DIR when set (and not blank), else None."""
    value = os.environ.get(REFERENCE_DIR_VAR, "").strip()
    return Path(value) if value else None


def path_for(name: str) -> Path:
    """The file in use: its own variable, else REFERENCE_DIR, else the repo's reference/ directory."""
    override = os.environ.get(ENV_VARS[name], "").strip()
    if override:
        return Path(override)
    directory = reference_dir()
    return directory / file_name(name) if directory else default_path(name)


def all_paths() -> dict[str, Path]:
    return {name: path_for(name) for name in NAMES}
