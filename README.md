# demos

## Norwegian Salmon Monitor (Mowi lens)

A weekly-refreshed dashboard of the data the Norwegian government publishes about salmon
farming, with Mowi's footprint highlighted. Everything lives in [`salmon-dashboard/`](salmon-dashboard/).

**What refreshes every week (Wednesdays 08:30 UTC, after Statistics Norway's 08:00 Oslo release):**

| Data | Publisher | Cadence |
|---|---|---|
| Export price and volume of fresh and frozen farmed salmon (table 03024) | Statistics Norway (SSB) | weekly |
| Salmon exports by destination country, HS 0302.14 / 0303.13 (table 08799) | SSB external trade | monthly |
| Sales of slaughtered salmon, quantity and value | SSB aquaculture statistics | annual |
| Standing biomass, harvest, mortality, feed, smolt release per production area | Fiskeridirektoratet biomass register | monthly |
| Aquaculture register: localities, licence holders, capacity (Mowi filter, industry map) | Fiskeridirektoratet | continuous |
| Escape reports (Rømmingshendelser) | Fiskeridirektoratet | continuous |
| Sea lice, treatments, PD/ILA per locality (needs free API credentials, see below) | BarentsWatch / Mattilsynet | weekly |
| EUR/NOK | Norges Bank | daily |
| Traffic light colours for the 13 production areas | Nærings- og fiskeridepartementet (kept in `config.json`) | every second year |

### How it works

1. `.github/workflows/salmon-dashboard-data.yml` runs `salmon-dashboard/scripts/fetch_data.py`
   every Wednesday (and on demand from the Actions tab, "Run workflow").
2. The script writes `salmon-dashboard/data/latest.json`, computes the differences against the
   previous run into `data/changes.json` (new weeks, new months, revisions, new escape reports,
   Mowi register changes, lice alerts, source failures) and appends to `data/changelog.json`.
   Sources are independent: a failing one keeps last week's data and is flagged on the page.
3. The job commits the JSON back to this branch. `salmon-dashboard/index.html` is a static page
   that reads those files, so wherever the folder is served from, the dashboard is current.

### Viewing the dashboard

* **GitHub Pages (recommended, updates automatically).** Repository *Settings → Pages → Build and
  deployment → Source: Deploy from a branch → Branch `claude/charming-fermi-dkpsie` (or `main`
  once merged), folder `/ (root)` → Save*. The page is then at
  `https://maxhyde.github.io/demos/salmon-dashboard/` and every weekly data commit redeploys it.
* **Locally:** `cd salmon-dashboard && python3 -m http.server 8000` and open
  <http://localhost:8000/>. (Opening `index.html` straight from disk is blocked by browsers'
  file-URL rules for `fetch`.)

### Switching on sea-lice data (BarentsWatch)

1. Sign in at <https://www.barentswatch.no/minside/> and create an API client (free).
2. Add two repository secrets under *Settings → Secrets and variables → Actions*:
   `BARENTSWATCH_CLIENT_ID` and `BARENTSWATCH_CLIENT_SECRET`.
3. The next run backfills 52 weeks: national and per-production-area averages, sites over the
   limit, PD/ILA counts, and every Mowi locality's weekly count.

### Maintenance notes

* `salmon-dashboard/config.json` holds the source list, production-area names, Mowi name patterns
  (`MOWI`, `MARINE HARVEST`) and the traffic-light decision. Update the `traffic_lights` block when
  the ministry publishes the next decision (expected 2028).
* GitHub disables scheduled workflows in repositories with no activity for 60 days; the job's own
  commits keep it active, but if the "Data refreshed" date on the page stops moving, open the
  Actions tab and run the workflow manually once.
* Run the fetcher locally with `pip install -r salmon-dashboard/requirements.txt &&
  python3 salmon-dashboard/scripts/fetch_data.py` (set `DEBUG=1` for verbose logs, `KEEP_RAW=1`
  to keep downloaded workbooks under `data/raw/`).

Data is used under the Norwegian Licence for Open Government Data (NLOD).
