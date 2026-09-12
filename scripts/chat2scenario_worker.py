"""Explicit entry point for the isolated Chat2Scenario worker."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from scenario_metadata.chat2scenario import worker_main

if __name__ == "__main__":
    worker_main()
