# lifeplan landing

Static landing page + a freshly generated demo dashboard.

```bash
make site                 # writes dist/
python3 -m http.server -d dist 8000
# open http://127.0.0.1:8000/
```

`make site` runs `lifeplan init --seed`, then generates `dist/demo/dashboard.html`,
`report.md`, and `data.json` from the synthetic seed. The landing links to that
generated demo; do not hand-copy a dashboard into `landing/`.

GitHub Pages deploys `dist/` on every push to `main` via
`.github/workflows/pages.yml`.
