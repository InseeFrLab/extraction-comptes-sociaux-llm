"""Configuration pytest partagée.

Rend importables depuis les tests, sans avoir à les packager :

- `scripts/` et `libs/src/` — le pipeline et le package partagé ;
- `website/scripts/` — `build_data.py` y porte la classification des cellules dont sortent
  les chiffres publiés par le site ;
- `legacy/` — `geometrie_marker.py` y dort, analyse close mais toujours couverte.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

for path in (
    ROOT / "scripts",
    ROOT / "libs" / "src",
    ROOT / "website" / "scripts",
    ROOT / "legacy",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
