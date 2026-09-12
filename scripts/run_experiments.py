"""Run public sample checks and actual Chat2Scenario on synthetic trajectories."""
import subprocess
import sys
from pathlib import Path

project = Path(__file__).resolve().parents[1]
for command in ("benchmark", "bootstrap", "demo"):
    print(f"Running {command}...", flush=True)
    subprocess.run([sys.executable, "-m", "scenario_metadata", command], cwd=project, check=True)
