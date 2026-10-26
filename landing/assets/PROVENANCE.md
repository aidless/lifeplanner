# Asset provenance

All screenshots in this folder were captured from this repository's own demo output on 2026-10-25. No real user data is present.

| File | Origin | Command / capture |
|---|---|---|
| `dashboard-desktop.png` | `lifeplan` demo dashboard, synthetic seed data from `lifeplan/seed.py` | `make demo`, then headless Chrome at 1440×1000 |
| `dashboard-proof.png` | The same demo dashboard, cropped to the banner + KPI strip + calibration card | headless Chrome at 1440×775, deviceScaleFactor 2 |
| `demo/dashboard.html` | Snapshot of the same synthetic demo, committed so the landing works when opened from the repo | `make site`, then copied from `dist/demo/dashboard.html` |
| `og.png` | This landing page (`landing/index.html`) | headless Chrome at 1200×630 |
| `icon.svg` | Authored for this landing page | — |

The demo seed is explicitly fictional (`lifeplan/seed.py`); it is not a record of a real person.
