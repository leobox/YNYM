import sys
from pathlib import Path

# exit_engine.py etc. live at the quant-collector root, one level up from tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
