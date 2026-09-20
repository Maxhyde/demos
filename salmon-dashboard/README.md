# Norwegian salmon farming dashboard (Mowi lens)

Weekly-refreshed dashboard of the data the Norwegian government publishes about salmon
farming, with Mowi's licences, localities, lice status and escapes highlighted.

* `index.html` – the dashboard (static, reads `data/latest.json` and `data/changes.json`)
* `scripts/fetch_data.py` – fetches every source and writes the JSON files
* `config.json` – sources, production areas, traffic-light decision, Mowi matching rules
* `.github/workflows/salmon-dashboard-data.yml` – runs the fetch every Wednesday and commits the result

See the repository README for the full documentation.
