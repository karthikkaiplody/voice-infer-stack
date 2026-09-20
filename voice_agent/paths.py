"""Every folder and file the code reads or writes, in one place.

Paths are built from this file's own location, so nothing depends on the folder
you happen to run a command from.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Things you can edit
AGENTS_DIR = REPO_ROOT / "agents"                       # one folder per agent: prompt + notes

# Recordings and traces
AUDIO_FIXTURES_DIR = REPO_ROOT / "fixtures" / "audio"   # WAV utterances + manifest.json
TELEMETRY_FIXTURES_DIR = REPO_ROOT / "fixtures" / "telemetry"  # recorded contract events
ARTIFACTS_DIR = REPO_ROOT / "artifacts"                 # traces you record, reference traces

# The event contract
CONTRACT_SCHEMA = REPO_ROOT / "telemetry_contract" / "v1.schema.json"

# Downloaded models (git-ignored)
PIPER_DIR = REPO_ROOT / "models" / "piper"

# The built browser UI (`make ui-build`, git-ignored)
UI_DIST = REPO_ROOT / "ui" / "dist"
