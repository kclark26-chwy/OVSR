"""Shared, read-only explanation for the dashboard and HTML snapshots."""
from html import escape


GUIDE_HTML = """
<h2>How OVSR Works</h2>
<p>Every site gets one status per Sunday-start week. Read the status and workload pressure first; open the sections below for the rules and math. Tiers are checked top-down, and the first match wins.</p>
<div class="guide-scroll"><table><thead><tr><th>Status</th><th>Fires when</th></tr></thead><tbody>
<tr><td>__S_Elevated__</td><td>The operating week has all 7 matched volume/hours days, and either pressure reaches the high line in this week and the immediately preceding complete week, or a safety-rate signal is supported by high pressure or 2+ operating families.</td></tr>
<tr><td>__S_Moderate Elevation__</td><td>One high-pressure week; hours below plan with a pressure signal; any safety-rate signal; or operating triggers from 2+ families.</td></tr>
<tr><td>__S_Watch__</td><td>At least one trigger fires, with no higher-tier condition.</td></tr>
<tr><td>__S_Normal__</td><td>No trigger fires and the site has evaluable operating data.</td></tr>
<tr><td>__S_No ops data__</td><td>No trigger fires and operating data is unavailable. This is not a Normal rating.</td></tr>
</tbody></table></div>
<p><b>Families prevent double counting.</b> Pressure, staffing hours, pace and volume above plan are one workload family; volume below plan is disruption; inbound and backlog are flow. Injury-rate categories count as one safety family because cases can overlap. A raw trigger count never causes Elevated. A missing or partial prior week breaks persistence.</p>
<h3>Workload pressure</h3>
<p><b>Pressure (points) = (shipped units ÷ planned units − outbound hours ÷ planned hours) × 100.</b> Units 8% above plan and hours 2% below plan give <b>+10 points</b>. Comparisons require actual values and positive plans; volume and hours must cover the same matched days. A current partial week cannot reach Elevated.</p>
<details><summary>Triggers and thresholds in force</summary>__THRESHOLDS__<p>These values follow the current session. Edited thresholds create a scenario; reset on Site to return to network defaults. Planning and workforce context do not change a dated rating.</p></details>
<details><summary>Safety-rate signals and rate math</summary>
<p><b>Rate = summed events × __BASIS__ ÷ summed hours.</b> Never average site rates. Example: 20 events across 1,000,000 hours gives __RATE_EXAMPLE__ per __BASIS__ hours. Zero events with valid exposure is zero; missing events or exposure is unavailable.</p>
<p>Compare the last 4 full-week window with the site's preceding 13-week baseline. At least 8 complete baseline weeks are required. <b>Expected events = baseline events ÷ baseline hours × recent hours.</b> A zero-event site baseline uses the eligible network baseline rate instead.</p>
<p>A signal needs all three: <b>3+ observed events</b>, <b>observed ≥ 1.25 × expected</b>, and <b>Poisson P(X ≥ observed | expected) &lt; 0.05</b>. This asks whether the count is unusually high for the exposure under the model. The three checks cover recordables + first aid, MSD injuries and MOE incidents. A signal is evidence for review, not proof of a cause. Critical-event counts and iCare flags do not independently override OVSR status.</p></details>
<details><summary>S&amp;OP ramp and recommended actions</summary>
<p><b>Ramp (%) = (larger of the next 2 planned weeks × calibration ÷ average shipped units in the last 4 full weeks − 1) × 100.</b> Calibration aligns planned with shipped units using the latest overlapping full week, bounded from 0.5 to 2.0. This is the latest published plan, not an archived as-of plan, and is context only.</p>
<p>Each fired trigger selects a fixed response-library entry with an owner role, first moves and a numeric evidence line. Safety signals appear before operating signals. “Call first” sorts flagged sites Elevated → Moderate Elevation → Watch, then trigger count. Current workforce indicators and plan ramp support discussion without rescoring history. Accepted actions can record an accountable person, due date, evidence and effectiveness review; the register lasts for the session unless exported.</p></details>
<details><summary>Ops × Safety: relationships, theories and uncertainty</summary><ul>
<li><b>Correlation:</b> r ranges from −1 to +1 and describes direction and strength. The within-site view subtracts each site's own average; Spearman uses ranks. Fisher-z p-values are approximate exploratory screens, not proof of independence or causation.</li>
<li><b>Quarters of a driver:</b> compare pooled event counts ÷ pooled hours in four driver groups, with 95% intervals. Trigger comparisons use fired versus other site-weeks, including a separate next-week view.</li>
<li><b>Evidence ledger:</b> each driver × outcome × timing is a question. H0 says no association after accounting for site and exposure. Poisson regression uses log(hours) as an offset, site effects and annual seasonality, with conservative site/week cluster sensitivity. Effect = (exp(beta) − 1) × 100% per 1 within-site standard deviation. q is the Benjamini–Hochberg adjustment across the tested questions; q &lt; 0.05 is labeled exploratory after correction. Small numbers of sites and correlated tests limit certainty.</li>
<li><b>Mediation:</b> X → M → Y separates a (X → M), b (M → Y holding X fixed), c (total association) and c′ (direct association); indirect association = a × b. Standardized within-site values use a synchronous 4-week block bootstrap for a 95% interval, with 600 draws in the dashboard and 200 in HTML snapshots. This observational decomposition does not establish a mechanism or the effect of changing staffing.</li>
<li><b>Mechanism of injury:</b> source cause codes and narrative keywords group events into 14 mechanisms inside Snowflake. Lift &amp; carry includes lifting, lowering and carrying; reach &amp; repetitive includes overreaching, repetitive motion and bending/twisting. Equipment attribution requires the specified equipment/event language. These are classification rules, not independently verified causes.</li>
<li><b>What's moving injuries:</b> shows qualifying ledger associations alongside the recent 4-week versus prior-13-week comparison. OVSR status still drives actions. A trigger comparison whose 95% interval includes 1 is inconclusive; it does not demonstrate no risk.</li>
<li><b>Next-week checks:</b> rolling-origin models refit before each origin and compare with a flat-rate baseline. Target hours and driver values are observed. These are conditional association checks; they do not establish an issue-time forecast.</li></ul></details>
<details><summary>Measures and formulas</summary><p>Percent shares are fractions × 100. Variance percentages are (actual ÷ reference − 1) × 100. TM means team member; a TM-path is a person and work-path pairing.</p>__DRIVERS__
<p>Overtime comparisons are withheld when event classifications and exposure do not reconcile. Labor-path matching is an attribution proxy. Product mentions and tenure/time-on-task case mix are descriptive unless the matching handling or hours denominators are available.</p></details>
<details><summary>Peak glidepath: projected hours and injuries</summary>
<p><b>Normal hours</b> = median of the site's last 8 full weeks; <b>normal rate</b> = events ÷ hours in its last 13 full weeks. Normal shipped units use the last 8 full weeks. Calibration aligns plan and actual units.</p>
<p><b>Projected hours = normal hours × (planned units × calibration ÷ normal shipped units)<sup>epsilon</sup>.</b> Epsilon is the fitted hours-per-unit elasticity. The average of current and preceding weekly loads feeds <b>risk multiplier = exp(max(b, 0) × max(2-week hours load − 1, 0))</b>. Expected events = projected hours × normal rate × multiplier. A nonpositive fitted slope adds no uplift.</p>
<p>Risk scenario lines are +10% for watch and +25% for action. The 80% band is a <b>conditional process-noise range</b>, not a full forecast interval: plan error, parameter uncertainty and future source revisions are not all included. Archived-plan and out-of-time calibration remain necessary.</p></details>
<details><summary>iCare, case probabilities and year-end projections</summary>
<p>iCare follow-up activity uses the selected contact dates. Open-case inventory uses the latest snapshot across all injury dates. Source follow-up flags are separate from OVSR site status.</p>
<p>The delayed-recordability model is a follow-up aid, using eligible nonrecordable injury cases rather than a verified historical first-aid cohort. <b>Probability of a future event = 1 − product of future survival probabilities.</b> Weekly hazards are prorated for the remaining days, conditional on survival through today, and stop at day 113. A day-112 case has one modeled day left. Current features are held fixed; historical source-state validation remains incomplete.</p>
<p><b>Projected year-end rate = (recorded events + modeled future events + applicable pending-case contribution) × __BASIS__ ÷ (recorded hours + projected remaining hours).</b> The baseline uses the preceding 13 full weeks. An unavailable case model keeps dependent outputs unavailable. Calendar year is Jan 1–Dec 31; fiscal year requires both governed calendar boundaries. Prior-year comparisons require coverage. The process-noise band does not include every source of forecast uncertainty.</p></details>
<details><summary>Data rules, dates and sources</summary><ul>
<li>Weeks run Sunday–Saturday. Correlations leave out weeks with fewer than 2,000 hours. Each page's date caption identifies its effective window; whole-week views can start before the requested date.</li>
<li>The default reporting date screens closed days for hours and site coverage. This is not certification that upstream event counts and hours reconcile. A successful query is not proof of source freshness; event dates, source refresh times and query times differ.</li>
<li>MET is scheduled mandatory extra time from the staffing plan ÷ planned regular hours, using the plan in force for that week. It is not a separate UKG pay code. Plan history starts Sep 13, 2026.</li>
<li>All-cause near misses are a separate measure. Historical Jun–Aug 2025 equipment-detail snapshots are identified separately from live data.</li>
<li>__VIEW_NOTE__</li></ul>__SOURCES__</details>
"""

GUIDE_CSS = """<style>
.ovsr-guide {line-height:1.5;max-width:1100px}
.ovsr-guide .guide-scroll {overflow-x:auto}
.ovsr-guide table {border-collapse:collapse;width:100%;margin:12px 0;font-size:0.94rem}
.ovsr-guide th,.ovsr-guide td {border:1px solid #cbd5e1;padding:9px 12px;text-align:left;vertical-align:top}
.ovsr-guide th {background:#eef3fb;color:#12365b}
.ovsr-guide details {border:1px solid #d5deeb;border-radius:7px;padding:10px 14px;margin:10px 0}
.ovsr-guide summary {font-weight:600;cursor:pointer;color:#12365b}
.ovsr-guide details[open] summary {margin-bottom:10px}
.ovsr-guide li {margin:7px 0}
</style>"""

THRESHOLD_NOTES = {
    "press_watch": "Pressure at or above this line fires the pressure trigger.",
    "press_elev": "High-pressure line used by the status table above.",
    "hrs_short": "Hours variance at or below this value.",
    "hrs_over": "Hours variance at or above this value.",
    "pace": "Average actual TPH versus average planned TPH, at or above this value.",
    "vol_over": "Shipped volume variance at or above this value.",
    "vol_short": "Shipped volume variance at or below this value.",
    "ib_over": "Inbound volume variance at or above this value.",
    "bl_age": "Maximum age during the week, at or above this value.",
    "ramp": "Planning context only; excluded from dated status.",
    "attr": "Current workforce context only; excluded from dated status.",
}

SOURCES = (
    "OBR safety metrics daily + PLAT (EDLDB.SNOP_SANDBOX)",
    "HOLIDAY_FC_METRICS (operating actuals versus plan)",
    "S&OP outbound forecast, latest publication",
    "CLMS labor (T_BRONZE_CLMS_BY_ENTRY_DAY)",
    "UKG timecard totals and employees",
    "Incident detail (T_EHS_INCIDENT_DETAILS)",
    "PUMA worked-not-trained; attrition; UKG roster; iCare",
    "Network staffing plan (STAFF_DAILY_DEPARTMENT) for scheduled MET",
)

MEASURE_FORMULAS = {
    "NEW90": "Hourly team members under 90 days of tenure ÷ hourly headcount in the Sunday roster snapshot × 100.",
    "ATTR": "Terminations ÷ active team members that week × 100.",
    "STRAIN_PCT": "Direct hours on high-strain paths ÷ all direct hours × 100. High-strain paths include truck/line loading, container unloading, multis ground and pallet picking.",
    "OVER_UTIL": "High-strain TM-paths above 106 hours in the last 4 weeks ÷ all high-strain TM-paths × 100.",
    "PATHS_TM": "Sum of distinct direct paths worked per TM ÷ number of TMs with direct hours.",
    "SINGLE_PATH": "TMs with 30+ direct hours and one direct path ÷ all TMs with 30+ direct hours × 100.",
    "OB_UPH_PUSH": "(Outbound UPH ÷ the site's prior-13-week average UPH − 1) × 100.",
}


def render_guide(thresholds, specs, drivers, colors, *, basis=200000, snapshot=False):
    """Render the same guide with current threshold values for either viewing surface."""
    rows = []
    for key, (label, default, step, help_text) in specs.items():
        role = "Context" if key in ("ramp", "attr") else "Scoring"
        rows.append("<tr><td>" + escape(label) + "</td><td>" + escape(f"{thresholds[key]:.1f}")
                    + "</td><td>" + role + "</td><td>" + escape(THRESHOLD_NOTES.get(key, help_text)) + "</td></tr>")
    table = ("<div class='guide-scroll'><table><thead><tr><th>Trigger / context</th><th>In force</th>"
             "<th>Use</th><th>Rule</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")
    measures = "<ul>" + "".join("<li><b>" + escape(v[0]) + "</b> (" + escape(v[1]) + "): "
                                + escape(MEASURE_FORMULAS.get(key, v[4])) + "</li>" for key, v in drivers.items()) + "</ul>"
    sources = "<h3>Sources</h3><ul>" + "".join("<li>" + escape(s) + "</li>" for s in SOURCES) + "</ul>"
    view_note = ("This HTML is a fixed snapshot of the numbers at generation time and does not update. "
                 "No names, usernames, employee IDs or narratives are included in the snapshot."
                 if snapshot else
                 "The dashboard queries Snowflake and caches results. Refresh data re-queries; exports remain fixed copies. "
                 "Aggregate views are the default; individual case views require an administrator-approved role.")
    out = GUIDE_HTML.replace("__THRESHOLDS__", table).replace("__DRIVERS__", measures)
    out = out.replace("__BASIS__", f"{basis:,.0f}").replace("__RATE_EXAMPLE__", f"{20 * basis / 1000000:g}")
    out = out.replace("__VIEW_NOTE__", view_note).replace("__SOURCES__", sources)
    for status, color in colors.items():
        chip = ("<span style='font-weight:700;color:#172b4d'><span style='color:"
                + escape(color, quote=True) + "'>●</span> " + escape(status) + "</span>")
        out = out.replace("__S_" + status + "__", chip)
    return GUIDE_CSS + "<div class='ovsr-guide'>" + out + "</div>"
