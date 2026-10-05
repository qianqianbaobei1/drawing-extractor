"""Shared isolated paths for the unittest suite.

Import before app/store/db. Tests must not use the operator's persistent DB,
settings, uploaded drawings or work directory.
"""
import os
import tempfile

_suite_state = tempfile.TemporaryDirectory(prefix="drawing-extractor-tests-")
os.environ["EXTRACTOR_DATA_DIR"] = _suite_state.name

import store  # noqa: E402

store.DATA_DIR = _suite_state.name
store.PROJECTS_FILE = os.path.join(_suite_state.name, "projects.json")
store.HISTORY_FILE = os.path.join(_suite_state.name, "history.json")
store.SETTINGS_FILE = os.path.join(_suite_state.name, "settings.json")

import app as _app  # noqa: E402

_app.WORKDIR = os.path.join(_suite_state.name, "work")
os.makedirs(_app.WORKDIR, exist_ok=True)
