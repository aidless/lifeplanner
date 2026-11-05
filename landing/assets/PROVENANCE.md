# Asset provenance

All screenshots in this folder were captured from this repository's own demo output on 2026-11-05. No real user data is present. The calibration card says the sample is too small to support a conclusion.

| File | Origin | Command / capture |
|---|---|---|
| `dashboard-desktop.png` | `lifeplan` demo dashboard, synthetic seed data from `lifeplan/seed.py` | headless Chrome at 1440×1100, `file://` on `landing/demo/dashboard.html` |
| `dashboard-proof.png` | The same synthetic demo, top of the page including the sample-size verdict | headless Chrome at 1440×900, deviceScaleFactor 2 |
| `demo/dashboard.html` | Snapshot of the same synthetic demo, committed so the landing works when opened from the repo | `python3 -m lifeplan dashboard` after `init --seed` |
| `og.png` | This landing page (`landing/index.html`) | headless Chrome at 1200×630 |
| `icon.svg` | Authored for this landing page | — |

The demo seed is explicitly fictional (`lifeplan/seed.py`); it is not a record of a real person.
