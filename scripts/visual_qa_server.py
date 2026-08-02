import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gongkao.web.application import run

if __name__ == "__main__":
    run(port=int(os.environ.get("VISUAL_QA_PORT", "5169")), db_path=ROOT / ".visual-qa" / "gongkao.sqlite3")
