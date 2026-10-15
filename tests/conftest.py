import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["HAUSLICHT_BACKENDS"] = "mock"  # the core tests never load Bluetooth
