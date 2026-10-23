# OVSR — project brief (week of Oct 19–23, 2026)

OVSR = Operational Variance & Safety Response. Owner: Kevin Clark, EHS & Asset Protection, Chewy.
Weekly site status (Normal / Watch / Moderate Elevation / Elevated) from ops-vs-plan triggers, tied to safety outcomes
(recordable, first aid, MSD, MOE), driver evidence, peak glidepath and year-end projections. Two surfaces: the Streamlit
app in Snowflake, and an HTML site snapshot on SharePoint for people without Snowflake.

## Where things live
- App: `EDLDB.SNOP_SANDBOX.MOE_INTELEX_DASHBOARD` (Streamlit in Snowflake)
  - Viewer: https://app.snowflake.com/streamlit/chewy/chewy/#/apps/vde5beb4skfps6vuutms
  - Editor: https://app.snowflake.com/chewy/chewy/#/streamlit-apps/EDLDB.SNOP_SANDBOX.MOE_INTELEX_DASHBOARD/edit
  - Files: `streamlit_app.py`, `ovsr_ext.py` (snapshot builder, Ops × Safety, year end, first-aid model), `ovsr_governance.py`, `ovsr_guide.py`, `snowflake.yml`
  - Runtime: container runtime, compute pool SYSTEM_COMPUTE_POOL_CPU, warehouse CHEWY_ETL_S, owner role SNOP_USER (owner's rights, so no secondary roles)
  - Editor edits auto-save; Run applies; viewers see changes after a fresh session (add `?r=2` before `#` to force it)
- Snapshot file: SharePoint Asset Risk Operations › Shared Documents › OVSR › `OVSR_site_snapshot.html` (file ID 5ff6f7a3-3175-43b3-bb16-5bd688ca6a87)
- SharePoint page: https://chewycomllc.sharepoint.com/sites/AssetRiskOperations/SitePages/OVSR.aspx — embeds the file via
  `_layouts/15/embed.aspx?UniqueId=5ff6f7a3-3175-43b3-bb16-5bd688ca6a87`. Page is still a draft (v0.28).
- OneDrive: shortcut to the OVSR folder in My files. Old copy `OVSR_site_snapshot_20261023 (1).html` is superseded.

## Done this week
- Embedded snapshot errored on tab clicks: SharePoint's sandboxed viewer blocks `history.replaceState` → wrapped in try/catch (file and generator).
- Gave the snapshot a stable home (site library, fixed file name) and repointed the page embed. Restored the page title area after a draft save (versions 0.27/0.28; the 0.24 edit had removed the banner/logo).
- Dashboard fixes: "Drivers & evidence could not render: cannot convert float NaN to integer" (Poisson tail on incomplete weeks); errors now log a traceback; one "data through" date everywhere (provisional latest days dropped in the screened daily view); p-values read "p < 0.001", not "p = 0.000"; clearer note when MET isn't loaded.
- "Build site snapshot (HTML)" now downloads automatically as `OVSR_site_snapshot.html` (same name as the SharePoint file), with a "Download again" fallback.
- Oct 23 (in progress): snapshot made interactive like the app — pick injury measure, ops metric, timing and weeks on the weekly charts; mediation pathway picker (starter paths or custom X → M → Y); sortable tables; projected year-end injury rates on Overview and Safety KPIs › Year end; "Strongest relationships" and the evidence-ledger table collapsed by default. Also: make year-end rates easier to find in the app.

## Decisions
- Snapshot lives in the SharePoint site library, not personal OneDrive; a fixed file name so new builds replace it in place.
- MET: `STAFF_DAILY_DEPARTMENT` is readable only through Kevin's secondary roles; SNOP_USER can't read it. No other MET source SNOP_USER can read (checked UKG "MOT Scheduled", CHEWYBI.EMPLOYEE_ATTENDANCE (stale), RX_FACT_ATTENDANCE_UKG (Rx only)). Not requesting access for now.
- Replacing the SharePoint file automatically: Snowflake has no external access integration (only S3 storage integrations), so the app can't write to SharePoint. Going with Power Automate (a Mac auto-move was the earlier pick; it would need macOS Accessibility + Screen Recording for Claude).

## Open items
1. Power Automate Snowflake connection (premium connector, Entra ID): account `chewy-chewy.snowflakecomputing.com`, database EDLDB, schema SNOP_SANDBOX, warehouse CHEWY_ETL_S, role SNOP_USER, tenant 99879b8e-3b16-427d-902a-05eda342f629. Client ID, client secret and resource URL come from IT (Snowflake External OAuth). Paste the secret straight into Power Automate. Then: a snapshot table in EDLDB.SNOP_SANDBOX, the Build button writes to it, and the flow overwrites `OVSR_site_snapshot.html` keeping the same file ID. Fallback: standard SharePoint flow from a OneDrive-synced download folder.
2. Publish OVSR.aspx (still a draft).
3. Finish and verify the interactive snapshot; fix the Site tab tile "Recordable rate · year end" showing "—".
4. Audit leftovers: truncated metric tiles ("Mode…", "Slip, trip …"), overlapping correlation-map headers, clipped legends/axis titles, `use_container_width` deprecation warnings, Arrow warning on a "Rows" column, "SOWNAS Test" promotions in the weekly note, EDLDB.INTELEX access for event detail, tabs not yet audited (Event detail, Peak glidepath, Safety KPIs, How OVSR Works, Weekly grain).
5. Standing goal: keep a Tableau Cloud version matching the app tab for tab.

## Methods (reference)
- Evidence ledger: Poisson regression of outcome counts with an hours offset, site fixed effects, annual seasonality and site/week-cluster sensitivity; Benjamini–Hochberg q across all tests; α = 0.05; effect = change per typical (1 SD within-site) rise.
- Weekly charts: each line as SDs from its own average over the weeks shown; one site = 4-week rolling; r on weekly values.
- Mediation: within-site demeaned, standardized OLS paths a, b, c, c′; indirect a×b with a 4-week block bootstrap (600 resamples, seed 11).
- Year end: injuries so far + open first aid cases weighted by their chance of becoming recordable + projected days left (S&OP plan hours × hours-to-risk curve × last-13-week rate; the site's normal week after the plan); 80% process-noise range (z = 1.28); Calendar (OSHA 300 year) and Fiscal.
