"""Background entry point with startup diagnostics even before app imports."""
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / 'data' / 'runtime'
RUNTIME.mkdir(parents=True, exist_ok=True)
try:
    import app
    app.main()
except BaseException:
    with (RUNTIME / 'startup-error.log').open('a', encoding='utf-8') as log:
        traceback.print_exc(file=log)
    raise
