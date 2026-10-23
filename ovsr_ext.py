"""OVSR extension: ops x labor x safety panel, correlations, Ops x Safety tab and the shareable site snapshot.

Loaded by streamlit_app.py with `import ovsr_ext` and `ovsr_ext.bind(globals())`. Everything here is read-only and
aggregates inside Snowflake: no names, usernames or employee IDs reach the app or the snapshot file.
"""
import base64
import gzip
import html
import json
import math
import re
from contextvars import ContextVar

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st
import ovsr_governance as gov
import ovsr_guide

_NS = ContextVar("ovsr_app_namespace", default=None)


def bind(ns):
    """Bind the running script context without sharing filters or loaders across sessions."""
    _NS.set(ns)


def _namespace():
    ns = _NS.get()
    if ns is None:
        raise RuntimeError("OVSR extension is not bound to this script context.")
    return ns


def g(name, default=None):
    return _namespace().get(name, default)


# ---------------------------------------------------------------------------------------------------------------
# SQL (Phase 1 sources). All readable by SNOP_USER; all aggregated before leaving Snowflake.
# ---------------------------------------------------------------------------------------------------------------
X_SRC = {
    "clms": "EDLDB.SNOP_SANDBOX.T_BRONZE_CLMS_BY_ENTRY_DAY",
    "ukg_pay": "EDLDB.CHEWY_CERTIFIED.FCT_UKG_TIMECARD_TOTAL",
    "ukg_emp": "EDLDB.CHEWY_CERTIFIED.DIM_UKG_EMPLOYEE",
    "inc": "EDLDB.SNOP_SANDBOX.T_EHS_INCIDENT_DETAILS",
    "wnt": "EDLDB.PUMA.WORKED_NOT_TRAINED_SUMMARY",
    "holiday": "EDLDB.SNOP_SANDBOX.HOLIDAY_FC_METRICS",
    "met_plan": "EDLDB.SC_ORDER_ROUTING_TEAM_SANDBOX.STAFF_DAILY_DEPARTMENT",
}
# High-strain paths: Rotation Tool lift level 3-4 plus the Operation Strain Salvation rotation boards.
STRAIN_PATHS = ["%truck load%", "%line load%", "%container unload%", "%multis ground pick%", "%pallet pick%"]
OVER_UTIL_HRS = 106          # Rotation Tool: more than 106 hrs in one path in ~30 days = over-utilized
_STRAIN_SQL = ", ".join(f"'{p}'" for p in STRAIN_PATHS)

SQL_CLMS_WEEK = f"""
WITH d AS (
  SELECT WH_ID AS SITE,
         DATEADD(day, -(DAYOFWEEKISO(TIME_SEGMENT::DATE) % 7), TIME_SEGMENT::DATE)::DATE AS WK,
         CASE WHEN LABOR_DEPARTMENT ILIKE 'outbound%' THEN 'Outbound' WHEN LABOR_DEPARTMENT ILIKE 'inbound%' THEN 'Inbound'
              WHEN LABOR_DEPARTMENT ILIKE 'replen%' THEN 'Replenishment' WHEN LABOR_DEPARTMENT ILIKE 'inventory%' THEN 'Inventory Control'
              ELSE 'Other' END AS DEPT,
         LABOR_FUNCTION AS FUNC, COALESCE(IS_DIRECT, FALSE) AS DIR, EMPLOYEE_ID AS E,
         TIME_IN_SECONDS / 3600.0 AS H, COALESCE(UNITS, 0) AS U, COALESCE(TRANSACTIONS, 0) AS T,
         LABOR_FUNCTION ILIKE ANY ({_STRAIN_SQL}) AS STRAIN
  FROM {X_SRC['clms']}
  WHERE TIME_SEGMENT >= '2025-05-01' AND TIME_IN_SECONDS > 0 AND WH_ID IS NOT NULL),
tp AS (SELECT SITE, WK, DEPT, FUNC, E, DIR, STRAIN, SUM(H) AS H FROM d GROUP BY 1, 2, 3, 4, 5, 6, 7),
roll AS (
  SELECT a.SITE, a.WK, a.DEPT, a.E, a.FUNC, a.H, SUM(b.H) AS H4
  FROM tp a JOIN tp b ON b.SITE = a.SITE AND b.E = a.E AND b.FUNC = a.FUNC AND b.DEPT = a.DEPT
   AND b.WK BETWEEN DATEADD(day, -21, a.WK) AND a.WK
  WHERE a.STRAIN AND a.DIR
  GROUP BY 1, 2, 3, 4, 5, 6),
tw AS (SELECT SITE, WK, DEPT, E, COUNT(DISTINCT IFF(DIR, FUNC, NULL)) AS PATHS, SUM(IFF(DIR, H, 0)) AS DH
       FROM tp GROUP BY 1, 2, 3, 4),
agg AS (SELECT SITE, WK, DEPT, SUM(H) AS HRS, SUM(IFF(DIR, H, 0)) AS DIRECT_HRS, SUM(U) AS UNITS,
               SUM(IFF(DIR, T, 0)) AS TRANS, SUM(IFF(STRAIN AND DIR, H, 0)) AS STRAIN_HRS
        FROM d GROUP BY 1, 2, 3),
rot AS (SELECT SITE, WK, DEPT, COUNT(*) AS STRAIN_TM_PATHS, COUNT_IF(H4 > {OVER_UTIL_HRS}) AS OVER_TM_PATHS
        FROM roll GROUP BY 1, 2, 3),
tms AS (SELECT SITE, WK, DEPT, COUNT_IF(DH > 0) AS TMS, SUM(IFF(DH > 0, PATHS, 0)) AS TM_PATH_SUM,
               COUNT_IF(DH >= 30 AND PATHS = 1) AS SINGLE_PATH_TMS, COUNT_IF(DH >= 30) AS FULL_WEEK_TMS
        FROM tw GROUP BY 1, 2, 3)
SELECT agg.*, COALESCE(rot.STRAIN_TM_PATHS, 0) AS STRAIN_TM_PATHS, COALESCE(rot.OVER_TM_PATHS, 0) AS OVER_TM_PATHS,
       tms.TMS, tms.TM_PATH_SUM, tms.SINGLE_PATH_TMS, tms.FULL_WEEK_TMS
FROM agg
LEFT JOIN rot ON rot.SITE = agg.SITE AND rot.WK = agg.WK AND rot.DEPT = agg.DEPT
LEFT JOIN tms ON tms.SITE = agg.SITE AND tms.WK = agg.WK AND tms.DEPT = agg.DEPT
WHERE agg.WK >= '2025-06-01'
"""

SQL_PAY_WEEK = f"""
WITH e AS (
  SELECT PERSON_ID, REGEXP_SUBSTR(PRIMARY_ORG_PATH_TXT, '- ([A-Z]{{3}}[0-9])/', 1, 1, 'e') AS SITE
  FROM {X_SRC['ukg_emp']}
  QUALIFY ROW_NUMBER() OVER (PARTITION BY PERSON_ID ORDER BY VALID_FROM_TS DESC NULLS LAST) = 1)
SELECT e.SITE, DATEADD(day, -(DAYOFWEEKISO(t.PARTITION_DT) % 7), t.PARTITION_DT)::DATE AS WK,
       SUM(IFF(t.PAY_CODE = 'Regular', t.HOURS_AMT, 0)) AS REG_H,
       SUM(IFF(t.PAY_CODE = 'Overtime', t.HOURS_AMT, 0)) AS OT_H,
       SUM(IFF(t.PAY_CODE = 'Voluntary Extra Time', t.HOURS_AMT, 0)) AS VET_H,
       SUM(IFF(t.PAY_CODE = 'Voluntary Time Off', t.HOURS_AMT, 0)) AS VTO_H
FROM {X_SRC['ukg_pay']} t
JOIN e ON e.PERSON_ID = t.PERSON_ID
WHERE t.PARTITION_DT >= '2025-06-01' AND t.PARTITION_DT < CURRENT_DATE()
  AND COALESCE(t.IS_CURRENT, TRUE) AND NOT COALESCE(t.IS_DELETED, FALSE)
  AND t.PAY_CODE IN ('Regular', 'Overtime', 'Voluntary Extra Time', 'Voluntary Time Off')
  AND e.SITE IS NOT NULL
GROUP BY 1, 2
"""

# MET (mandatory extra time) from the network staffing plan: UKG has no MET pay code (MET hours land in Overtime).
# Each week uses the plan in force for it: the last upload of the latest weekly model run (MODEL_RUN_WEEK) on or
# before that week that covers the day. Forward weeks come from the latest run. Plan history starts Sep 13, 2026.
SQL_MET_WEEK = f"""
WITH r AS (
  SELECT UPPER(TRIM(FC_NAME)) AS SITE, TRY_TO_DATE(MODEL_RUN_WEEK) AS RUN, RUN_ID, MAX(UPLOAD_DATETIME) AS UP
  FROM {X_SRC['met_plan']}
  WHERE TRY_TO_DATE(MODEL_RUN_WEEK) IS NOT NULL
  GROUP BY 1, 2, 3
  QUALIFY ROW_NUMBER() OVER (PARTITION BY SITE, RUN ORDER BY UP DESC, RUN_ID DESC) = 1),
d AS (
  SELECT r.SITE, r.RUN, TRY_TO_DATE(s.DATE) AS D, s.DEPARTMENT AS DEPT, COALESCE(s.MET, 0) AS MET,
         COALESCE(s.REGULAR_HOURS, 0) AS REG
  FROM {X_SRC['met_plan']} s
  JOIN r ON r.SITE = UPPER(TRIM(s.FC_NAME)) AND r.RUN_ID = s.RUN_ID AND r.RUN = TRY_TO_DATE(s.MODEL_RUN_WEEK)
  WHERE TRY_TO_DATE(s.DATE) IS NOT NULL),
w AS (SELECT d.*, DATEADD(day, -(DAYOFWEEKISO(D) % 7), D)::DATE AS WK FROM d),
k AS (SELECT * FROM w WHERE RUN <= WK QUALIFY DENSE_RANK() OVER (PARTITION BY SITE, D ORDER BY RUN DESC) = 1)
SELECT SITE, WK, MAX(RUN) AS RUN, COUNT(DISTINCT D) AS MET_DAYS, SUM(MET) AS MET_H,
       SUM(IFF(DEPT = 'OB', MET, 0)) AS MET_OB_H, SUM(REG) AS PLAN_REG_H
FROM k GROUP BY 1, 2
"""

SQL_HIRES_WEEK = f"""
WITH e AS (
  SELECT PERSON_ID, HIRE_DT, PRIMARY_ORG_PATH_TXT AS P, IS_MANAGER
  FROM {X_SRC['ukg_emp']}
  QUALIFY ROW_NUMBER() OVER (PARTITION BY PERSON_ID ORDER BY VALID_FROM_TS DESC NULLS LAST) = 1)
SELECT REGEXP_SUBSTR(P, '- ([A-Z]{{3}}[0-9])/', 1, 1, 'e') AS SITE,
       DATEADD(day, -(DAYOFWEEKISO(HIRE_DT) % 7), HIRE_DT)::DATE AS WK, COUNT(*) AS NEW_HIRES
FROM e
WHERE HIRE_DT >= '2025-06-01' AND P ILIKE '%Fulfillment Center Function%' AND NOT COALESCE(IS_MANAGER, FALSE)
GROUP BY 1, 2
HAVING SITE IS NOT NULL
"""

_B = lambda c: f"UPPER({c}::STRING) IN ('TRUE', 'YES', '1', 'Y')"   # noqa: E731
# Mechanism of injury: Intelex's cause field, split and re-sorted with keywords read inside Snowflake (the text never
# leaves it). Lifting/lowering vs carrying and overreaching vs bending come from the event description and activity.
MECH_ORDER = ["Lifting / lowering", "Carrying", "Overreaching", "Repetitive motion", "Bending / twisting / posture",
              "Pushing / pulling", "Struck by / against", "Caught in / between", "Slip, trip or fall", "Cut or sharp object",
              "PIT or equipment", "Chemical, heat or electrical", "Animal", "Other or not recorded"]
MECH_GROUPS = {"INJ_LIFT": ["Lifting / lowering", "Carrying"],
               "INJ_REACH": ["Overreaching", "Repetitive motion", "Bending / twisting / posture"]}
SEVERITY_ORDER = ["Lost time", "Recordable", "First aid", "Report only", "Not recorded"]
# Report text for the mechanism keywords (read only inside Snowflake). Equipment names are swapped for one marker
# word ("pitequip") first, so "forklift" doesn't read as lifting or "reach truck" as reaching, and the PIT rule below
# can find the equipment.
_TXT_SQL = ("REGEXP_REPLACE(REGEXP_REPLACE(LOWER(COALESCE(EVENT_NARRATIVE, '') || ' ' || COALESCE(ACTIVITY_BEFORE_INCIDENT, '') "
            "|| ' ' || COALESCE(SUSPECTED_CAUSE, '')), "
            "'(fork ?-?lifts?|reach ?-?trucks?|scissor ?-?lifts?|lift ?-?trucks?|lift ?-?gates?|man ?-?lifts?|"
            "boom ?-?lifts?|pallet ?-?lifts?|order ?-?pickers?|cherry ?-?pickers?|(electric |power )?pallet ?-?jacks?|"
            "tuggers?|walkie ?-?stackers?|swing ?-?reach(es)?|turret ?-?trucks?)', ' pitequip '), "
            "'(^|[^a-z])(pits?|p[.]i[.]t[.]?)([^a-z]|$)', ' pitequip ')")
# PIT or equipment from the text (only reached when Intelex's cause is blank or not one mapped above): the text names
# equipment AND, in the same sentence, says it struck / hit / pinned / ran over / backed into / collided with someone
# or tipped over, or that the TM fell off it or was operating or driving it.
_PIT_RX = ("pitequip[^.]{0,60}[^a-z](struck|strikes?|striking|hit|hits|hitting|pinned|pinning|pins|ran over|runs? over|"
           "running over|backed into|backing into|backs into|collided|collides|colliding|collision|tipped|tipping|tips? over)"
           "([^a-z]|$)"
           "|(^|[^a-z])(struck|hit|pinned|ran over|run over|backed into|collided)[^a-z.][^.]{0,40}pitequip"
           "|(^|[^a-z])(operat(e|es|ed|ing|or|ors)|driv(e|es|en|ing)|drove|fell off|falls? off|falling off)[^a-z.][^.]{0,25}"
           "pitequip"
           "|pitequip {0,3}(operators?|drivers?)([^a-z]|$)")
_MECH_SQL = """
CASE
  WHEN CAUSE_OF_INJURY ILIKE 'Lowering/Lifting/Carrying%' THEN IFF(TXT ILIKE ANY ('%carry%', '%carrie%'), 'Carrying', 'Lifting / lowering')
  WHEN CAUSE_OF_INJURY ILIKE 'Body Position%' THEN IFF(TXT ILIKE ANY ('%reach%', '%overhead%', '%over head%', '%stretch%'), 'Overreaching', 'Bending / twisting / posture')
  WHEN CAUSE_OF_INJURY ILIKE 'Repetitive%' THEN 'Repetitive motion'
  WHEN CAUSE_OF_INJURY ILIKE 'Forceful Exertion%' THEN 'Pushing / pulling'
  WHEN CAUSE_OF_INJURY ILIKE ANY ('Struck%', 'Falling Product%', 'Stepped On%') THEN 'Struck by / against'
  WHEN CAUSE_OF_INJURY ILIKE 'Caught%' THEN 'Caught in / between'
  WHEN CAUSE_OF_INJURY ILIKE ANY ('Slip%', 'Falls%', 'Climbing%', 'Environmental Hazard%') THEN 'Slip, trip or fall'
  WHEN CAUSE_OF_INJURY ILIKE ANY ('%Sharp Object%', 'Rubbed%') THEN 'Cut or sharp object'
  WHEN CAUSE_OF_INJURY ILIKE ANY ('Interaction with PIT%', 'Moving Violation%', 'Equipment Malfunction%', 'Interaction with Robotic%') THEN 'PIT or equipment'
  WHEN CAUSE_OF_INJURY ILIKE ANY ('Contact with Substance%', 'Contact with Chemicals%', 'Chemical Exposure%', 'Inhalation%', 'Burn%', 'Contact with Electrical%') THEN 'Chemical, heat or electrical'
  WHEN CAUSE_OF_INJURY ILIKE ANY ('Animal%', 'Scratch (from animal)%') THEN 'Animal'
  WHEN REGEXP_INSTR(TXT, '__PIT_RX__') > 0 THEN 'PIT or equipment'
  WHEN TXT ILIKE ANY ('%reach%', '%overhead%', '%over head%') THEN 'Overreaching'
  WHEN TXT ILIKE ANY ('%carry%', '%carrie%') THEN 'Carrying'
  WHEN TXT ILIKE ANY ('%lift%', '%lower%', '%pick up%', '%picked up%', '%picking up%') THEN 'Lifting / lowering'
  WHEN TXT ILIKE ANY ('%repetit%', '%repeated%') THEN 'Repetitive motion'
  WHEN TXT ILIKE ANY ('%push%', '%pull%') THEN 'Pushing / pulling'
  WHEN TXT ILIKE ANY ('%twist%', '%bend%', '%bent%', '%turned%', '%squat%') THEN 'Bending / twisting / posture'
  WHEN TXT ILIKE ANY ('%slip%', '%trip%', '%fell%', '% fall%') THEN 'Slip, trip or fall'
  WHEN TXT ILIKE ANY ('%struck%', '% hit %', '%bump%', '%fell on%') THEN 'Struck by / against'
  WHEN TXT ILIKE ANY ('% cut %', '%lacerat%', '%box cutter%', '%blade%') THEN 'Cut or sharp object'
  ELSE 'Other or not recorded' END""".replace("__PIT_RX__", _PIT_RX)

SQL_INJ_PROFILE = f"""
WITH b AS (
  SELECT *, {_TXT_SQL} AS TXT,
         TRY_TO_TIMESTAMP(EVENT_DATE_TIME) AS ETS
  FROM {X_SRC['inc']}
  WHERE INCIDENT_TYPE ILIKE 'injury%' AND EVENT_DATE >= '2025-06-01'
    AND COALESCE(INJURY_CLASSIFICATION, '') NOT ILIKE 'non-occupational%')
SELECT COALESCE(REGEXP_SUBSTR(UPPER(LOCATION), '[A-Z]{{3}}[0-9]'), TRIM(LOCATION)) AS SITE,
       DATEADD(day, -(DAYOFWEEKISO(EVENT_DATE::DATE) % 7), EVENT_DATE::DATE)::DATE AS WK,
       COALESCE(NULLIF(TRIM(EVENT_DEPARTMENT), ''), 'Not recorded') AS DEPT,
       COALESCE(NULLIF(TRIM(POSITION), ''), 'Not recorded') AS ROLE,
       COALESCE(NULLIF(TRIM(TYPE_OF_INJURY), ''), 'Not recorded') AS INJURY_TYPE,
       COALESCE(NULLIF(TRIM(CAUSE_OF_INJURY), ''), 'Not recorded') AS CAUSE,
       CASE WHEN DATE_HIRED IS NULL OR DATE_HIRED > EVENT_DATE THEN 'Not recorded'
            WHEN DATEDIFF(day, DATE_HIRED, EVENT_DATE) < 30 THEN 'Under 30 days'
            WHEN DATEDIFF(day, DATE_HIRED, EVENT_DATE) < 90 THEN '30-89 days'
            WHEN DATEDIFF(day, DATE_HIRED, EVENT_DATE) < 365 THEN '90 days - 1 year'
            ELSE '1 year +' END AS TENURE,
       CASE WHEN WORKING_OT_HOURS ILIKE '%MET%' THEN 'MET' WHEN WORKING_OT_HOURS ILIKE '%VET%' THEN 'VET'
            WHEN WORKING_OT_HOURS ILIKE 'no%' THEN 'Regular time' ELSE 'Not recorded' END AS EXTRA_TIME,
       CASE WHEN TRY_TO_NUMBER(HOURS_IN_PATH::STRING) IS NULL OR TRY_TO_NUMBER(HOURS_IN_PATH::STRING) < 0 THEN 'Not recorded'
            WHEN TRY_TO_NUMBER(HOURS_IN_PATH::STRING) < 2 THEN '0-1 h'
            WHEN TRY_TO_NUMBER(HOURS_IN_PATH::STRING) < 4 THEN '2-3 h'
            WHEN TRY_TO_NUMBER(HOURS_IN_PATH::STRING) < 6 THEN '4-5 h'
            WHEN TRY_TO_NUMBER(HOURS_IN_PATH::STRING) < 8 THEN '6-7 h' ELSE '8 h +' END AS HOURS_IN_PATH,
       IFF({_B('TM_JOB_PATH')}, 'In job path', IFF(TM_JOB_PATH IS NULL, 'Not recorded', 'Outside job path')) AS IN_PATH,
       CASE WHEN TRY_TO_BOOLEAN(TM_LABOR_SHARED::STRING) THEN 'Labor-shared' WHEN TRY_TO_BOOLEAN(TM_LABOR_SHARED::STRING) = FALSE THEN 'Home department' ELSE 'Not recorded' END AS SHARED,
       CASE WHEN TRY_TO_BOOLEAN(CHEWY_PRODUCT_INVOLVED::STRING) THEN 'Product involved' WHEN TRY_TO_BOOLEAN(CHEWY_PRODUCT_INVOLVED::STRING) = FALSE THEN 'No product' ELSE 'Not recorded' END AS PRODUCT,
       {_MECH_SQL} AS MECH,
       IFF(ETS IS NULL OR ETS::TIME = '00:00:00'::TIME, -1, HOUR(ETS)) AS EVENT_HOUR,
       DAYOFWEEKISO(EVENT_DATE::DATE) AS DOW,
       CASE WHEN INJURY_CLASSIFICATION ILIKE 'lost time%' OR OSHA_OUTCOME ILIKE 'days away%' THEN 'Lost time'
            WHEN INJURY_CLASSIFICATION ILIKE 'recordable%' OR {_B('OSHA_RECORDABLE')} THEN 'Recordable'
            WHEN INJURY_CLASSIFICATION ILIKE 'first aid%' THEN 'First aid'
            WHEN INJURY_CLASSIFICATION ILIKE 'report only%' THEN 'Report only' ELSE 'Not recorded' END AS SEVERITY,
       COALESCE(NULLIF(TRIM(EVENT_AREA), ''), 'Not recorded') AS AREA,
       COUNT(*) AS INJURIES,
       COUNT_IF({_B('OSHA_RECORDABLE')} OR INJURY_CLASSIFICATION ILIKE ANY ('recordable%', 'lost time%')) AS RECORDABLES,
       COUNT_IF(INJURY_CLASSIFICATION ILIKE 'first aid%') AS FIRST_AID,
       COUNT_IF({_B('MSD_INJURY')}) AS MSD,
       COUNT_IF({_B('MOE_FLAG')}) AS MOE
FROM b
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17
"""

SQL_WNT_ALL = f"""
WITH s AS (
  SELECT WAREHOUSE AS SITE, SEGMENT_START::DATE AS D, TRAINING_TITLE_SNAPSHOT AS TRAINING,
         SECONDS / 3600 AS H, EMPLOYEE_USERNAME AS U
  FROM {X_SRC['wnt']}
  WHERE SEGMENT_START >= DATEADD(day, -56, CURRENT_DATE())
    AND NOT COALESCE(LOWER(REPLICATION_DELETED_FLAG::STRING), 'false') IN ('true', '1', 'y', 'yes'))
SELECT 'WEEK' AS LVL, SITE, DATEADD(day, -(DAYOFWEEKISO(D) % 7), D)::DATE AS WK, NULL::STRING AS TRAINING,
       SUM(H) AS HOURS, COUNT(DISTINCT U) AS PEOPLE
FROM s GROUP BY SITE, WK
UNION ALL
SELECT 'L28', SITE, NULL::DATE, TRAINING, SUM(H), COUNT(DISTINCT U)
FROM s WHERE D >= DATEADD(day, -28, CURRENT_DATE()) GROUP BY SITE, TRAINING
"""

SQL_SHIP_WEEK = f"""
SELECT WH_ID AS SITE, DATEADD(day, -(DAYOFWEEKISO(DATE) % 7), DATE)::DATE AS WK,
       SUM(TOTAL_SHIPPED_CONTAINERS) AS CONTAINERS, COUNT(TOTAL_SHIPPED_CONTAINERS) AS CONT_DAYS
FROM {X_SRC['holiday']}
WHERE WH_ID IS NOT NULL AND WH_ID <> 'Network' AND DATE < CURRENT_DATE()
GROUP BY 1, 2
"""

X_QUERIES = {"x_clms": SQL_CLMS_WEEK, "x_pay": SQL_PAY_WEEK, "x_hires": SQL_HIRES_WEEK, "x_inj": SQL_INJ_PROFILE,
             "x_wnt_all": SQL_WNT_ALL, "x_ship": SQL_SHIP_WEEK, "x_met": SQL_MET_WEEK}


def _clean(df, dates=("WK", "RUN"), text=()):
    df = df.copy()
    for c in dates:
        if c in df.columns:
            s = pd.to_datetime(df[c], errors="coerce")
            if getattr(s.dt, "tz", None) is not None:
                s = s.dt.tz_localize(None)
            df[c] = s.dt.normalize()
    for c in df.columns:
        if c in dates or c in text or c in ("SITE", "DEPT", "LVL", "TRAINING", "ROLE", "INJURY_TYPE", "CAUSE", "TENURE",
                                            "EXTRA_TIME", "HOURS_IN_PATH", "IN_PATH", "SHARED", "PRODUCT", "MECH",
                                            "SEVERITY", "AREA"):
            continue
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if "SITE" in df.columns:
        df["SITE"] = df["SITE"].astype("string").str.upper().str.strip()
    return df


def load_x(key):
    """(DataFrame, error) for one Phase 1 source, via the app's cached run_sql (demo-aware)."""
    df, err = g("run_sql")(key, X_QUERIES[key])
    if df is None:
        return None, err
    return _clean(df, dates=("WK", "RUN") + tuple(X_DATES.get(key, ())), text=X_TEXT), None


# ---------------------------------------------------------------------------------------------------------------
# Site-week panel: ops + productivity + labor + rotation + safety, one row per full site-week
# ---------------------------------------------------------------------------------------------------------------
# key: (label, group, unit, format, note)
DRIVERS = {
    "PRESSURE": ("Workload pressure", "Ops vs plan", "pts", "+.1f", "Volume % over plan minus hours % over plan."),
    "VOL_PCT": ("Volume vs plan", "Ops vs plan", "%", "+.1f", "Outbound shipped units ÷ plan − 1."),
    "HRS_PCT": ("Hours vs plan", "Ops vs plan", "%", "+.1f", "Outbound hours ÷ plan − 1."),
    "TPH_PCT": ("Pace vs planned TPH", "Ops vs plan", "%", "+.1f", "Average TPH ÷ planned TPH − 1."),
    "IB_PCT": ("Inbound vs plan", "Ops vs plan", "%", "+.1f", "Inbound units ÷ plan − 1."),
    "BL_AGE": ("Oldest backlog container", "Ops vs plan", "h", ".0f", "Oldest outbound container in WMS backlog that week."),
    "HRS_LOAD": ("Hours load vs site normal", "Ops vs plan", "×", ".2f", "Total hours ÷ the site's average of its prior 13 full weeks."),
    "OB_UPH": ("Outbound UPH", "Productivity", "", ".0f", "Outbound units ÷ outbound direct hours (CLMS)."),
    "OB_TPH": ("Outbound TPH", "Productivity", "", ".1f", "Outbound transactions ÷ outbound direct hours (CLMS)."),
    "OB_TLPH": ("Outbound TLPH", "Productivity", "", ".0f", "Outbound units ÷ all outbound labor hours, direct + indirect (CLMS)."),
    "OB_CPLH": ("Outbound CPLH", "Productivity", "", ".1f", "Shipped containers ÷ all outbound labor hours."),
    "IB_UPH": ("Inbound UPH", "Productivity", "", ".0f", "Inbound units ÷ inbound direct hours (CLMS)."),
    "IB_TPH": ("Inbound TPH", "Productivity", "", ".1f", "Inbound transactions ÷ inbound direct hours (CLMS)."),
    "IB_TLPH": ("Inbound TLPH", "Productivity", "", ".0f", "Inbound units ÷ all inbound labor hours, direct + indirect (CLMS)."),
    "IB_CPLH": ("Inbound CPLH", "Productivity", "", ".1f", "Inbound transactions (case-level receipts and stows) ÷ all inbound labor hours."),
    "IB_UPT": ("Inbound units per transaction", "Productivity", "", ".1f", "Inbound units ÷ inbound transactions (CLMS): how much each receive or stow touch handles (the Inbound Metric Dash's UPT)."),
    "OB_UPH_PUSH": ("Outbound rate push", "Productivity", "%", "+.1f", "Outbound UPH vs the site's prior-13-week average."),
    "OT_PCT": ("Overtime share", "Labor", "%", ".1f", "UKG Overtime hours ÷ Regular hours."),
    "MET_PCT": ("MET share", "Labor", "%", ".2f", "MET (mandatory extra time) scheduled in the network staffing plan ÷ planned regular hours, plan in force that week (STAFF_DAILY_DEPARTMENT, from Sep 13, 2026)."),
    "VET_PCT": ("VET share", "Labor", "%", ".2f", "UKG 'Voluntary Extra Time' hours ÷ Regular hours."),
    "VTO_PCT": ("VTO share", "Labor", "%", ".1f", "UKG 'Voluntary Time Off' hours ÷ Regular hours."),
    "HIRES_100": ("New hires per 100 TMs", "Labor", "", ".1f", "UKG hourly hires that week ÷ direct TMs in CLMS × 100."),
    "NEW90": ("Under-90-day share", "Labor", "%", ".1f", "UKG roster Sunday snapshot (loaded with workforce signals)."),
    "ATTR": ("Weekly attrition", "Labor", "%", ".2f", "Terminations ÷ active TMs that week (loaded with workforce signals)."),
    "STRAIN_PCT": ("High-strain path hours", "Rotation", "%", ".1f", "Direct hours in truck/line loading, container unload, multis ground and pallet picking."),
    "OVER_UTIL": ("Over-utilized TM-paths", "Rotation", "%", ".1f", f"High-strain TM-paths with more than {OVER_UTIL_HRS} hrs in the last 4 weeks."),
    "PATHS_TM": ("Paths per TM", "Rotation", "", ".2f", "Average distinct direct paths each TM worked that week."),
    "SINGLE_PATH": ("Single-path TMs", "Rotation", "%", ".1f", "TMs with 30+ direct hours that week, all in one path."),
}
DRIVER_GROUPS = ["Ops vs plan", "Productivity", "Labor", "Rotation"]
OUTCOMES = {
    "INJ_RATE": ("Injuries (recordable + first aid)", ["RECORDABLE_INCIDENTS", "FIRST_AID_INCIDENT"]),
    "TRIR": ("Recordable rate", ["RECORDABLE_INCIDENTS"]),
    "FAR": ("First-aid rate", ["FIRST_AID_INCIDENT"]),
    "MSDR": ("MSD rate", ["MSD_INJURIES"]),
    "MOER": ("MOE rate", ["MOE_INCIDENTS"]),
    "LIFTR": ("Lift & carry injury rate", ["INJ_LIFT"]),
    "REACHR": ("Reach, repetition & posture injury rate", ["INJ_REACH"]),
}
OUT_SHORT = {"INJ_RATE": "Injuries (rec + FA)", "TRIR": "Recordable", "FAR": "First aid", "MSDR": "MSD", "MOER": "MOE",
             "LIFTR": "Lift & carry", "REACHR": "Reach & repetitive"}
MIN_WEEK_HOURS = 2000
MIN_FIRED = 4          # FC-weeks a trigger must fire before its rate ratio is shown


def _prior_mean(s, n=13, minp=6):
    return s.shift(1).rolling(n, min_periods=minp).mean()


def build_panel(dfact, hol, wf=None, basis=200_000):
    """One row per full site-week (Sunday start). Counts and hours stay additive so any scope can be re-rated."""
    sw = g("site_weeks")(dfact)
    sw = sw[sw["DAYS"] == 7].copy()
    ow = g("ops_weeks")(hol) if hol is not None else pd.DataFrame(columns=["SITE", "WK"])
    keep_o = [c for c in ["SITE", "WK", "UNITS", "UNITS_PLAN", "HRS", "HRS_PLAN", "TPH", "TPH_PLAN", "IB", "IB_PLAN",
                          "BL_AGE", "PRESS_DAYS", "DAYS", "VOL_PCT", "HRS_PCT", "TPH_PCT", "IB_PCT", "PRESSURE"] if c in ow.columns]
    o = ow[keep_o].rename(columns={"UNITS": "OB_UNITS_ACT", "UNITS_PLAN": "OB_UNITS_PLAN", "HRS": "OB_HRS_ACT",
                                   "HRS_PLAN": "OB_HRS_PLAN", "DAYS": "OPS_DAYS", "IB": "IB_UNITS_ACT"})
    p = sw.merge(o, on=["SITE", "WK"], how="left")
    p["OPS_FULL"] = p.get("PRESS_DAYS", pd.Series(0, index=p.index)).fillna(0) >= 7

    notes = {}
    clms, e = load_x("x_clms")
    notes["clms"] = e
    if clms is not None and len(clms):
        piv = clms.pivot_table(index=["SITE", "WK"], columns="DEPT",
                               values=["HRS", "DIRECT_HRS", "UNITS", "TRANS"], aggfunc="sum")
        abbr = {"Outbound": "OB", "Inbound": "IB", "Replenishment": "RP", "Inventory Control": "IC", "Other": "OT"}
        piv.columns = [f"{abbr.get(d, 'OT')}_{m}" for m, d in piv.columns]
        tot = clms.groupby(["SITE", "WK"])[["HRS", "DIRECT_HRS", "STRAIN_HRS", "STRAIN_TM_PATHS", "OVER_TM_PATHS", "TMS",
                                             "TM_PATH_SUM", "SINGLE_PATH_TMS", "FULL_WEEK_TMS"]].sum()
        tot.columns = ["CLMS_" + c for c in tot.columns]
        c2 = tot.join(piv, how="left").reset_index()
        p = p.merge(c2, on=["SITE", "WK"], how="left")
        dh = clms.groupby(["SITE", "WK", "DEPT"], as_index=False)["HRS"].sum()
    else:
        dh = pd.DataFrame(columns=["SITE", "WK", "DEPT", "HRS"])
    ship, e = load_x("x_ship")
    notes["ship"] = e
    if ship is not None and len(ship):
        p = p.merge(ship[["SITE", "WK", "CONTAINERS"]], on=["SITE", "WK"], how="left")
    pay, e = load_x("x_pay")
    notes["pay"] = e
    if pay is not None and len(pay):
        p = p.merge(pay.groupby(["SITE", "WK"], as_index=False)[["REG_H", "OT_H", "VET_H", "VTO_H"]].sum(),
                    on=["SITE", "WK"], how="left")
    met, e = load_x("x_met")
    notes["met"] = e
    if met is not None and len(met):
        p = p.merge(met.groupby(["SITE", "WK"], as_index=False)[["MET_H", "MET_OB_H", "PLAN_REG_H"]].sum(),
                    on=["SITE", "WK"], how="left")
    hires, e = load_x("x_hires")
    notes["hires"] = e
    if hires is not None and len(hires):
        p = p.merge(hires.groupby(["SITE", "WK"], as_index=False)["NEW_HIRES"].sum(), on=["SITE", "WK"], how="left")
        p["NEW_HIRES"] = p["NEW_HIRES"].fillna(0)
    inj, e = load_x("x_inj")      # mechanism counts per site-week (Intelex incident detail)
    notes["mechanism"] = e
    if inj is not None and len(inj) and "MECH" in inj.columns:
        mg = inj.assign(**{k: inj["INJURIES"].where(inj["MECH"].isin(v), 0) for k, v in MECH_GROUPS.items()})
        mg = mg.groupby(["SITE", "WK"], as_index=False)[list(MECH_GROUPS)].sum()
        p = p.merge(mg, on=["SITE", "WK"], how="left")
        for k in MECH_GROUPS:
            p[k] = p[k].fillna(0)
    if wf:
        attr, _ = wf.get("attrition", (None, None))
        if attr is not None and len(attr):
            p = p.merge(attr[["SITE", "WK", "TERMS", "ACTIVE"]], on=["SITE", "WK"], how="left")
        rost, _ = wf.get("roster", (None, None))
        if rost is not None and len(rost):
            r = rost.dropna(subset=["DT"]).rename(columns={"DT": "WK"})
            p = p.merge(r[["SITE", "WK", "HOURLY_HC", "NEW90"]].rename(columns={"NEW90": "NEW90_N"}), on=["SITE", "WK"], how="left")

    for c in ["OB_HRS", "OB_DIRECT_HRS", "OB_UNITS", "OB_TRANS", "IB_HRS", "IB_DIRECT_HRS", "IB_UNITS", "IB_TRANS",
              "CONTAINERS", "REG_H", "OT_H", "VET_H", "VTO_H", "MET_H", "MET_OB_H", "PLAN_REG_H", "NEW_HIRES", "TERMS", "ACTIVE",
              "HOURLY_HC", "NEW90_N", "CLMS_HRS", "CLMS_DIRECT_HRS", "CLMS_STRAIN_HRS", "CLMS_STRAIN_TM_PATHS", "CLMS_OVER_TM_PATHS", "CLMS_TMS",
              "CLMS_TM_PATH_SUM", "CLMS_SINGLE_PATH_TMS", "CLMS_FULL_WEEK_TMS", *MECH_GROUPS]:
        if c not in p.columns:
            p[c] = np.nan
    p = p.sort_values(["SITE", "WK"]).reset_index(drop=True)
    gs = p.groupby("SITE")
    p["HRS_LOAD"] = p["TOTAL_HOURS"] / gs["TOTAL_HOURS"].transform(_prior_mean)
    div = lambda a, b: a / b.where(b > 0)   # noqa: E731
    p["OB_UPH"], p["OB_TPH"] = div(p["OB_UNITS"], p["OB_DIRECT_HRS"]), div(p["OB_TRANS"], p["OB_DIRECT_HRS"])
    p["OB_TLPH"], p["OB_CPLH"] = div(p["OB_UNITS"], p["OB_HRS"]), div(p["CONTAINERS"], p["OB_HRS"])
    p["IB_UPH"], p["IB_TPH"] = div(p["IB_UNITS"], p["IB_DIRECT_HRS"]), div(p["IB_TRANS"], p["IB_DIRECT_HRS"])
    p["IB_TLPH"], p["IB_CPLH"] = div(p["IB_UNITS"], p["IB_HRS"]), div(p["IB_TRANS"], p["IB_HRS"])
    p["IB_UPT"] = div(p["IB_UNITS"], p["IB_TRANS"])
    p["OB_UPH_PUSH"] = (p["OB_UPH"] / gs["OB_UPH"].transform(_prior_mean) - 1) * 100
    p["OT_PCT"], p["MET_PCT"] = div(p["OT_H"], p["REG_H"]) * 100, div(p["MET_H"], p["PLAN_REG_H"]) * 100
    p["VET_PCT"], p["VTO_PCT"] = div(p["VET_H"], p["REG_H"]) * 100, div(p["VTO_H"], p["REG_H"]) * 100
    p["HIRES_100"] = div(p["NEW_HIRES"], p["CLMS_TMS"]) * 100
    p["NEW90"] = div(p["NEW90_N"], p["HOURLY_HC"]) * 100
    p["ATTR"] = div(p["TERMS"], p["ACTIVE"]) * 100
    p["STRAIN_PCT"] = div(p["CLMS_STRAIN_HRS"], p["CLMS_DIRECT_HRS"]) * 100
    p["OVER_UTIL"] = div(p["CLMS_OVER_TM_PATHS"], p["CLMS_STRAIN_TM_PATHS"]) * 100
    p["PATHS_TM"] = div(p["CLMS_TM_PATH_SUM"], p["CLMS_TMS"])
    p["SINGLE_PATH"] = div(p["CLMS_SINGLE_PATH_TMS"], p["CLMS_FULL_WEEK_TMS"]) * 100
    for c in ("PRESSURE", "VOL_PCT", "HRS_PCT", "TPH_PCT", "IB_PCT", "BL_AGE"):
        if c not in p.columns:
            p[c] = np.nan
    p["INJ_ALL"] = p["RECORDABLE_INCIDENTS"] + p["FIRST_AID_INCIDENT"]
    for k, (_, cols) in OUTCOMES.items():
        p[k] = div(p[cols].sum(axis=1, min_count=1), p["TOTAL_HOURS"]) * basis
    p = p[p["TOTAL_HOURS"] >= MIN_WEEK_HOURS].reset_index(drop=True)
    p.attrs["notes"] = notes
    p.attrs["dept_hours"] = dh
    return p


# ---------------------------------------------------------------------------------------------------------------
# Statistics: correlation (Pearson, Spearman, within-site), quartile rate ratios, trigger vs non-trigger
# ---------------------------------------------------------------------------------------------------------------
def _p_from_r(r, n):
    """Two-sided p-value for a correlation via Fisher's z (normal approximation)."""
    if not np.isfinite(r) or n is None or n < 8:
        return np.nan
    r = float(np.clip(r, -0.999999, 0.999999))
    z = abs(math.atanh(r)) * math.sqrt(n - 3)
    return math.erfc(z / math.sqrt(2))


def strength(r):
    a = abs(r) if np.isfinite(r) else np.nan
    if not np.isfinite(a):
        return "—"
    return "negligible" if a < 0.1 else "weak" if a < 0.3 else "moderate" if a < 0.5 else "strong"


def _corr(x, y):
    m = np.isfinite(x) & np.isfinite(y)
    n = int(m.sum())
    if n < 8:
        return np.nan, n
    xs, ys = x[m], y[m]
    if np.std(xs) == 0 or np.std(ys) == 0:
        return np.nan, n
    return float(np.corrcoef(xs, ys)[0, 1]), n


def _shift_outcome(p, lag):
    """Outcome counts/hours from `lag` weeks later at the same site (consecutive weeks only)."""
    if lag == 0:
        return p
    q = p.copy()
    cols = ["RECORDABLE_INCIDENTS", "FIRST_AID_INCIDENT", "MSD_INJURIES", "MOE_INCIDENTS", "TOTAL_HOURS",
            *MECH_GROUPS] + list(OUTCOMES)
    cols = [c for c in cols if c in q.columns]
    nxt = q[["SITE", "WK"] + cols].copy()
    nxt["WK"] = nxt["WK"] - pd.Timedelta(weeks=lag)
    q = q.drop(columns=cols).merge(nxt, on=["SITE", "WK"], how="inner")
    return q


def _avg_rank(v):
    return pd.Series(v).rank(method="average").to_numpy(float)


def _pearson(a, b):
    if len(a) < 8:
        return np.nan
    sa, sb = a.std(), b.std()
    if sa == 0 or sb == 0:
        return np.nan
    return float(((a - a.mean()) * (b - b.mean())).mean() / (sa * sb))


def correlations(p, outcome, lag=0, drivers=None):
    """One row per driver: Pearson and Spearman across site-weeks, within-site r, p-values, quartile rate ratio."""
    q = _shift_outcome(p, lag)
    if q.empty:
        return pd.DataFrame()
    drivers = [d for d in (drivers or list(DRIVERS)) if d in q.columns]
    cols = OUTCOMES[outcome][1]
    y_all = q[outcome].to_numpy(float)
    n_all = q[cols].sum(axis=1, min_count=1).to_numpy(float)
    h_all = q["TOTAL_HOURS"].to_numpy(float)
    codes, uniq = pd.factorize(q["SITE"])
    X = q[drivers].to_numpy(float)
    rows = []
    for j, d in enumerate(drivers):
        x = X[:, j]
        m = np.isfinite(x) & np.isfinite(y_all)
        xs, ys, cs = x[m], y_all[m], codes[m]
        n = int(m.sum())
        row = {"DRIVER": d, "LABEL": DRIVERS[d][0], "GROUP": DRIVERS[d][1], "N": n, "SITES": 0, "R": np.nan, "RHO": np.nan,
               "P": np.nan, "R_WITHIN": np.nan, "P_WITHIN": np.nan, "STRENGTH": "—", "DIRECTION": "—",
               "RR": np.nan, "RR_LO": np.nan, "RR_HI": np.nan, "Q1_MED": np.nan, "Q4_MED": np.nan}
        if n >= 8:
            cnt = np.bincount(cs, minlength=len(uniq)).astype(float)
            n_sites = int((cnt > 0).sum())
            r = _pearson(xs, ys)
            rs = _pearson(_avg_rank(xs), _avg_rank(ys))
            mx = np.bincount(cs, xs, minlength=len(uniq)) / np.maximum(cnt, 1)
            my = np.bincount(cs, ys, minlength=len(uniq)) / np.maximum(cnt, 1)
            rw = _pearson(xs - mx[cs], ys - my[cs])
            key = rw if n_sites > 1 else rs
            row.update({"SITES": n_sites, "R": r, "RHO": rs, "P": _p_from_r(rs, n), "R_WITHIN": rw,
                        "P_WITHIN": _p_from_r(rw, n - n_sites if n_sites > 1 else n), "STRENGTH": strength(key),
                        "DIRECTION": ("higher" if key > 0 else "lower") if np.isfinite(key) else "—"})
            if n >= 40 and len(np.unique(xs)) >= 4:
                order = np.argsort(np.argsort(xs, kind="mergesort"), kind="mergesort")
                qi = (order * 4) // n
                nn, hh = n_all[m], h_all[m]
                a_, b_ = qi == 3, qi == 0
                na, ha, nb, hb = nn[a_].sum(), hh[a_].sum(), nn[b_].sum(), hh[b_].sum()
                if ha > 0 and hb > 0 and na > 0 and nb > 0:
                    rr = (na / ha) / (nb / hb)
                    se = math.sqrt(1 / na + 1 / nb)
                    row.update({"RR": rr, "RR_LO": rr * math.exp(-1.96 * se), "RR_HI": rr * math.exp(1.96 * se)})
                row.update({"Q1_MED": float(np.median(xs[b_])), "Q4_MED": float(np.median(xs[a_]))})
        rows.append(row)
    return pd.DataFrame(rows)


def quartile_rates(p, driver, outcome, lag=0, basis=200_000):
    """Outcome rate (pooled counts ÷ hours) by quartile of a driver, with 95% intervals."""
    q = _shift_outcome(p, lag)
    q = q[np.isfinite(q[driver])]
    if len(q) < 40 or q[driver].nunique() < 4:
        return pd.DataFrame()
    cols = OUTCOMES[outcome][1]
    q = q.assign(_N=q[cols].sum(axis=1, min_count=1), _Q=pd.qcut(q[driver].rank(method="first"), 4, labels=False) + 1)
    t = q.groupby("_Q").agg(LO_V=(driver, "min"), HI_V=(driver, "max"), WEEKS=(driver, "size"), N=("_N", "sum"),
                            H=("TOTAL_HOURS", "sum")).reset_index()
    t["RATE"] = t["N"] * basis / t["H"]
    t["CI_LO"] = np.maximum(t["N"] - 1.96 * np.sqrt(t["N"]), 0) * basis / t["H"]
    t["CI_HI"] = (t["N"] + 1.96 * np.sqrt(t["N"])) * basis / t["H"]
    return t.rename(columns={"_Q": "Q"})


def trigger_compare(p, ow, th, basis=200_000, lag=0):
    """For every OVSR ops rule: outcome rates in FC-weeks where it fired vs weeks it didn't (same or next week)."""
    if ow is None or ow.empty:
        return pd.DataFrame()
    fl = pd.concat([ow[["SITE", "WK"]].reset_index(drop=True), g("ops_flags")(ow, th).reset_index(drop=True)], axis=1)
    fl = fl[ow["DAYS"].reset_index(drop=True) == 7]
    if lag:
        fl = fl.assign(WK=fl["WK"] + pd.Timedelta(weeks=lag))
    j = p.merge(fl, on=["SITE", "WK"], how="inner")
    rows = []
    for rule in g("OPS_RULES"):
        if rule not in j.columns:
            continue
        a, b = j[j[rule]], j[~j[rule]]
        row = {"RULE": rule, "TITLE": g("RESPONSES")[rule][0], "FIRED": len(a), "NOT_FIRED": len(b)}
        for k, (lab, cols) in OUTCOMES.items():
            na, nb = a[cols].sum().sum(), b[cols].sum().sum()
            ha, hb = a["TOTAL_HOURS"].sum(), b["TOTAL_HOURS"].sum()
            ra = na * basis / ha if ha > 0 else np.nan
            rb = nb * basis / hb if hb > 0 else np.nan
            ratio = ra / rb if rb and np.isfinite(rb) and rb > 0 and np.isfinite(ra) else np.nan
            if len(a) < MIN_FIRED:      # too few fired weeks to compare
                ratio = np.nan
            se = math.sqrt(1 / max(na, 0.5) + 1 / max(nb, 0.5))
            row.update({f"{k}_F": ra, f"{k}_NF": rb, f"{k}_RATIO": ratio,
                        f"{k}_LO": ratio * math.exp(-1.96 * se) if np.isfinite(ratio) else np.nan,
                        f"{k}_HI": ratio * math.exp(1.96 * se) if np.isfinite(ratio) else np.nan})
        rows.append(row)
    return pd.DataFrame(rows)


def status_rates(p, hist, basis=200_000):
    """Outcome rates for site-weeks at each OVSR status tier."""
    if hist is None or hist.empty:
        return pd.DataFrame()
    j = p.merge(hist[["SITE", "WK", "STATUS"]], on=["SITE", "WK"], how="inner")
    rows = []
    for stt in g("STATUS_ORDER"):
        s = j[j["STATUS"] == stt]
        row = {"STATUS": stt, "WEEKS": len(s), "HOURS": s["TOTAL_HOURS"].sum()}
        for k, (_, cols) in OUTCOMES.items():
            n, h = s[cols].sum().sum(), s["TOTAL_HOURS"].sum()
            row[k] = n * basis / h if h > 0 else np.nan
            row[k + "_N"] = n
        rows.append(row)
    return pd.DataFrame(rows)


def injury_profile(inj, sites, start=None, end=None):
    """Aggregated injury detail for `sites` (and optional week range)."""
    if inj is None or inj.empty:
        return inj
    d = inj[inj["SITE"].isin(sites)]
    if start is not None:
        d = d[d["WK"] >= pd.Timestamp(start)]
    if end is not None:
        d = d[d["WK"] <= pd.Timestamp(end)]
    return d


def dim_counts(d, dim, top=None):
    if d is None or d.empty:
        return pd.DataFrame(columns=[dim, "INJURIES", "RECORDABLES", "FIRST_AID", "MSD", "MOE", "SHARE"])
    t = d.groupby(dim, as_index=False)[["INJURIES", "RECORDABLES", "FIRST_AID", "MSD", "MOE"]].sum()
    t = t.sort_values("INJURIES", ascending=False)
    t["SHARE"] = t["INJURIES"] / max(t["INJURIES"].sum(), 1) * 100
    return t.head(top) if top else t


# ---------------------------------------------------------------------------------------------------------------
# Context helpers shared by the tab and the snapshot
# ---------------------------------------------------------------------------------------------------------------
def _daily_fact():
    if g("grain") == g("DAILY"):
        return g("fact")
    df, _ = g("load_fact")(g("DAILY"))
    return df


def _wf_data(force=False):
    """Workforce frames if they're loaded (or force-load them)."""
    if force or g("wf_is_warm")() or st.session_state.get("wf_load"):
        return g("load_workforce")()
    return None


@gov.viewer_cache(st, lambda: g("_viewer_context")(), ttl=3600, show_spinner=False)
def panel_cached(basis, wf_on, _rev=6):
    """Full panel for every site (cached). Scope filtering happens after, so all scopes share one build."""
    dfact = _daily_fact()
    hol, _, _, _ = g("load_plan")()
    wf = g("load_workforce")() if wf_on else None
    p = build_panel(dfact, hol, wf, basis)
    return p, p.attrs.get("notes", {}), p.attrs.get("dept_hours")


@gov.viewer_cache(st, lambda: g("_viewer_context")(), ttl=3600, show_spinner=False)
def hist_cached(weeks, th_items, sites):
    """OVSR status for each site-week of the panel (reuses the app's cached scorer)."""
    today_ts = pd.Timestamp(g("_today_et")())
    cur_wk = today_ts - pd.Timedelta(days=(today_ts.weekday() + 1) % 7)
    dfact = _daily_fact()
    last_day = dfact["DT"].max()
    return g("ovsr_history")(g("DAILY"), weeks, th_items, sites, cur_wk, last_day)


def _fmt(v, f, unit=""):
    if v is None or (isinstance(v, float) and not np.isfinite(v)) or pd.isna(v):
        return "—"
    s = format(float(v), f)
    return s + (unit if unit not in ("×",) else "×")


def _lc(s):
    """Lower-case for mid-sentence use, keeping acronyms (TPH, MSD, MET, TM...)."""
    return " ".join(w if sum(ch.isupper() for ch in w) >= 2 else w.lower() for w in str(s).split(" "))


def _ptxt(pv):
    """p-value text: 'p < 0.001' instead of 'p = 0.000'."""
    return "p < 0.001" if pv < 0.001 else f"p = {pv:.3f}"


_SRC_NAMES = {"met": "MET staffing plan (the app's role can't read STAFF_DAILY_DEPARTMENT)"}


def plain_finding(row, outcome_label, lag):
    """One readable sentence for a correlation row."""
    r = row["R_WITHIN"] if row["SITES"] > 1 else row["RHO"]
    when = "the same week" if lag == 0 else "the following week"
    more = "higher" if r > 0 else "lower"
    lab, olab = _lc(row["LABEL"]), _lc(outcome_label)
    rr = (f"; weeks in the top quarter for {lab} ran {row['RR']:.2f}× the {olab} of the bottom quarter"
          if np.isfinite(row.get("RR", np.nan)) else "")
    return (f"When {lab} runs above a site's normal, {olab} tends to be {more} {when} "
            f"(r = {r:+.2f}, {strength(r)}, {_ptxt(row['P_WITHIN'] if row['SITES'] > 1 else row['P'])}, "
            f"{int(row['N'])} site-weeks{rr}).")


def top_findings(ct, outcome_label, lag, k=5, alpha=0.05):
    if ct is None or ct.empty:
        return []
    c = ct.copy()
    c["_R"] = np.where(c["SITES"] > 1, c["R_WITHIN"], c["RHO"])
    c["_P"] = np.where(c["SITES"] > 1, c["P_WITHIN"], c["P"])
    c = c[(c["N"] >= 30) & (c["_P"] < alpha) & (c["_R"].abs() >= 0.1)]
    c = c.reindex(c["_R"].abs().sort_values(ascending=False).index).head(k)
    return [plain_finding(r, outcome_label, lag) for r in c.to_dict("records")]


# ---------------------------------------------------------------------------------------------------------------
# Streamlit tab: Ops × Safety
# ---------------------------------------------------------------------------------------------------------------
def _xs_setup():
    """Panel for the sidebar scope, built once per run and shared by the Ops x Safety sub-tabs."""
    basis = g("basis")
    wf_on = bool(g("wf_is_warm")() or st.session_state.get("wf_load"))
    with st.spinner("Building the site-week panel (ops, CLMS labor, UKG time codes, injuries)…"):
        p_all, notes, dept_h = panel_cached(basis, wf_on)
    pool = sorted(g("scope")(_daily_fact(), g("sel_regions"), g("sel_sites"))["SITE"].unique())
    return {"p": p_all[p_all["SITE"].isin(pool)], "pool": pool, "notes": notes, "dept_h": dept_h, "wf_on": wf_on}


def _xs_vars(x):
    return (x["p"], x["pool"], x["notes"], x["dept_h"], x["wf_on"], g("basis"), g("basis_label"), g("seg"), g("BORDER"),
            g("STRETCH"), g("PALETTE"), st.column_config.NumberColumn)


_XS_EMPTY = "No full site-weeks for the sidebar filters. This view covers FC and Rx sites in the daily OBR table."


def _xs_drivers(x):
    """Drivers & evidence: correlations, the evidence ledger, one driver in detail, triggers, mediation."""
    gov.render_scope(st, _namespace(), "Whole weeks")
    p, pool, notes, dept_h, wf_on, basis, basis_label, seg, BORDER, STRETCH, PALETTE, NC = _xs_vars(x)
    st.markdown("#### Ops × Safety · how operations, labor and rotation move with injuries")
    if p.empty:
        st.info(_XS_EMPTY)
        return
    missing = [k for k, v in notes.items() if v]
    st.caption(f"{p['SITE'].nunique()} sites · {len(p):,} full site-weeks from {p['WK'].min():%b %d, %Y} to "
               f"{p['WK'].max():%b %d, %Y} · weeks with under {MIN_WEEK_HOURS:,} hours are left out · rates {basis_label}. "
               "Sidebar region and site filters apply; the date range doesn't (correlations need every week)."
               + (f" Not loaded: {', '.join(_SRC_NAMES.get(k, k) for k in missing)}." if missing else ""))
    if not wf_on:
        st.button("Load workforce signals (attrition, new-hire share)", key="xs_wf_btn",
                  on_click=lambda: st.session_state.update(wf_load=True),
                  help="Adds attrition and under-90-day share as drivers. Cached for everyone for 6 hours after the first load.")
    c1, c2, c3 = st.columns([2.2, 1.4, 1.6])
    with c1:
        oc = st.selectbox("Outcome", list(OUTCOMES), format_func=lambda k: OUTCOMES[k][0], key="xs_outcome")
    with c2:
        timing = seg("Timing", ["Same week", "Next week"], "Same week", key="xs_lag",
                     help="Next week = the driver this week against injuries the following week at the same site.")
    with c3:
        lens = seg("Lens", ["Within each site", "Across sites"], "Within each site", key="xs_lens",
                   help="Within each site compares every week with that site's own average, so big and small sites "
                        "don't drive the result. Across sites ranks all site-weeks together.")
    lag = 0 if timing == "Same week" else 1
    olab = OUTCOMES[oc][0]
    ct = correlations(p, oc, lag)
    if ct.empty:
        st.info("Not enough site-weeks to correlate.")
        return
    rcol = "R_WITHIN" if lens == "Within each site" and p["SITE"].nunique() > 1 else "RHO"
    pcol = "P_WITHIN" if rcol == "R_WITHIN" else "P"

    with st.container(**BORDER):
        st.markdown(f"**Strongest relationships with {_lc(olab)}** · {timing.lower()}")
        f = top_findings(ct, olab, lag)
        st.markdown("\n".join(f"- {x}" for x in f) if f else
                    "- No driver clears r ≥ 0.10 with p < 0.05 on 30+ site-weeks for this outcome and timing.")
        st.caption("Correlation shows direction and strength, not cause. r runs from −1 to +1: under 0.1 negligible, "
                   "0.1–0.3 weak, 0.3–0.5 moderate, 0.5+ strong. p < 0.05 means unlikely to be chance alone.")

    st.markdown(f"**Every driver vs {_lc(olab)}**")
    tb = ct.assign(R_SHOW=ct[rcol], P_SHOW=ct[pcol], STRENGTH=ct[rcol].map(strength),
                   DIRECTION=np.where(ct[rcol] > 0, "↑ more injuries", np.where(ct[rcol] < 0, "↓ fewer injuries", "—")),
                   RR_TXT=[f"{a:.2f}× ({b:.2f}–{c:.2f})" if np.isfinite(a) else "—"
                           for a, b, c in zip(ct["RR"], ct["RR_LO"], ct["RR_HI"])])
    tb["_O"] = tb["GROUP"].map({k: i for i, k in enumerate(DRIVER_GROUPS)})
    tb = tb.sort_values(["_O", "R_SHOW"], ascending=[True, False])
    NC = st.column_config.NumberColumn
    st.dataframe(tb[["GROUP", "LABEL", "R_SHOW", "STRENGTH", "DIRECTION", "P_SHOW", "N", "RR_TXT"]], hide_index=True, **STRETCH,
                 column_config={"GROUP": "Group", "LABEL": "Driver", "R_SHOW": NC("r", format="%+.2f"),
                                "STRENGTH": "Strength", "DIRECTION": "When the driver is higher", "P_SHOW": NC("p", format="%.3f"),
                                "N": NC("Site-weeks", format="%d"),
                                "RR_TXT": st.column_config.TextColumn("Top vs bottom quarter (rate ratio, 95%)",
                                                                      help="Pooled injuries ÷ hours in the top quarter of "
                                                                           "site-weeks for the driver, over the bottom quarter.")})

    # Matrix: all drivers × outcomes
    mats = []
    for k in OUTCOMES:
        c = correlations(p, k, lag)
        if len(c):
            mats.append(c.assign(OUTCOME=OUTCOMES[k][0], R_SHOW=c[rcol], P_SHOW=c[pcol]))
    if mats:
        m = pd.concat(mats, ignore_index=True)
        m["SIG"] = np.where(m["P_SHOW"] < 0.05, "●", "")
        m["TXT"] = [f"{r:+.2f}{s}" if np.isfinite(r) else "" for r, s in zip(m["R_SHOW"], m["SIG"])]
        short = {v[0]: OUT_SHORT.get(k, v[0]) for k, v in OUTCOMES.items()}
        m["OUT_S"] = m["OUTCOME"].map(short)
        order = [DRIVERS[d][0] for d in DRIVERS if d in set(m["DRIVER"])]
        st.markdown("**Correlation map** · every driver against every outcome")
        base = alt.Chart(m).encode(
            x=alt.X("OUT_S:N", sort=[OUT_SHORT.get(k, v[0]) for k, v in OUTCOMES.items()], title=None,
                    axis=alt.Axis(labelAngle=0, labelLimit=150, labelOverlap=False, orient="top", ticks=False, domain=False)),
            y=alt.Y("LABEL:N", sort=order, title=None, axis=alt.Axis(ticks=False, domain=False, labelLimit=220)))
        heat = base.mark_rect(cornerRadius=2).encode(
            color=alt.Color("R_SHOW:Q", title="r", scale=alt.Scale(domain=[-0.4, 0, 0.4], range=["#2a78d6", "#f2f1ee", "#e34948"],
                                                                     clamp=True), legend=alt.Legend(orient="bottom")),
            tooltip=[alt.Tooltip("LABEL:N", title="Driver"), alt.Tooltip("OUTCOME:N", title="Outcome"),
                     alt.Tooltip("R_SHOW:Q", title="r", format="+.2f"), alt.Tooltip("P_SHOW:Q", title="p", format=".3f"),
                     alt.Tooltip("N:Q", title="Site-weeks")])
        txt = base.mark_text(fontSize=11).encode(text="TXT:N")
        g("show_chart")((heat + txt).properties(height=24 * len(order) + 60))
        st.caption("Red = injuries tend to rise with the driver, blue = fall. ● = p < 0.05. "
                   + ("Within each site: each site-week compared with that site's own average." if rcol == "R_WITHIN"
                      else "Across sites: Spearman rank correlation over all site-weeks."))

    render_ledger(p)

    # Driver detail
    st.markdown("**Look at one driver**")
    drv_opts = list(tb["DRIVER"])
    best = tb.reindex(tb["R_SHOW"].abs().sort_values(ascending=False).index)["DRIVER"].iloc[0]
    d1, _ = st.columns([2, 3])
    drv = d1.selectbox("Driver", drv_opts, index=drv_opts.index(best), format_func=lambda d: DRIVERS[d][0], key="xs_drv")
    qr = quartile_rates(p, drv, oc, lag, basis)
    if qr.empty:
        st.caption("Not enough site-weeks with this driver to split into quarters.")
    else:
        lab, _g, unit, fmt_, note = DRIVERS[drv]
        qr["BIN"] = [f"Q{int(q)}: {_fmt(a, fmt_)}–{_fmt(b, fmt_)}{'' if unit in ('', '×') else ' ' + unit}" for q, a, b in
                     zip(qr["Q"], qr["LO_V"], qr["HI_V"])]
        bars = alt.Chart(qr).mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4, color=PALETTE[0]).encode(
            x=alt.X("BIN:N", sort=list(qr["BIN"]), title=f"{lab} · quarter of site-weeks", axis=alt.Axis(labelAngle=0, labelLimit=180)),
            y=alt.Y("RATE:Q", title=f"{olab} {basis_label.replace(' (OSHA)', '')}"),
            tooltip=[alt.Tooltip("BIN:N", title=""), alt.Tooltip("RATE:Q", title="Rate", format=".2f"),
                     alt.Tooltip("CI_LO:Q", title="95% low", format=".2f"), alt.Tooltip("CI_HI:Q", title="95% high", format=".2f"),
                     alt.Tooltip("WEEKS:Q", title="Site-weeks"), alt.Tooltip("N:Q", title="Injuries")])
        err = alt.Chart(qr).mark_rule(color=g("RULE_GRAY"), strokeWidth=1.5).encode(
            x=alt.X("BIN:N", sort=list(qr["BIN"])), y="CI_LO:Q", y2="CI_HI:Q")
        g("show_chart")((bars + err).properties(height=280))
        st.caption(f"{note} Bars = pooled {_lc(olab)} for the site-weeks in each quarter of the driver; whiskers = 95% "
                   "interval. Quarters pool every site in scope, so site size differences can show up here.")
    st.markdown(f"**{DRIVERS[drv][0]} and {_lc(olab)} by week** · last 52 weeks, one axis")
    show_paired(p, drv, oc, lag, weeks=52)

    # Triggers vs non-triggers
    st.markdown("#### Triggers vs non-triggers")
    th = g("ovsr_th_current")()
    hol, _, _, _ = g("load_plan")()
    ow = g("ops_weeks")(hol)
    ow = ow[ow["SITE"].isin(pool)]
    tc = trigger_compare(p, ow, th, basis, lag)
    if tc.empty:
        st.caption("No FC ops data for the sites in scope.")
    else:
        show = tc[["TITLE", "FIRED", "NOT_FIRED", f"{oc}_F", f"{oc}_NF", f"{oc}_RATIO"]].copy()
        show["CI"] = [f"{a:.2f}–{b:.2f}" if np.isfinite(a) else "—" for a, b in zip(tc[f"{oc}_LO"], tc[f"{oc}_HI"])]
        st.dataframe(show, hide_index=True, **STRETCH, column_config={
            "TITLE": "Trigger", "FIRED": NC("FC-weeks fired", format="%d"), "NOT_FIRED": NC("FC-weeks not fired", format="%d"),
            f"{oc}_F": NC("Rate when fired", format="%.2f", help=f"{olab} per {basis:,} hours in FC-weeks where it fired"),
            f"{oc}_NF": NC("Rate otherwise", format="%.2f", help=f"{olab} per {basis:,} hours in every other FC-week"),
            f"{oc}_RATIO": NC("Ratio", format="%.2f×", help=f"Fired ÷ not fired. Shown once a trigger fired in {MIN_FIRED}+ FC-weeks."),
            "CI": "95% interval"})
        st.caption(f"Pooled {_lc(olab)} in FC-weeks where each trigger fired vs every other FC-week"
                   + (" (injuries the following week)." if lag else ".") + " An interval entirely above 1.00 means the "
                   "trigger lines up with more injuries. Thresholds in force are the Site tab's.")
    wks = tuple(sorted(p["WK"].unique()))[-52:]
    hist = hist_cached(wks, tuple(sorted(th.items())), tuple(sorted(p["SITE"].unique())))
    sr = status_rates(p, hist, basis)
    if len(sr):
        st.markdown(f"**{olab} by OVSR status** · last {len(wks)} weeks")
        s2 = sr[["STATUS", "WEEKS", oc, oc + "_N"]]
        st.dataframe(s2, hide_index=True, **STRETCH, column_config={
            "STATUS": "Status that week", "WEEKS": NC("Site-weeks", format="%d"), oc: NC(olab, format="%.2f"),
            oc + "_N": NC("Injuries", format="%d")})

    render_mediation(p, lag)
    with st.expander("How these drivers are measured"):
        st.markdown("\n".join(f"- **{v[0]}** ({v[1]}) — {v[4]}" for v in DRIVERS.values()))

def _xs_profile(x):
    """Injury profile: who, where and when people get hurt (incident detail, aggregated in Snowflake)."""
    gov.render_scope(st, _namespace(), "Whole weeks")
    p, pool, notes, dept_h, wf_on, basis, basis_label, seg, BORDER, STRETCH, PALETTE, NC = _xs_vars(x)
    # Injury profile
    st.markdown("#### Who, where and when · injury profile")
    inj, inj_err = load_x("x_inj")
    if inj is None:
        st.info(f"The incident detail table couldn't be read: {inj_err}")
    else:
        lo_wk = pd.Timestamp(g("start")) - pd.Timedelta(days=(pd.Timestamp(g("start")).weekday() + 1) % 7)
        d = injury_profile(inj, pool, lo_wk, pd.Timestamp(g("end")))
        st.caption(f"{int(d['INJURIES'].sum()):,} work-related injuries in the sidebar date range at the sites in scope "
                   f"({X_SRC['inc']}; non-occupational excluded). Names, narratives and IDs are never pulled.")
        try:
            render_mech(inj, pool, lo_wk, pd.Timestamp(g("end")))
        except Exception as exc:   # the mechanism section never breaks the rest of the profile
            st.caption(f"Mechanism section unavailable: {exc}")
        t1, t2, t3, t4, t5 = [st.container() for _ in range(5)]
        with t1:
            st.markdown("##### Department & role")
            a, b = g("pair_cols")(2)
            dc = dim_counts(d, "DEPT")
            if dept_h is not None and len(dept_h):
                dh = dept_h[dept_h["SITE"].isin(pool) & (dept_h["WK"] >= lo_wk) & (dept_h["WK"] <= pd.Timestamp(g("end")))]
                dh = dh.groupby("DEPT")["HRS"].sum()
                dc["HOURS"] = dc["DEPT"].map(dh)
                dc["RATE"] = dc["INJURIES"] * basis / dc["HOURS"].where(dc["HOURS"] > 0)
            with a:
                st.markdown("**By department**")
                st.dataframe(dc, hide_index=True, **STRETCH, column_config={
                    "DEPT": "Department", "INJURIES": NC("Injuries", format="%d"), "RECORDABLES": NC("Recordable", format="%d"),
                    "FIRST_AID": NC("First aid", format="%d"), "MSD": NC("MSD", format="%d"), "MOE": NC("MOE", format="%d"),
                    "SHARE": NC("Share", format="%.0f%%"), "HOURS": NC("CLMS hours", format="%d"),
                    "RATE": NC("Injury rate", format="%.2f", help=f"Injuries {basis_label} logged in that CLMS department")})
            with b:
                st.markdown("**By position**")
                st.dataframe(dim_counts(d, "ROLE", 12), hide_index=True, **STRETCH, column_config={
                    "ROLE": "Position", "INJURIES": NC("Injuries", format="%d"), "RECORDABLES": NC("Recordable", format="%d"),
                    "FIRST_AID": NC("First aid", format="%d"), "MSD": NC("MSD", format="%d"), "MOE": NC("MOE", format="%d"),
                    "SHARE": NC("Share", format="%.0f%%")})
        with t2:
            st.markdown("##### Extra time · MET and VET")
            et = dim_counts(d, "EXTRA_TIME")
            ps = p[(p["WK"] >= lo_wk) & (p["WK"] <= pd.Timestamp(g("end")))]
            reg_h, ot_h, met_h = ps["REG_H"].sum(), ps["OT_H"].sum(), ps["MET_H"].sum()
            n_ext = et.loc[et["EXTRA_TIME"].isin(["MET", "VET"]), "INJURIES"].sum()
            n_reg = et.loc[et["EXTRA_TIME"] == "Regular time", "INJURIES"].sum()
            k = g("kpi_cols")(4)
            k[0].metric("Injuries on MET", f"{int(et.loc[et['EXTRA_TIME'] == 'MET', 'INJURIES'].sum()):,}", **BORDER)
            k[1].metric("Injuries on VET", f"{int(et.loc[et['EXTRA_TIME'] == 'VET', 'INJURIES'].sum()):,}", **BORDER)
            unknown = int(et.loc[et["EXTRA_TIME"] == "Not recorded", "INJURIES"].sum())
            total = int(et["INJURIES"].sum())
            k[2].metric("Unknown overtime classification", f"{unknown:,}", f"{unknown/total:.1%} of injuries" if total else None, **BORDER)
            k[3].metric("Extra-time rate comparison", "Unavailable", help="Incident MET/VET labels have not been reconciled to UKG payroll exposure categories.", **BORDER)
            st.info("Overtime comparison requires reconciliation: injury MET/VET labels, UKG overtime and scheduled MET measure different populations. Rates and ratio are withheld.")
            st.caption("From the incident's 'Working OT hours' field (No / Yes – MET / Yes – VET). Extra-time hours = UKG "
                       f"Overtime ({ot_h:,.0f} h in range). MET scheduled in the staffing plan for these weeks: {met_h:,.0f} h. "
                       f"{int(et.loc[et['EXTRA_TIME'] == 'Not recorded', 'INJURIES'].sum()):,} injuries don't record it.")
        with t3:
            st.markdown("##### Tenure")
            tn = dim_counts(d, "TENURE")
            order_t = ["Under 30 days", "30-89 days", "90 days - 1 year", "1 year +", "Not recorded"]
            tn["_O"] = tn["TENURE"].map({k_: i for i, k_ in enumerate(order_t)})
            st.dataframe(tn.sort_values("_O").drop(columns="_O"), hide_index=True, **STRETCH, column_config={
                "TENURE": "Tenure at injury", "INJURIES": NC("Injuries", format="%d"), "SHARE": NC("Share", format="%.0f%%"),
                "RECORDABLES": NC("Recordable", format="%d"), "FIRST_AID": NC("First aid", format="%d"),
                "MSD": NC("MSD", format="%d"), "MOE": NC("MOE", format="%d")})
            ps = p[(p["WK"] >= lo_wk) & (p["WK"] <= pd.Timestamp(g("end")))]
            if ps["NEW90_N"].notna().any():
                hc_share = ps["NEW90_N"].sum() / max(ps["HOURLY_HC"].sum(), 1) * 100
                inj90 = tn.loc[tn["TENURE"].isin(["Under 30 days", "30-89 days"]), "INJURIES"].sum() / max(
                    tn.loc[tn["TENURE"] != "Not recorded", "INJURIES"].sum(), 1) * 100
                st.markdown(f"Associates under 90 days were **{hc_share:.0f}%** of hourly headcount and **{inj90:.0f}%** of "
                            f"injuries with a hire date. This compares case mix with headcount, not exposure-adjusted risk." if hc_share else "")
            else:
                st.caption("Load workforce signals to compare with the under-90-day share of headcount.")
        with t4:
            st.markdown("##### Hours into the path")
            hp = dim_counts(d, "HOURS_IN_PATH")
            order_h = ["0-1 h", "2-3 h", "4-5 h", "6-7 h", "8 h +", "Not recorded"]
            hp = hp.set_index("HOURS_IN_PATH").reindex(order_h).fillna(0).reset_index()
            ch = alt.Chart(hp[hp["HOURS_IN_PATH"] != "Not recorded"]).mark_bar(
                cornerRadiusTopLeft=4, cornerRadiusTopRight=4, color=PALETTE[1]).encode(
                x=alt.X("HOURS_IN_PATH:N", sort=order_h, title="Hours into the path when injured", axis=alt.Axis(labelAngle=0)),
                y=alt.Y("INJURIES:Q", title="Injuries"),
                tooltip=[alt.Tooltip("HOURS_IN_PATH:N", title=""), alt.Tooltip("INJURIES:Q", title="Injuries"),
                         alt.Tooltip("MSD:Q", title="MSD"), alt.Tooltip("SHARE:Q", title="Share", format=".0f")])
            g("show_chart")(ch.properties(height=240))
            st.caption("Case mix only. The first four bands span 2 hours; 8+ is open ended. Exposure hours in these bands are unavailable, so counts do not establish a fatigue rate.")
        with t5:
            st.markdown("##### Path, labor sharing and product")
            cols_ = st.columns(3)
            for col_, dim, title in ((cols_[0], "IN_PATH", "Job path"), (cols_[1], "SHARED", "Labor sharing"),
                                     (cols_[2], "PRODUCT", "Chewy product involved")):
                with col_:
                    st.markdown(f"**{title}**")
                    st.dataframe(dim_counts(d, dim)[[dim, "INJURIES", "MSD", "SHARE"]], hide_index=True, **STRETCH,
                                 column_config={dim: "", "INJURIES": NC("Injuries", format="%d"), "MSD": NC("MSD", format="%d"),
                                                "SHARE": NC("Share", format="%.0f%%")})
            st.caption("Outside job path = the TM wasn't in their assigned job path when injured. Item numbers, weight and "
                       "size are on the Items & promotions tab (read from the report text and matched to the product catalog).")
        a2, b2 = g("pair_cols")(2)
        with a2:
            st.markdown("**Top injury types**")
            st.dataframe(dim_counts(d, "INJURY_TYPE", 8)[["INJURY_TYPE", "INJURIES", "MSD", "SHARE"]], hide_index=True, **STRETCH,
                         column_config={"INJURY_TYPE": "Type", "INJURIES": NC("Injuries", format="%d"),
                                        "MSD": NC("MSD", format="%d"), "SHARE": NC("Share", format="%.0f%%")})
        with b2:
            st.markdown("**Top causes**")
            st.dataframe(dim_counts(d, "CAUSE", 8)[["CAUSE", "INJURIES", "MSD", "SHARE"]], hide_index=True, **STRETCH,
                         column_config={"CAUSE": "Cause", "INJURIES": NC("Injuries", format="%d"),
                                        "MSD": NC("MSD", format="%d"), "SHARE": NC("Share", format="%.0f%%")})


def _xs_labor(x):
    """Labor, productivity and rotation trends for the scope, with MET scheduled ahead."""
    gov.render_scope(st, _namespace(), "Whole weeks")
    p, pool, notes, dept_h, wf_on, basis, basis_label, seg, BORDER, STRETCH, PALETTE, NC = _xs_vars(x)
    if p.empty:
        st.info(_XS_EMPTY)
        return
    # Labor, productivity and rotation trends for the scope
    st.markdown("#### Labor, productivity and rotation · last 26 weeks")
    agg = scope_weekly(p).tail(26)
    if len(agg):
        a3, b3 = g("pair_cols")(2)
        with a3:
            st.markdown("**Outbound and inbound UPH**")
            g("show_chart")(g("_wk_chart")(agg, {"OB_UPH": "Outbound UPH", "IB_UPH": "Inbound UPH"}, "Units per direct hour", ",.0f"))
        with b3:
            st.markdown("**Extra time and VTO** · % of regular hours")
            g("show_chart")(g("_wk_chart")(agg, {"OT_PCT": "Overtime", "MET_PCT": "MET", "VTO_PCT": "VTO"}, "% of regular hours", ".1f"))
        a4, b4 = g("pair_cols")(2)
        with a4:
            st.markdown("**High-strain rotation**")
            g("show_chart")(g("_wk_chart")(agg, {"OVER_UTIL": "Over-utilized TM-paths", "STRAIN_PCT": "High-strain hours",
                                                  "SINGLE_PATH": "Single-path TMs"}, "%", ".0f"))
        with b4:
            st.markdown("**New hires** · per week")
            g("show_chart")(g("_wk_chart")(agg, {"NEW_HIRES": "New hires"}, "Hires", ",.0f", kind="bar", colors=[PALETTE[2]]))
        met, _ = load_x("x_met")
        if met is not None and len(met):
            cur = _wk_of(g("_today_et")())
            ma = met[met["SITE"].isin(pool) & (met["WK"] >= cur)].groupby("WK", as_index=False)[["MET_H", "PLAN_REG_H"]].sum()
            if len(ma) and ma["MET_H"].sum() > 0:
                ma["MET_PCT"] = ma["MET_H"] / ma["PLAN_REG_H"].where(ma["PLAN_REG_H"] > 0) * 100
                a5, b5 = g("pair_cols")(2)
                with a5:
                    st.markdown("**MET scheduled ahead** · hours, staffing plan")
                    g("show_chart")(g("_wk_chart")(ma, {"MET_H": "MET hours"}, "Hours", ",.0f", kind="bar", colors=[PALETTE[7]]))
                with b5:
                    st.markdown("**MET share ahead** · % of planned regular hours")
                    g("show_chart")(g("_wk_chart")(ma, {"MET_PCT": "MET share"}, "%", ".1f", colors=[PALETTE[7]]))
                pk = ma.loc[ma["MET_H"].idxmax()]
                st.caption(f"Latest staffing-plan run. MET peaks the week of {pk['WK']:%b %d} at {pk['MET_H']:,.0f} hours "
                           f"({pk['MET_PCT']:.1f}% of planned regular hours) for the sites in scope. Watch PIT operators and "
                           "high-strain paths late in those shifts.")
        st.caption("Over-utilized = a TM-path in a high-strain path with more than 106 hours in the last 4 weeks "
                   "(Network Rotation Tool rule). MET = MET scheduled in the network staffing plan ÷ planned regular hours "
                   "(plan history starts Sep 13, 2026; UKG has no separate MET pay code).")


def render_tab():
    """Everything on one page (older app versions call this; the current app uses render_xs and sub-tabs)."""
    x = _xs_setup()
    _xs_drivers(x)
    if not x["p"].empty:
        _xs_profile(x)
        _xs_labor(x)


def scope_weekly(p, basis=None):
    """Roll a (filtered) panel up to one row per week, re-deriving every rate from summed parts."""
    basis = basis or g("basis") or 200_000
    if p.empty:
        return p
    sums = ["TOTAL_HOURS", "RECORDABLE_INCIDENTS", "FIRST_AID_INCIDENT", "MSD_INJURIES", "MOE_INCIDENTS", "PIT_INCIDENTS",
            "OB_UNITS_ACT", "OB_UNITS_PLAN", "OB_HRS_ACT", "OB_HRS_PLAN", "IB_UNITS_ACT", "IB_PLAN",
            "OB_HRS", "OB_DIRECT_HRS", "OB_UNITS", "OB_TRANS", "IB_HRS", "IB_DIRECT_HRS", "IB_UNITS", "IB_TRANS",
            "CONTAINERS", "REG_H", "OT_H", "VET_H", "VTO_H", "MET_H", "MET_OB_H", "PLAN_REG_H", "NEW_HIRES", "TERMS", "ACTIVE",
            "HOURLY_HC", "NEW90_N",
            "CLMS_HRS", "CLMS_DIRECT_HRS", "CLMS_STRAIN_HRS", "CLMS_STRAIN_TM_PATHS", "CLMS_OVER_TM_PATHS", "CLMS_TMS",
            "CLMS_TM_PATH_SUM", "CLMS_SINGLE_PATH_TMS", "CLMS_FULL_WEEK_TMS", *MECH_GROUPS]
    sums = [c for c in sums if c in p.columns]
    a = p.groupby("WK", as_index=False)[sums].sum(min_count=1)
    a = a.merge(p.groupby("WK", as_index=False).agg(TPH_PCT=("TPH_PCT", "mean"), BL_AGE=("BL_AGE", "mean"),
                                                    SITES=("SITE", "nunique")), on="WK")
    div = lambda x, y: x / y.where(y > 0)   # noqa: E731
    a["VOL_PCT"] = (div(a["OB_UNITS_ACT"], a["OB_UNITS_PLAN"]) - 1) * 100
    a["HRS_PCT"] = (div(a["OB_HRS_ACT"], a["OB_HRS_PLAN"]) - 1) * 100
    a["PRESSURE"] = a["VOL_PCT"] - a["HRS_PCT"]
    a["IB_PCT"] = (div(a["IB_UNITS_ACT"], a["IB_PLAN"]) - 1) * 100 if "IB_PLAN" in a else np.nan
    a["OB_UPH"], a["OB_TPH"] = div(a["OB_UNITS"], a["OB_DIRECT_HRS"]), div(a["OB_TRANS"], a["OB_DIRECT_HRS"])
    a["OB_TLPH"], a["OB_CPLH"] = div(a["OB_UNITS"], a["OB_HRS"]), div(a["CONTAINERS"], a["OB_HRS"])
    a["IB_UPH"], a["IB_TPH"] = div(a["IB_UNITS"], a["IB_DIRECT_HRS"]), div(a["IB_TRANS"], a["IB_DIRECT_HRS"])
    a["IB_TLPH"], a["IB_CPLH"] = div(a["IB_UNITS"], a["IB_HRS"]), div(a["IB_TRANS"], a["IB_HRS"])
    a["IB_UPT"] = div(a["IB_UNITS"], a["IB_TRANS"])
    a["OT_PCT"], a["MET_PCT"] = div(a["OT_H"], a["REG_H"]) * 100, div(a["MET_H"], a["PLAN_REG_H"]) * 100
    a["VET_PCT"], a["VTO_PCT"] = div(a["VET_H"], a["REG_H"]) * 100, div(a["VTO_H"], a["REG_H"]) * 100
    a["HIRES_100"] = div(a["NEW_HIRES"], a["CLMS_TMS"]) * 100
    a["NEW90"] = div(a["NEW90_N"], a["HOURLY_HC"]) * 100
    a["ATTR"] = div(a["TERMS"], a["ACTIVE"]) * 100
    a["STRAIN_PCT"] = div(a["CLMS_STRAIN_HRS"], a["CLMS_DIRECT_HRS"]) * 100
    a["OVER_UTIL"] = div(a["CLMS_OVER_TM_PATHS"], a["CLMS_STRAIN_TM_PATHS"]) * 100
    a["PATHS_TM"] = div(a["CLMS_TM_PATH_SUM"], a["CLMS_TMS"])
    a["SINGLE_PATH"] = div(a["CLMS_SINGLE_PATH_TMS"], a["CLMS_FULL_WEEK_TMS"]) * 100
    for k, (_, cols) in OUTCOMES.items():
        a[k] = div(a[cols].sum(axis=1, min_count=1), a["TOTAL_HOURS"]) * basis
    a = a.sort_values("WK").reset_index(drop=True)
    a["HRS_LOAD"] = a["TOTAL_HOURS"] / _prior_mean(a["TOTAL_HOURS"])            # same rules as the site panel
    a["OB_UPH_PUSH"] = (a["OB_UPH"] / _prior_mean(a["OB_UPH"]) - 1) * 100
    return a


# ---------------------------------------------------------------------------------------------------------------
# Mediation analysis: exposure -> mediator -> outcome (Baron & Kenny paths + bootstrapped indirect effect)
# ---------------------------------------------------------------------------------------------------------------
MEDIATION_PATHS = [
    ("VOL_PCT", "OT_PCT", "INJ_RATE"),
    ("VOL_PCT", "MET_PCT", "INJ_RATE"),
    ("PRESSURE", "OB_UPH_PUSH", "MSDR"),
    ("HRS_LOAD", "OVER_UTIL", "MSDR"),
    ("IB_PCT", "STRAIN_PCT", "MSDR"),
    ("HIRES_100", "PATHS_TM", "INJ_RATE"),
]


def _ols(y, cols):
    """Coefficients and standard errors for y ~ 1 + cols (numpy only)."""
    X = np.column_stack([np.ones(len(y))] + list(cols))
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    dof = max(len(y) - X.shape[1], 1)
    s2 = float(resid @ resid) / dof
    try:
        cov = s2 * np.linalg.inv(X.T @ X)
        se = np.sqrt(np.clip(np.diag(cov), 0, None))
    except np.linalg.LinAlgError:
        se = np.full(X.shape[1], np.nan)
    return beta[1:], se[1:]


def _pz(b, se):
    if not np.isfinite(b) or not np.isfinite(se) or se <= 0:
        return np.nan
    return math.erfc(abs(b / se) / math.sqrt(2))


def mediation(p, x, m, y, lag=0, boots=600, seed=11, within=True):
    """Standardized mediation within sites: predictor X -> mediator M -> outcome Y. Returns a, b, c, c', indirect a*b (+95% bootstrap CI), % mediated."""
    if any(k not in p.columns for k in (x, m, y)):
        return None
    q = _shift_outcome(p, lag) if lag else p
    d = q[["SITE", "WK", x, m, y]].replace([np.inf, -np.inf], np.nan).dropna()
    if len(d) < 40 or d[x].nunique() < 5 or d[m].nunique() < 5:
        return None
    if within and d["SITE"].nunique() > 1:
        for c_ in (x, m, y):
            d[c_] = d[c_] - d.groupby("SITE")[c_].transform("mean")
    z = lambda s: ((s - s.mean()) / s.std()).to_numpy(float) if s.std() > 0 else None   # noqa: E731
    X, M, Y = z(d[x]), z(d[m]), z(d[y])
    if X is None or M is None or Y is None:
        return None
    (c,), (se_c,) = _ols(Y, [X])
    (a,), (se_a,) = _ols(M, [X])
    (cp, b), (se_cp, se_b) = _ols(Y, [X, M])
    ind = a * b
    rng = np.random.default_rng(seed)
    n = len(X)
    bs = np.empty(boots)
    for i in range(boots):
        ix = gov.block_indices(d["WK"], rng, block=4)
        (ai,), _ = _ols(M[ix], [X[ix]])
        (_, bi), _ = _ols(Y[ix], [X[ix], M[ix]])
        bs[i] = ai * bi
    lo, hi = np.nanpercentile(bs, [2.5, 97.5])
    return {"x": x, "m": m, "y": y, "n": n, "sites": int(d["SITE"].nunique()), "lag": lag,
            "a": a, "b": b, "c": c, "cp": cp, "p_a": _pz(a, se_a), "p_b": _pz(b, se_b), "p_c": _pz(c, se_c),
            "p_cp": _pz(cp, se_cp), "ind": ind, "ind_lo": lo, "ind_hi": hi,
            "ind_sig": bool(lo > 0 or hi < 0), "pct_med": (ind / c * 100) if abs(c) > 0.02 else np.nan}


def _lab(k):
    if k in DRIVERS:
        return DRIVERS[k][0]
    if k in OUTCOMES:
        return OUTCOMES[k][0]
    return k


def mediation_sentence(r):
    if r is None:
        return "Not enough complete site-weeks for an exploratory decomposition."
    return (f"Exploratory decomposition: indirect association {r['ind']:+.3f}, 95% four-week block-bootstrap interval "
            f"{r['ind_lo']:+.3f} to {r['ind_hi']:+.3f}. This observational decomposition does not show causation or the effect of changing staffing.")


def _wrap(txt, n=26):
    words, lines, cur = str(txt).split(), [], ""
    for w_ in words:
        if len(cur) + len(w_) + 1 > n and cur:
            lines.append(cur)
            cur = w_
        else:
            cur = (cur + " " + w_).strip()
    lines.append(cur)
    return lines[:2] if len(lines) <= 2 else [lines[0], (" ".join(lines[1:]))[: n - 1] + "…"]


def mediation_svg(r, width=640):
    """Box-and-arrow diagram (predictor X, mediator M, outcome Y) with standardized path coefficients. Plain inline SVG."""
    E_ = lambda s: html.escape(str(s))   # noqa: E731
    xl, ml, yl = _lab(r["x"]), _lab(r["m"]), _lab(r["y"])

    def coef(v, pv):
        if not np.isfinite(v):
            return "—"
        stars = "**" if (np.isfinite(pv) and pv < 0.01) else "*" if (np.isfinite(pv) and pv < 0.05) else ""
        return f"{v:+.2f}{stars}"
    W, H, bw, bh = 640, 300, 200, 62
    ex, ey, ox, oy, mx, my = 12, 168, W - 200 - 12, 168, (W - 200) / 2, 16
    ink, line = "currentColor", "#8a8f98"
    sa = np.isfinite(r["p_a"]) and r["p_a"] < 0.05
    sb = np.isfinite(r["p_b"]) and r["p_b"] < 0.05
    sc = np.isfinite(r["p_cp"]) and r["p_cp"] < 0.05

    def box(x0, y0, txt, sub):
        ls = _wrap(txt)
        y_first = y0 + (24 if len(ls) == 1 else 19)
        t_ = "".join(f"<text x='{x0 + bw / 2}' y='{y_first + 15 * i}' text-anchor='middle' font-size='13' font-weight='600' "
                     f"fill='{ink}'>{E_(l_)}</text>" for i, l_ in enumerate(ls))
        return (f"<rect x='{x0}' y='{y0}' width='{bw}' height='{bh}' rx='8' fill='none' stroke='{line}' stroke-width='1.25'/>" + t_
                + f"<text x='{x0 + bw / 2}' y='{y0 + bh - 9}' text-anchor='middle' font-size='11' fill='{ink}' fill-opacity='0.6'>{E_(sub)}</text>")

    def arrow(x1, y1, x2, y2, bold):
        return (f"<line x1='{x1:.0f}' y1='{y1:.0f}' x2='{x2:.0f}' y2='{y2:.0f}' stroke='{ink if bold else line}' "
                f"stroke-width='{2 if bold else 1.4}' marker-end='url(#medarrow)'/>")
    ax1, ay1, ax2, ay2 = ex + bw / 2, ey, mx + 30, my + bh
    bx1, by1, bx2, by2 = mx + bw - 30, my + bh, ox + bw / 2, oy
    svg = [f"<svg viewBox='0 0 {W} {H}' role='img' aria-label='Mediation: predictor {E_(xl)}, mediator {E_(ml)}, outcome {E_(yl)}' "
           f"style='width:100%;max-width:{width}px;height:auto;font-family:inherit'>"
           f"<defs><marker id='medarrow' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' markerHeight='7' orient='auto-start-reverse'>"
           f"<path d='M0 0L10 5L0 10z' fill='{line}'/></marker></defs>",
           box(ex, ey, xl, "Predictor · X"), box(mx, my, ml, "Mediator · M"), box(ox, oy, yl, "Outcome · Y"),
           arrow(ax1, ay1, ax2, ay2, sa), arrow(bx1, by1, bx2, by2, sb),
           arrow(ex + bw, ey + bh / 2, ox - 3, oy + bh / 2, sc),
           f"<text x='{(ax1 + ax2) / 2 - 10:.0f}' y='{(ay1 + ay2) / 2:.0f}' text-anchor='end' font-size='13' fill='{ink}'>a = {coef(r['a'], r['p_a'])}</text>",
           f"<text x='{(bx1 + bx2) / 2 + 10:.0f}' y='{(by1 + by2) / 2:.0f}' text-anchor='start' font-size='13' fill='{ink}'>b = {coef(r['b'], r['p_b'])}</text>",
           f"<text x='{W / 2:.0f}' y='{ey + bh / 2 - 9:.0f}' text-anchor='middle' font-size='13' fill='{ink}'>c′ = {coef(r['cp'], r['p_cp'])}</text>",
           f"<text x='{W / 2:.0f}' y='{H - 12}' text-anchor='middle' font-size='12' fill='{ink}' fill-opacity='0.75'>total c = {coef(r['c'], r['p_c'])} · "
           f"indirect a×b = {r['ind']:+.3f} (95% {r['ind_lo']:+.3f} to {r['ind_hi']:+.3f})</text>",
           "</svg>"]
    return "".join(svg)


def render_mediation(p, lag):
    """Mediation section of the Ops × Safety tab."""
    st.caption("Exploratory decomposition of observed associations. Four-week calendar blocks retain all sites together. This is not a causal mediation analysis.")
    BORDER = g("BORDER")
    st.markdown("#### Mediation · does the effect run through a middle step?")
    st.caption("Predictor (X) = the starting cause or exposure · Mediator (M) = the mechanism that carries X's effect · "
               "Outcome (Y) = the result affected by both. Each site's weeks are compared with its own average. "
               "a = X → M, b = M → Y holding X fixed, c = total effect of X on Y, c′ = direct effect of X on Y. Coefficients are "
               "standardized (in standard deviations); * p < 0.05, ** p < 0.01. The indirect effect a×b is significant when "
               "its 95% bootstrap interval excludes zero.")
    labels = {k: v[0] for k, v in DRIVERS.items()}
    c1, c2, c3 = st.columns(3)
    preset = [f"{labels[x]} → {labels[m]} → {OUTCOMES[y][0]}" for x, m, y in MEDIATION_PATHS] + ["Custom…"]
    pick = st.selectbox("Pathway (X → M → Y)", preset, key="xs_med_pick")
    if pick == "Custom…":
        x = c1.selectbox("Predictor (X)", list(DRIVERS), format_func=lambda k: labels[k], key="xs_med_x")
        m = c2.selectbox("Mediator (M)", [k for k in DRIVERS if k != x], format_func=lambda k: labels[k], key="xs_med_m")
        y = c3.selectbox("Outcome (Y)", list(OUTCOMES), format_func=lambda k: OUTCOMES[k][0], key="xs_med_y")
    else:
        x, m, y = MEDIATION_PATHS[preset.index(pick)]
    r = mediation(p, x, m, y, lag)
    with st.container(**BORDER):
        if r is None:
            st.info("Not enough full site-weeks with the exposure, mediator and outcome all present.")
        else:
            st.markdown(mediation_svg(r), unsafe_allow_html=True)
            st.markdown(mediation_sentence(r))
            st.caption(f"{r['n']:,} site-weeks across {r['sites']} sites · "
                       + ("injuries the same week" if lag == 0 else "injuries the following week") + ".")
    with st.expander("All starter pathways"):
        rows = []
        for x_, m_, y_ in MEDIATION_PATHS:
            rr = mediation(p, x_, m_, y_, lag, boots=300)
            if rr:
                rows.append({"Predictor (X)": labels[x_], "Mediator (M)": labels[m_], "Outcome (Y)": OUTCOMES[y_][0],
                             "a": rr["a"], "b": rr["b"], "Total c": rr["c"], "Direct c′": rr["cp"], "Indirect a×b": rr["ind"],
                             "95% low": rr["ind_lo"], "95% high": rr["ind_hi"],
                             "% mediated": rr["pct_med"], "Site-weeks": rr["n"]})
        if rows:
            NC = st.column_config.NumberColumn
            st.dataframe(pd.DataFrame(rows), hide_index=True, **g("STRETCH"), column_config={
                k: NC(format="%+.3f") for k in ["a", "b", "Total c", "Direct c′", "Indirect a×b", "95% low", "95% high"]}
                | {"% mediated": NC(format="%.0f%%"), "Site-weeks": NC(format="%d")})
        else:
            st.caption("Not enough data yet.")


# ---------------------------------------------------------------------------------------------------------------
# Evidence ledger: every driver x outcome x timing framed as a research question, tested with a Poisson regression
# (hours offset, site fixed effects, annual seasonality and conservative site/week-cluster sensitivity (few-site results remain exploratory)) and corrected for false discoveries (Benjamini-Hochberg).
# ---------------------------------------------------------------------------------------------------------------
ALPHA = 0.05            # significance level for each test and the false-discovery rate across the whole ledger
LEDGER_MIN_N = 30       # site-weeks a test needs before it is run
VERDICTS = ["Exploratory after correction", "Suggestive", "Not supported", "Too little data"]
VERDICT_HELP = {
    "Exploratory after correction": f"Exploratory p < {ALPHA} after cluster sensitivity and the Benjamini–Hochberg false-discovery correction across every test.",
    "Suggestive": f"p < {ALPHA} on its own, but not after correcting for the number of tests. Treat as a lead, not a finding.",
    "Not supported": f"p ≥ {ALPHA}: no reliable relationship once site and hours are accounted for.",
    "Too little data": f"Fewer than {LEDGER_MIN_N} site-weeks with the driver and the outcome, or the driver doesn't vary yet "
                       "(for example MET share before the plan schedules any MET).",
}


def bh_qvalues(p):
    """Benjamini–Hochberg adjusted p-values (q). NaNs stay NaN and don't count toward the number of tests."""
    p = np.asarray(p, float)
    q = np.full(p.shape, np.nan)
    ok = np.isfinite(p)
    m = int(ok.sum())
    if m == 0:
        return q
    pv = p[ok]
    order = np.argsort(pv)
    ranked = pv[order] * m / np.arange(1, m + 1)
    ranked = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(m)
    out[order] = np.clip(ranked, 0, 1)
    q[ok] = out
    return q


def poisson_fe(y, log_h, x, groups, iters=50, tol=1e-9):
    r = gov.poisson_fit(y, log_h, x, groups, min_n=LEDGER_MIN_N)
    return r["b"], r["se"], r["p"], r["phi"]


def _rq_text(d, oc, lag):
    lab, olab = _lc(DRIVERS[d][0]), _lc(OUTCOMES[oc][0])
    when = "the same week" if lag == 0 else "the following week"
    return (f"Do weeks with higher {lab} than the site's normal have a different {olab} {when}?",
            f"{DRIVERS[d][0]} has no relationship with {olab} {when} once site and hours worked are accounted for (IRR = 1).",
            f"{OUTCOMES[oc][0]} {when} changes with {lab} (IRR ≠ 1).")


def evidence_ledger(p, lags=(0, 1), outcomes=None, drivers=None):
    """One row per research question (driver x outcome x timing): within-site r, Poisson IRR per 1 SD of the
    driver's week-to-week swing at a site, cluster-sensitive p, Benjamini–Hochberg q across every test in the ledger, exploratory verdict. Cluster sensitivity and annual seasonality are included; few-site uncertainty remains."""
    outcomes = outcomes or list(OUTCOMES)
    drivers = [d for d in (drivers or list(DRIVERS)) if d in p.columns]
    rows = []
    for lag in lags:
        q = _shift_outcome(p, lag)
        if q.empty:
            continue
        for oc in outcomes:
            cols = OUTCOMES[oc][1]
            cnt = q[cols].sum(axis=1, min_count=1).to_numpy(float)
            hrs = q["TOTAL_HOURS"].to_numpy(float)
            ct = correlations(q, oc, 0, drivers)        # q is already shifted
            ct = ct.set_index("DRIVER") if len(ct) else ct
            for d in drivers:
                x = q[d].to_numpy(float)
                m = np.isfinite(x) & np.isfinite(cnt) & (hrs > 0)
                n = int(m.sum())
                rq, h0, ha = _rq_text(d, oc, lag)
                row = {"LAG": lag, "DRIVER": d, "LABEL": DRIVERS[d][0], "GROUP": DRIVERS[d][1], "OUTCOME": oc,
                       "OUT_LABEL": OUTCOMES[oc][0], "RQ": rq, "H0": h0, "HA": ha, "N": n, "SITES": 0,
                       "R": np.nan, "IRR": np.nan, "IRR_LO": np.nan, "IRR_HI": np.nan, "P": np.nan, "DISP": np.nan}
                if len(ct) and d in ct.index:
                    rr = ct.loc[d]
                    row["R"] = rr["R_WITHIN"] if rr["SITES"] > 1 else rr["RHO"]
                if n >= LEDGER_MIN_N:
                    s = q.loc[m, "SITE"].astype(str).to_numpy()
                    xs = x[m]
                    dm = xs - pd.Series(xs).groupby(s).transform("mean").to_numpy() if len(set(s)) > 1 else xs - xs.mean()
                    sd = float(np.std(dm))
                    if sd > 0:
                        fit = gov.poisson_fit(cnt[m], np.log(hrs[m]), (xs-xs.mean())/sd, s, q.loc[m, "WK"], min_n=LEDGER_MIN_N)
                        b, se, pv, phi = fit["b"], fit["se"], fit["p"], fit["phi"]
                        row.update(QUASI_SE=fit["quasi_se"], CLUSTER_SE=se, CLUSTERS=fit["clusters"], CONVERGED=fit["converged"], METHOD=fit["method"])
                        crit = fit["crit"]
                        if np.isfinite(b):
                            row.update({"IRR": math.exp(b), "IRR_LO": math.exp(np.clip(b - crit * se, -30, 30)), "IRR_HI": math.exp(np.clip(b + crit * se, -30, 30)),
                                        "P": pv, "DISP": phi, "SITES": len(set(s))})
                rows.append(row)
    led = pd.DataFrame(rows)
    if led.empty:
        return led
    led["Q"] = bh_qvalues(led["P"])
    led["VERDICT"] = np.select(
        [~np.isfinite(led["P"]), led["Q"] < ALPHA, led["P"] < ALPHA],
        ["Too little data", "Exploratory after correction", "Suggestive"], "Not supported")
    led["EFFECT"] = (led["IRR"] - 1) * 100
    led.attrs["tests"] = int(np.isfinite(led["P"]).sum())
    return led


@gov.viewer_cache(st, lambda: g("_viewer_context")(), ttl=3600, show_spinner=False)
def ledger_cached(basis, wf_on, sites, _rev=4):
    """Ledger for a set of sites (cached; the panel itself is cached too)."""
    p_all, _, _ = panel_cached(basis, wf_on)
    p = p_all[p_all["SITE"].isin(sites)]
    led = evidence_ledger(p)
    return led, led.attrs.get("tests", 0)


def effect_text(row):
    """'+12% (95% +5% to +19%) per 1 SD' style text for an IRR row."""
    if not np.isfinite(row.get("IRR", np.nan)):
        return "—"
    return (f"{(row['IRR'] - 1) * 100:+.0f}% (95% {(row['IRR_LO'] - 1) * 100:+.0f}% to {(row['IRR_HI'] - 1) * 100:+.0f}%)")


def ledger_sentence(row):
    lab, olab = _lc(row["LABEL"]), _lc(row["OUT_LABEL"])
    when = "that week" if row["LAG"] == 0 else "the next week"
    return (f"A typical week-to-week rise in {lab} (1 SD) goes with {olab} {effect_text(row)} {when} "
            f"(q = {row['Q']:.3f}, {int(row['N'])} site-weeks).")


def top_evidence(led, k=3, outcomes=("INJ_RATE", "MSDR", "MOER"), verdicts=("Exploratory after correction",)):
    """Best-supported rows, one per driver, strongest effect first."""
    if led is None or led.empty:
        return led
    t = led[led["VERDICT"].isin(verdicts) & led["OUTCOME"].isin(outcomes)].copy()
    if t.empty:
        return t
    t["_A"] = np.abs(np.log(t["IRR"]))
    t = t.sort_values(["Q", "_A"], ascending=[True, False]).drop_duplicates("DRIVER")
    return t.sort_values("_A", ascending=False).head(k)


def driver_now(p, sites, drivers, end_wk=None, weeks=4):
    """Where each driver stands for `sites`: the last `weeks` full site-weeks vs the prior 13, as the share of
    the sites' own history it exceeds (percentile) and the change in within-site SDs."""
    d = p[p["SITE"].isin(sites)]
    if end_wk is not None:
        d = d[d["WK"] <= pd.Timestamp(end_wk)]
    if d.empty:
        return pd.DataFrame()
    wks = sorted(d["WK"].unique())
    recent, prior = wks[-weeks:], wks[-(weeks + 13):-weeks]
    a = scope_weekly(d)
    out = []
    for k in drivers:
        if k not in a.columns:
            continue
        s = a.set_index("WK")[k].astype(float)
        cur, base = s.reindex(recent).mean(), s.reindex(prior)
        hist = s.dropna()
        sd = float(base.std()) if base.notna().sum() >= 4 else np.nan
        pct = float((hist < cur).mean() * 100) if len(hist) >= 8 and np.isfinite(cur) else np.nan
        out.append({"DRIVER": k, "NOW": cur, "NORMAL": base.mean(), "Z": (cur - base.mean()) / sd if sd and sd > 0 else np.nan,
                    "PCT": pct, "WEEKS": len(recent)})
    return pd.DataFrame(out)


def _ord(n):
    n = int(round(n))
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _unit_txt(unit):
    return {"pts": " pts", "%": "%", "h": " h"}.get(unit, "")


def _arrow_tone(z, irr):
    """Is the driver moving in the direction that goes with more injuries?"""
    if not np.isfinite(z) or not np.isfinite(irr):
        return "", "—"
    risky = (z > 0 and irr > 1) or (z < 0 and irr < 1)
    if abs(z) < 0.5:
        return "", "near normal"
    return ("bad" if risky else "good"), ("toward more injuries" if risky else "toward fewer injuries")


def no_lift_triggers(p, ow, th, basis=200_000):
    """Ops triggers that fired 4+ FC-weeks with no injury lift: the fired-vs-not ratio's 95% interval reaches 1.00 or below."""
    tc = trigger_compare(p, ow, th, basis, 0)
    if tc.empty:
        return tc
    tc = tc[tc["FIRED"] >= MIN_FIRED].copy()
    lift = (tc["INJ_RATE_LO"] > 1) | (tc["MSDR_LO"] > 1) | (tc["MOER_LO"] > 1)
    tc["LIFT"] = lift
    return tc


# ---------------------------------------------------------------------------------------------------------------
# App: evidence strip (Overview), driver panel (Site), ledger (Ops x Safety)
# ---------------------------------------------------------------------------------------------------------------
def _panel_for_app():
    basis = g("basis")
    wf_on = bool(g("wf_is_warm")() or st.session_state.get("wf_load"))
    p_all, notes, dept_h = panel_cached(basis, wf_on)
    return p_all, basis, wf_on


INJ_PICK = {"Recordable": "TRIR", "MSD": "MSDR", "MOE": "MOER", "Lift & carry": "LIFTR",
            "Reach & repetitive": "REACHR"}   # injury measures people pick from; Recordable first
_VERDICT_ORDER = {"Exploratory after correction": 0, "Suggestive": 1, "Not supported": 2, "Too little data": 3}
_VERDICT_TAG = {"Exploratory after correction": " · holds", "Suggestive": " · suggestive"}


def _pick_measure(key, help_=None):
    lab = g("seg")("Injury measure", list(INJ_PICK), "Recordable", key=key,
                   help=help_ or "Recordable, MSD or MOE rate, or injuries by how they happened: lift & carry "
                                  "(lifting, lowering, carrying) or reach & repetitive (overreaching, repetitive motion, "
                                  "bending and twisting). Changes the list and the chart.")
    return lab, INJ_PICK[lab]


def _best_row(led, d, oc):
    """The ledger row for a driver and outcome at its better-supported timing (lowest p)."""
    t = led[(led["DRIVER"] == d) & (led["OUTCOME"] == oc)] if led is not None and len(led) else pd.DataFrame()
    if t.empty:
        return None
    t = t.assign(_P=t["P"].fillna(9)).sort_values(["_P", "LAG"])
    return t.iloc[0].to_dict()


def driver_options(led, oc):
    """Every ops metric, best-supported for this injury measure first."""
    rows = [(_best_row(led, d, oc), d) for d in DRIVERS]
    rows.sort(key=lambda x: (_VERDICT_ORDER.get((x[0] or {}).get("VERDICT"), 3),
                             (x[0] or {}).get("P", 9) if np.isfinite((x[0] or {}).get("P", np.nan)) else 9))
    return [d for _, d in rows]


def pair_explorer(p_scope, led, oc, key, weeks=26, end_wk=None):
    """App: pick any ops metric and the timing, then chart it with the chosen injury measure on one axis."""
    opts = driver_options(led, oc)
    tags = {d: _VERDICT_TAG.get((_best_row(led, d, oc) or {}).get("VERDICT"), "") for d in opts}
    c1, c2 = st.columns([2.4, 1.4])
    d = c1.selectbox("Ops metric", opts, key=f"{key}_drv_{oc}", format_func=lambda k: DRIVERS[k][0] + tags[k],
                     help="Every ops, productivity, labor and rotation measure. Best-supported for this injury measure first.")
    br = _best_row(led, d, oc)
    with c2:
        timing = g("seg")("Injuries", ["Same week", "Next week"], "Next week" if br and br["LAG"] else "Same week",
                          key=f"{key}_lag_{oc}_{d}", help="Next week = the metric this week against injuries the following week.")
    lag = 0 if timing == "Same week" else 1
    row = led[(led["DRIVER"] == d) & (led["OUTCOME"] == oc) & (led["LAG"] == lag)] if led is not None and len(led) else pd.DataFrame()
    if len(row):
        r = row.iloc[0].to_dict()
        st.markdown(f"Ledger: **{r['VERDICT'].lower()}**"
                    + (f" · {effect_text(r)} per typical rise · q = {r['Q']:.3f}" if np.isfinite(r.get("IRR", np.nan)) else ""))
    show_paired(p_scope, d, oc, lag, weeks=weeks, end_wk=end_wk)


def overview_strip(pool):
    """'What's moving injuries' strip on the OVSR overview: drivers that hold up in the ledger and where they stand now."""
    BORDER = g("BORDER")
    p_all, basis, wf_on = _panel_for_app()
    p = p_all[p_all["SITE"].isin(pool)]
    if len(p) < LEDGER_MIN_N:
        return
    led, n_tests = ledger_cached(basis, wf_on, tuple(sorted(p["SITE"].unique())))
    with st.container(**BORDER):
        st.markdown(f"**What's moving injuries** · {n_tests} of {len(led)} research questions tested on {len(p):,} site-weeks")
        mlab, oc = _pick_measure("ov_ev_out")
        top = top_evidence(led, k=4, outcomes=(oc,))
        weak = top is None or top.empty
        if weak:
            top = top_evidence(led, k=3, outcomes=(oc,), verdicts=("Suggestive",))
        if top is None or top.empty:
            st.markdown(f"No ops metric holds up or is even suggestive for the {_lc(mlab)} rate yet. OVSR status still "
                        "drives the actions; the Ops × Safety tab has the full evidence ledger.")
        else:
            if weak:
                st.markdown(f"Nothing holds up after correction for the {_lc(mlab)} rate yet. Suggestive leads:")
            now = driver_now(p, list(p["SITE"].unique()), list(top["DRIVER"]))
            now = now.set_index("DRIVER") if len(now) else now
            lines = []
            for r in top.to_dict("records"):
                z = now.loc[r["DRIVER"], "Z"] if len(now) and r["DRIVER"] in now.index else np.nan
                pct = now.loc[r["DRIVER"], "PCT"] if len(now) and r["DRIVER"] in now.index else np.nan
                tone, word = _arrow_tone(z, r["IRR"])
                cur = (f" Last 4 weeks: {word}" + (f" ({_ord(pct)} percentile of these sites' weeks)" if np.isfinite(pct) else "")
                       + ".") if np.isfinite(z) else ""
                lines.append(f"- **{r['LABEL']} → {_lc(r['OUT_LABEL'])}**: {effect_text(r)} per typical rise "
                             f"{'the same week' if r['LAG'] == 0 else 'the next week'}.{cur}")
            st.markdown("\n".join(lines))
        st.markdown("**See it by week** · any ops metric with the injury measure above")
        pair_explorer(p, led, oc, key="ov_ev", weeks=26)
        hol, _, _, _ = g("load_plan")()
        ow = g("ops_weeks")(hol)
        nl = no_lift_triggers(p, ow[ow["SITE"].isin(pool)], g("ovsr_th_current")(), basis)
        if len(nl):
            quiet = nl[~nl["LIFT"]]["TITLE"].tolist()
            if quiet:
                st.markdown("**Triggers with no injury lift so far:** " + ", ".join(quiet) + ". They still flag ops variance; "
                            "don't read them as injury warnings on their own.")
        st.caption("Each % is the change in the injury rate for a typical week-to-week rise in the metric at a site "
                   "(Poisson regression with site fixed effects and hours, annual seasonality and conservative site/week-cluster sensitivity (few-site results remain exploratory)). The list shows relationships "
                   f"that survive the Benjamini–Hochberg false-discovery correction (q < {ALPHA}); suggestive ones only when none "
                   "do. Evidence, not cause; OVSR status still drives the actions.")


def site_drivers(site, wk=None):
    """Site tab: the drivers that hold up network-wide, this site's own link, and where the site stands now."""
    p_all, basis, wf_on = _panel_for_app()
    pool = sorted(g("scope")(_daily_fact(), g("sel_regions"), g("sel_sites"))["SITE"].unique())
    p_net = p_all[p_all["SITE"].isin(pool)] if len(pool) > 1 else p_all
    ps = p_all[p_all["SITE"] == site]
    st.markdown("#### What's moving injuries here")
    if ps.empty or len(p_net) < LEDGER_MIN_N:
        st.caption("No full site-weeks with ops and labor data for this site yet (the panel covers FC and Rx sites in the "
                   "daily OBR table with 2,000+ hours a week).")
        return
    led, _ = ledger_cached(basis, wf_on, tuple(sorted(p_net["SITE"].unique())))
    mlab, oc = _pick_measure("site_ev_out")
    tbl = site_driver_table(ps, led, wk, outcomes=(oc,))
    if tbl.empty:
        st.caption(f"No ops metric holds up or is suggestive for the {_lc(mlab)} rate network-wide yet.")
    else:
        NC = st.column_config.NumberColumn
        show = tbl[["LABEL", "WHEN", "NET", "VERDICT", "SITE_R", "NOW_TXT", "NORMAL_TXT", "MOVE"]]
        st.dataframe(show, hide_index=True, **g("STRETCH"), column_config={
            "LABEL": "Ops metric", "WHEN": "Timing",
            "NET": st.column_config.TextColumn("Network effect", help="Change in the injury rate for a typical week-to-week "
                                                                       "rise in the metric (Poisson, site fixed effects, 95% interval)."),
            "VERDICT": st.column_config.TextColumn("Evidence", help="; ".join(f"{k}: {v}" for k, v in VERDICT_HELP.items())),
            "SITE_R": NC(f"{site}'s own r", format="%+.2f", help="Spearman r between the metric and the injury rate over this "
                                                                 "site's own weeks. Few weeks, so read it as direction only."),
            "NOW_TXT": st.column_config.TextColumn("Last 4 weeks"), "NORMAL_TXT": st.column_config.TextColumn("Prior 13 weeks"),
            "MOVE": st.column_config.TextColumn("Direction now")})
        watch = tbl[tbl["TONE"] == "bad"]
        if len(watch):
            st.markdown("**Watch here:** " + "; ".join(_watch_bit(r) for r in watch.head(3).to_dict("records"))
                        + f". Each is moving the way that goes with a higher {_lc(mlab)} rate across the network.")
    st.markdown(f"**See it by week at {site}** · any ops metric with the injury measure above")
    pair_explorer(ps, led, oc, key="site_ev", weeks=26, end_wk=wk)
    st.caption(f"Metrics that hold up (or are suggestive) across {p_net['SITE'].nunique()} sites in the evidence ledger, "
               f"with {site}'s own last 4 full weeks against its prior 13. Evidence, not cause; the triggers above still set "
               "the status and the actions.")


def _watch_bit(r):
    way = "up" if np.isfinite(r.get("Z", np.nan)) and r["Z"] > 0 else "down"
    return f"{r['LABEL']} {way} at {r['NOW_TXT']} vs {r['NORMAL_TXT']} normal"


def site_driver_table(ps, led, wk=None, k=6, outcomes=("INJ_RATE", "MSDR", "MOER")):
    """Rows for one site's driver panel (shared by the app and the snapshot)."""
    top = top_evidence(led, k=k, outcomes=outcomes, verdicts=("Exploratory after correction", "Suggestive"))
    if top is None or top.empty:
        return pd.DataFrame()
    end = None if wk is None else pd.Timestamp(wk)
    now = driver_now(ps, list(ps["SITE"].unique()), list(top["DRIVER"]), end_wk=end)
    now = now.set_index("DRIVER") if len(now) else now
    rows = []
    for r in top.to_dict("records"):
        d = r["DRIVER"]
        lab, grp, unit, f_, note = DRIVERS[d]
        q = _shift_outcome(ps, r["LAG"])
        x, y = q[d].to_numpy(float), q[r["OUTCOME"]].to_numpy(float)
        m = np.isfinite(x) & np.isfinite(y)
        site_r = _pearson(_avg_rank(x[m]), _avg_rank(y[m])) if m.sum() >= 12 else np.nan
        nv = now.loc[d] if len(now) and d in now.index else None
        z = nv["Z"] if nv is not None else np.nan
        tone, word = _arrow_tone(z, r["IRR"])
        u = _unit_txt(unit)
        rows.append({"DRIVER": d, "LABEL": lab, "OUTCOME": r["OUTCOME"], "LAG": r["LAG"],
                     "OUT_LABEL": OUT_SHORT.get(r["OUTCOME"], r["OUT_LABEL"]),
                     "WHEN": "Same week" if r["LAG"] == 0 else "Next week", "NET": effect_text(r), "VERDICT": r["VERDICT"],
                     "SITE_R": site_r, "NOW_TXT": _fmt(nv["NOW"], f_, u) if nv is not None else "—",
                     "NORMAL_TXT": _fmt(nv["NORMAL"], f_, u) if nv is not None else "—",
                     "Z": z, "MOVE": word, "TONE": tone, "IRR": r["IRR"]})
    return pd.DataFrame(rows)


def render_ledger(p):
    """Evidence ledger section of the Ops x Safety tab."""
    st.caption("Exploratory associations. Site/week cluster sensitivity and annual seasonality inform uncertainty. Few-site results can remain unstable; no out-of-time forecast or causal effect is established.")
    basis = g("basis")
    wf_on = bool(g("wf_is_warm")() or st.session_state.get("wf_load"))
    led, n_tests = ledger_cached(basis, wf_on, tuple(sorted(p["SITE"].unique())))
    st.markdown("#### Evidence ledger · every research question tested")
    if led is None or led.empty:
        st.info("Not enough site-weeks to test.")
        return
    cnt = led["VERDICT"].value_counts()
    k = g("kpi_cols")(4)
    for col, v in zip(k, VERDICTS):
        col.metric(v, f"{int(cnt.get(v, 0)):,}", help=VERDICT_HELP[v], **g("BORDER"))
    st.caption(f"{n_tests} of {len(led)} questions tested ({len(DRIVERS)} drivers × {len(OUTCOMES)} outcomes × same week and "
               "next week; the rest have too little data or a driver that doesn't vary yet). Each one asks "
               "a research question (RQ) with a null hypothesis (H0: no relationship once site and hours are accounted for) "
               "and an alternative (HA: the outcome changes with the driver). Test = Poisson regression of the outcome count "
               "with an hours offset and site fixed effects, annual seasonality and conservative site/week-cluster sensitivity (few-site results remain exploratory); effect = change in the rate for a typical "
               f"week-to-week rise in the driver (1 SD within site). Significance α = {ALPHA}; q = Benjamini–Hochberg "
               f"adjusted p, controlling false discoveries at {ALPHA:.0%} across all {n_tests} tests. Next week = association with the following week. Future predictive performance is not established.")
    c1, c2, c3 = st.columns([1.6, 1.4, 1.2])
    vsel = c1.multiselect("Evidence", VERDICTS, default=["Exploratory after correction", "Suggestive"], key="xs_led_v")
    osel = c2.multiselect("Outcome", list(OUTCOMES), format_func=lambda o: OUTCOMES[o][0], key="xs_led_o")
    tsel = g("seg")("Timing", ["Both", "Same week", "Next week"], "Both", key="xs_led_t")
    t = led[led["VERDICT"].isin(vsel or VERDICTS)]
    if osel:
        t = t[t["OUTCOME"].isin(osel)]
    if tsel != "Both":
        t = t[t["LAG"] == (0 if tsel == "Same week" else 1)]
    t = t.assign(WHEN=np.where(t["LAG"] == 0, "Same week", "Next week"), EFF=[effect_text(r) for r in t.to_dict("records")])
    t = t.sort_values(["Q", "P"])
    NC = st.column_config.NumberColumn
    st.dataframe(t[["RQ", "LABEL", "OUT_LABEL", "WHEN", "R", "EFF", "P", "Q", "VERDICT", "N"]], hide_index=True, **g("STRETCH"),
                 column_config={"RQ": st.column_config.TextColumn("Research question", width="large"), "LABEL": "Driver",
                                "OUT_LABEL": "Outcome", "WHEN": "Timing", "R": NC("Within-site r", format="%+.2f"),
                                "EFF": st.column_config.TextColumn("Effect per typical rise (95%)"),
                                "P": NC("p", format="%.4f"), "Q": NC("q (BH)", format="%.4f"), "VERDICT": "Evidence",
                                "N": NC("Site-weeks", format="%d")})
    with st.expander("Hypotheses for the rows above (H0 / HA)"):
        st.markdown("\n".join(f"- **{r['RQ']}**  \n  H0: {r['H0']}  \n  HA: {r['HA']}" for r in t.head(25).to_dict("records"))
                    or "No rows.")
    g("download")(t.drop(columns=["_A"], errors="ignore"), "ovsr_evidence_ledger.csv", "dl_xs_ledger")
    with st.expander("Out-of-time sensitivity check"):
        vd=st.selectbox("Validation driver", [d for d in DRIVERS if d in p], format_func=lambda d:DRIVERS[d][0], key="validation_driver")
        vo=st.selectbox("Validation outcome", list(OUTCOMES), format_func=lambda o:OUTCOMES[o][0], key="validation_outcome")
        if st.button("Run rolling-origin check", key="run_rolling_validation"):
            with st.spinner("Checking later weeks against a flat-rate baseline…"):
                bt=gov.conditional_backtest(p,vd,OUTCOMES[vo][1],lag=1)
            if bt.empty:
                st.info("Insufficient complete history for this check.")
            else:
                st.dataframe(bt,hide_index=True,**g("STRETCH"))
                g("download")(bt,"ovsr_conditional_backtest.csv","dl_conditional_bt")
        st.caption("The model is refitted before each origin. Target hours and recorded driver values are observed, not forecast. This checks conditional associations only. Archived plan error, source revisions, subgroup calibration and a full predictive validation remain separate requirements.")


# ---------------------------------------------------------------------------------------------------------------
# Snapshot (HTML): evidence strip, site driver table, ledger section
# ---------------------------------------------------------------------------------------------------------------
def _ledger_counts(led):
    n_q = len(led) if led is not None else 0
    n_t = int(np.isfinite(led["P"]).sum()) if n_q else 0
    return n_q, n_t


def _measure_tabs(grp):
    """Recordable / MSD / MOE switch for a snapshot section (Recordable shown first)."""
    on = " class='on'"
    return ("<div class='seg' role='tablist'>" + "".join(
        f"<button{on if i == 0 else ''} data-grpbtn='{grp}:{oc}'>{E(lab)}</button>"
        for i, (lab, oc) in enumerate(INJ_PICK.items())) + "</div>")


def _measure_pane(grp, oc, body):
    return f"<div data-pgrp='{grp}' data-pval='{oc}'{'' if oc == 'TRIR' else ' hidden'}>{body}</div>"


def _chart_rows(led, oc, top, n=2):
    """Rows to chart for one injury measure: the listed ones, else the best-supported metrics."""
    rows = top.head(n).to_dict("records") if top is not None and len(top) else []
    if len(rows) < n:
        seen = {r["DRIVER"] for r in rows}
        for d in driver_options(led, oc):
            if d in seen:
                continue
            br = _best_row(led, d, oc)
            if br is not None and np.isfinite(br.get("P", np.nan)):
                rows.append({"DRIVER": d, "OUTCOME": oc, "LAG": int(br["LAG"])})
            if len(rows) >= n:
                break
    return rows


def evidence_strip_html(ctx, sites):
    """'What's moving injuries' block for the snapshot Overview: a pane per injury measure (Recordable first)."""
    led = ctx.get("ledger")
    if led is None or led.empty:
        return ""
    p = ctx["panel"]
    ps = p[p["SITE"].isin(sites)]
    n_q, n_t = _ledger_counts(led)
    s = ["<h2>What's moving injuries</h2>", _measure_tabs("evo")]
    for mlab, oc in INJ_PICK.items():
        top = top_evidence(led, k=4, outcomes=(oc,))
        weak = top is None or top.empty
        if weak:
            top = top_evidence(led, k=3, outcomes=(oc,), verdicts=("Suggestive",))
        b = ["<div class='call'>"]
        if top is None or top.empty:
            b.append(f"<p>No ops metric holds up or is even suggestive for the {E(_lc(mlab))} rate yet.</p>")
        else:
            if weak:
                b.append(f"<p>Nothing holds up after correction for the {E(_lc(mlab))} rate yet. Suggestive leads:</p>")
            now = driver_now(ps, sites, list(top["DRIVER"]), end_wk=ctx["lw"]) if len(ps) else pd.DataFrame()
            now = now.set_index("DRIVER") if len(now) else now
            items = []
            for r in top.to_dict("records"):
                lab, _g, unit, f_, _n = DRIVERS[r["DRIVER"]]
                nv = now.loc[r["DRIVER"]] if len(now) and r["DRIVER"] in now.index else None
                tone, word = _arrow_tone(nv["Z"] if nv is not None else np.nan, r["IRR"])
                here = ""
                if nv is not None and np.isfinite(nv["NOW"]):
                    u = _unit_txt(unit)
                    here = (f" Last 4 weeks here: <b class='{tone}'>{E(word)}</b> ({fnum(nv['NOW'], f_, u)} vs "
                            f"{fnum(nv['NORMAL'], f_, u)} normal).")
                items.append(f"<li><b>{E(lab)} → {E(_lc(r['OUT_LABEL']))}</b>: {E(effect_text(r))} per typical rise "
                             f"{'the same week' if r['LAG'] == 0 else 'the next week'}.{here}</li>")
            b.append("<ul>" + "".join(items) + "</ul>")
        b.append(paired_block_html(ps, _chart_rows(led, oc, top), ctx["lw"]))
        b.append("</div>")
        s.append(_measure_pane("evo", oc, "".join(b)))
    nl = ctx.get("nolift")
    if nl is not None and len(nl):
        quiet = nl[~nl["LIFT"]]["TITLE"].tolist()
        if quiet:
            s.append(f"<p><b>Triggers with no injury lift so far:</b> {E(', '.join(quiet))}. They still flag ops variance; "
                     "don't read them as injury warnings on their own.</p>")
    s.append(f"<p class='cap'>Pick Recordable, MSD or MOE above. {n_t} of {n_q} research questions tested across "
             f"{ctx['panel_sites']} sites. Each % is the change in the injury rate for a typical week-to-week rise in the "
             "metric at a site (Poisson regression with site fixed effects and hours). The list shows relationships that "
             f"survive the Benjamini–Hochberg false-discovery correction (q &lt; {ALPHA}); suggestive ones only when none do. "
             "Evidence, not cause; OVSR status still drives the actions. Full ledger on the Ops × Safety tab.</p>")
    return "".join(s)


def site_drivers_html(ctx, site):
    """'What's moving injuries here' for one site in the snapshot: a pane per injury measure (Recordable first)."""
    p = ctx["panel"]
    ps = p[p["SITE"] == site]
    led = ctx.get("ledger")
    if ps.empty or led is None or led.empty:
        return ""
    s = ["<h2>What's moving injuries here</h2>", _measure_tabs("evs")]
    for mlab, oc in INJ_PICK.items():
        t = site_driver_table(ps, led, ctx["lw"], outcomes=(oc,))
        b = []
        if t.empty:
            b.append(f"<p class='cap'>No ops metric holds up or is suggestive for the {E(_lc(mlab))} rate network-wide yet.</p>")
        else:
            t["MOVE_H"] = [f"<b class='{tn}'>{E(w)}</b>" for tn, w in zip(t["TONE"], t["MOVE"])]
            b.append(html_table(t, [
                ("LABEL", "Ops metric", None), ("WHEN", "Timing", None),
                ("NET", "Network effect (95%)", lambda v: E(v)), ("VERDICT", "Evidence", None),
                ("SITE_R", f"{site}'s own r", lambda v: fnum(v, "+.2f")), ("NOW_TXT", "Last 4 weeks", lambda v: E(v)),
                ("NORMAL_TXT", "Prior 13 weeks", lambda v: E(v)), ("MOVE_H", "Direction now", "html")]))
            w = t[t["TONE"] == "bad"]
            if len(w):
                b.append("<p><b>Watch here:</b> " + E("; ".join(_watch_bit(r) for r in w.head(3).to_dict("records")))
                         + f". Each is moving the way that goes with a higher {E(_lc(mlab))} rate across the network.</p>")
        b.append(paired_block_html(ps, _chart_rows(led, oc, t if len(t) else None), ctx["lw"]))
        s.append(_measure_pane("evs", oc, "".join(b)))
    s.append(f"<p class='cap'>Pick Recordable, MSD or MOE above. Metrics that hold up (or are suggestive) in the network "
             f"evidence ledger, with {E(site)}'s last 4 full weeks against its prior 13. The site's own r uses only its weeks, "
             "so read it as direction. Evidence, not cause; the triggers above set the status and the actions.</p>")
    return "".join(s)


def scope_ledger(ctx, sites, kind):
    """Ledger for a snapshot scope: the network one for 'All sites', else built once per scope and kept in ctx."""
    if kind == "net":
        return ctx.get("ledger")
    cache = ctx.setdefault("ledger_by", {})
    key = tuple(sorted(sites))
    if key not in cache:
        p = ctx["panel"]
        cache[key] = evidence_ledger(p[p["SITE"].isin(sites)])
    return cache[key]


def ledger_html(ctx, sites, kind):
    """Evidence ledger section for the snapshot's Ops x Safety tab (this scope's sites)."""
    led = scope_ledger(ctx, sites, kind)
    if led is None or led.empty:
        return ""
    n_q, n_t = _ledger_counts(led)
    cnt = led["VERDICT"].value_counts()
    s = ["<h2>Evidence ledger · every research question tested</h2><div class='kpis'>"
         + "".join(kpi_card(v, fnum(cnt.get(v, 0), ",.0f"), None, "", VERDICT_HELP[v]) for v in VERDICTS) + "</div>"]
    t = led[led["VERDICT"].isin(["Exploratory after correction", "Suggestive"])].sort_values(["Q", "P"]).head(30).copy()
    if len(t):
        t["WHEN"] = np.where(t["LAG"] == 0, "Same week", "Next week")
        t["EFF"] = [effect_text(r) for r in t.to_dict("records")]
        s.append(html_table(t, [("RQ", "Research question", None), ("EFF", "Effect per typical rise (95%)", lambda v: E(v)),
                                ("P", "p", lambda v: fnum(v, ".4f")), ("Q", "q (BH)", lambda v: fnum(v, ".4f")),
                                ("VERDICT", "Evidence", None), ("N", "Site-weeks", lambda v: fnum(v, ",.0f"))]))
    else:
        s.append("<p class='cap'>No research question reaches p &lt; 0.05 for these sites.</p>")
    s.append(f"<p class='cap'>{n_t} of {n_q} questions tested ({len(DRIVERS)} drivers × {len(OUTCOMES)} outcomes × same week and next "
             "week; the rest have too little data or a driver that doesn't vary yet). Each asks a research question with "
             "H0: no relationship once site and hours are accounted for, and HA: the outcome changes with the driver. Test = "
             "Poisson regression of the outcome count with an hours offset" + (" and site fixed effects" if len(set(sites)) > 1 else "")
             + ", annual seasonality and conservative site/week-cluster sensitivity (few-site results remain exploratory). Effect = change in the rate for a typical week-to-week rise in the driver (1 SD within "
             f"site). α = {ALPHA}; q = Benjamini–Hochberg adjusted p across all {n_t} tests. Next week = association with the following week. Future predictive performance is not established. Showing "
             "rows that hold after correction or are suggestive.</p>")
    return "".join(s)


# ---------------------------------------------------------------------------------------------------------------
# Injuries and a correlated ops driver in one chart: both indexed to their own normal (SDs), so they share one axis
# ---------------------------------------------------------------------------------------------------------------
PAIR_COLORS = ("#2a78d6", "#e2622f")   # driver, injuries (validated light + dark: lightness, chroma, CVD, contrast)


def paired_weeks(p_scope, driver, outcome, lag=0, weeks=26, end_wk=None, roll=None):
    """Weekly driver and outcome rate for a scope, each as distance from its own average over the shown weeks (SDs).
    For next-week relationships the outcome is the following week's rate, lined up with the driver's week. One site's
    weekly injury counts are small, so a single site is drawn as 4-week rolling values (rates pooled from counts and
    hours); r is always on the weekly values."""
    a = scope_weekly(p_scope) if len(p_scope) else pd.DataFrame()
    if a.empty or driver not in a.columns or outcome not in a.columns:
        return pd.DataFrame(), np.nan
    roll = roll or (4 if p_scope["SITE"].nunique() == 1 else 1)
    s = a.set_index("WK").sort_index()
    s = s.reindex(pd.date_range(s.index.min(), s.index.max(), freq="7D"))   # gaps stay gaps
    basis = g("basis") or 200_000
    cols = OUTCOMES[outcome][1]
    shift = lambda x: x.reindex(s.index + pd.Timedelta(weeks=lag)).to_numpy(float)   # noqa: E731
    out_w, drv_w = shift(s[outcome]), s[driver].to_numpy(float)
    if roll > 1:
        num = s[cols].sum(axis=1, min_count=1).rolling(roll, min_periods=roll).sum()
        den = s["TOTAL_HOURS"].rolling(roll, min_periods=roll).sum()
        out_p, drv_p = shift(num / den.where(den > 0) * basis), s[driver].rolling(roll, min_periods=roll).mean().to_numpy(float)
    else:
        out_p, drv_p = out_w, drv_w
    d = pd.DataFrame({"WK": s.index, "DRV": drv_p, "OUT": out_p, "DRV_W": drv_w, "OUT_W": out_w})
    if end_wk is not None:
        d = d[d["WK"] <= pd.Timestamp(end_wk)]
    d = d.dropna(subset=["DRV"]).tail(weeks).reset_index(drop=True)
    for c in ("DRV", "OUT"):
        sd = d[c].std()
        d[c + "_Z"] = (d[c] - d[c].mean()) / sd if sd and np.isfinite(sd) and sd > 0 else np.nan
    m = d[["DRV_W", "OUT_W"]].dropna()
    r = (float(np.corrcoef(m["DRV_W"], m["OUT_W"])[0, 1])
         if len(m) >= 8 and m["DRV_W"].std() > 0 and m["OUT_W"].std() > 0 else np.nan)
    d.attrs["roll"] = roll
    return d, r


def _pair_labels(driver, outcome, lag):
    return DRIVERS[driver][0], OUTCOMES[outcome][0] + (" · following week" if lag else "")


def paired_caption(d, r, lag, multi, html_=False):
    lt = "&lt;" if html_ else "<"
    return (f"Both lines are each measure's distance from its own average over these {len(d)} weeks, in standard deviations "
            "(0 = normal, +1 = a typical high week), so injuries and the driver share one axis. "
            + ("One site: 4-week rolling values (rates pooled from counts and hours), since one site's weekly injury counts "
               "are small. " if not multi else "")
            + (f"r = {r:+.2f} on the weekly values. " if np.isfinite(r) else "")
            + ("Injuries are the following week's, lined up with the driver's week. " if lag else "")
            + ("Weekly totals for every site in scope; the ledger measures the link within each site. " if multi else "")
            + f"Hover a point for the actual values. Lines that move together point the same way the ledger does; a single "
              f"week {lt} 1 SD apart is noise.")


def paired_chart(d, driver, outcome, lag, height=250):
    """Altair: driver and outcome as two lines on one indexed axis, with a 'normal' rule and value tooltips."""
    dlab, olab = _pair_labels(driver, outcome, lag)
    f_, unit = DRIVERS[driver][3], _unit_txt(DRIVERS[driver][2])
    a = d.assign(SERIES=dlab, Z=d["DRV_Z"], VAL_TXT=[_fmt(v, f_, unit) for v in d["DRV"]])
    b = d.assign(SERIES=olab, Z=d["OUT_Z"], VAL_TXT=[_fmt(v, ".2f") for v in d["OUT"]])
    m = pd.concat([a, b], ignore_index=True)
    m["X"] = m["WK"].dt.strftime("%m/%d")
    m["WEEK"] = "Week of " + m["WK"].dt.strftime("%b %d, %Y")
    order = list(d["WK"].dt.strftime("%m/%d"))
    base = alt.Chart(m).encode(
        x=alt.X("X:O", sort=order, title=None, axis=alt.Axis(labelAngle=-40, ticks=False, domain=False, labelOverlap="greedy")),
        y=alt.Y("Z:Q", title="Distance from normal (SD)", axis=alt.Axis(gridOpacity=0.35, domain=False, ticks=False, format="+.1f")),
        color=alt.Color("SERIES:N", title=None, scale=alt.Scale(domain=[dlab, olab], range=list(PAIR_COLORS)),
                        legend=alt.Legend(orient="top", symbolType="stroke", labelLimit=320)),
        tooltip=[alt.Tooltip("WEEK:N", title=""), alt.Tooltip("SERIES:N", title=""), alt.Tooltip("VAL_TXT:N", title="Value"),
                 alt.Tooltip("Z:Q", title="SD from normal", format="+.2f")])
    rule = alt.Chart(pd.DataFrame({"y": [0.0]})).mark_rule(color=g("RULE_GRAY") or "#8a8f98", strokeDash=[4, 4], strokeWidth=1.2).encode(y="y:Q")
    ch = rule + base.mark_line(strokeWidth=2) + base.mark_point(filled=True, size=45, opacity=1)
    return ch.properties(height=height)


def paired_svg(d, driver, outcome, lag, height=210, width=640):
    """Snapshot (inline SVG) version of paired_chart."""
    dlab, olab = _pair_labels(driver, outcome, lag)
    x = [_wk(w, "%m/%d") for w in d["WK"]]
    return svg_chart(x, [(dlab, list(d["DRV_Z"]), PAIR_COLORS[0]), (olab, list(d["OUT_Z"]), PAIR_COLORS[1])], yfmt="+.1f",
                     rules=[(0, "Normal", "var(--axis)")], height=height, width=width, zero=False,
                     title=f"{dlab} and {olab} by week, distance from normal")


def show_paired(p_scope, driver, outcome, lag, weeks=26, end_wk=None, key_note=""):
    """Render the paired chart + caption in the app (no-op when there's too little data)."""
    d, r = paired_weeks(p_scope, driver, outcome, lag, weeks, end_wk)
    if len(d) < 8 or d["DRV_Z"].isna().all() or d["OUT_Z"].isna().all():
        st.caption("Not enough weeks with both measures to chart them together.")
        return
    g("show_chart")(paired_chart(d, driver, outcome, lag))
    st.caption(paired_caption(d, r, lag, p_scope["SITE"].nunique() > 1) + key_note)


def paired_block_html(p_scope, rows, end_wk=None, weeks=26, title=None):
    """Snapshot: paired charts (driver + its outcome) for ledger-style rows [{DRIVER, OUTCOME, LAG}], two per row."""
    figs = []
    for r in rows:
        d, rr = paired_weeks(p_scope, r["DRIVER"], r["OUTCOME"], r["LAG"], weeks, end_wk)
        if len(d) < 8 or d["DRV_Z"].isna().all() or d["OUT_Z"].isna().all():
            continue
        dlab, olab = _pair_labels(r["DRIVER"], r["OUTCOME"], r["LAG"])
        figs.append(f"<div><h3>{E(dlab)} · {E(_lc(olab))}</h3>" + paired_svg(d, r["DRIVER"], r["OUTCOME"], r["LAG"])
                    + (f"<p class='pcap'>r = {rr:+.2f} on {len(d)} weekly values</p>" if np.isfinite(rr) else "") + "</div>")
    if not figs:
        return ""
    multi = p_scope["SITE"].nunique() > 1
    return ((f"<h2>{E(title)}</h2>" if title else "") + "<div class='grid2'>" + "".join(figs) + "</div>"
            + "<p class='cap'>" + paired_caption(pd.DataFrame(index=range(weeks)), np.nan, False, multi, html_=True)
              .replace(f"these {weeks} weeks", f"the last {weeks} weeks") + " Next-week pairs show the following week's injuries.</p>")


# ---------------------------------------------------------------------------------------------------------------
# Snapshot charts: small inline-SVG renderers (no libraries, works offline). Colors come from CSS tokens.
# ---------------------------------------------------------------------------------------------------------------
SNAP_PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
E = lambda s: html.escape("" if s is None else str(s), quote=True)   # noqa: E731


def fnum(v, f=",.0f", unit="", dash="—"):
    try:
        if v is None or pd.isna(v) or not np.isfinite(float(v)):
            return dash
    except (TypeError, ValueError):
        return E(v)
    m = re.search(r"\.(\d+)f", f)
    v = round(float(v), int(m.group(1)) if m else 0) + 0.0      # no "-0" / "+-0.0"
    return format(v, f) + unit


def _nice_ticks(lo, hi, n=4):
    if not np.isfinite(lo) or not np.isfinite(hi):
        return [0, 1]
    if hi == lo:
        hi = lo + (abs(lo) or 1)
    span = hi - lo
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    step = min((s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw), default=mag * 10)
    t0 = math.floor(lo / step) * step
    ticks = []
    t = t0
    while len(ticks) < 12:          # always end at or above hi, so bars and lines never spill past the top tick
        ticks.append(round(t, 10))
        if t >= hi - 1e-9 * max(abs(hi), 1):
            break
        t += step
    return ticks


def svg_chart(xlabels, series, kind="line", yfmt=",.0f", height=220, width=640, rules=(), band=None, zero=True,
              legend=True, title=None, x_every=None, ylab=None, highlight=None):
    """series: list of (label, values, color). kind: line | bar (grouped). rules: [(y, label, color)].
    band: (lo_values, hi_values, color). Values may contain None/NaN (gaps)."""
    xs = list(xlabels)
    n = len(xs)
    if n == 0 or not series:
        return "<p class='cap'>No data.</p>"
    vals = [v for _, s, _ in series for v in s if v is not None and np.isfinite(v)]
    if band:
        vals += [v for v in list(band[0]) + list(band[1]) if v is not None and np.isfinite(v)]
    vals += [r[0] for r in rules if r[0] is not None and np.isfinite(r[0])]
    if not vals:
        return "<p class='cap'>No data.</p>"
    lo, hi = min(vals), max(vals)
    if zero or kind == "bar":
        lo, hi = min(lo, 0), max(hi, 0)
    pad = (hi - lo) * 0.08 or 1
    ticks = _nice_ticks(lo if lo >= 0 and zero else lo - pad * 0.3, hi + pad * 0.3)
    y0, y1 = ticks[0], ticks[-1]
    L, R = 52, 14
    T = 34 if (legend and len(series) > 1) or rules else 12
    B = 34
    W, H = width, height
    pw, ph = W - L - R, H - T - B
    X = lambda i: L + (i + 0.5) * pw / n   # noqa: E731
    Y = lambda v: T + ph - (v - y0) / (y1 - y0) * ph if y1 != y0 else T + ph / 2   # noqa: E731
    out = [f"<svg viewBox='0 0 {W} {H}' class='chart' role='img' aria-label='{E(title or 'chart')}'>"]
    for t in ticks:
        out.append(f"<line x1='{L}' x2='{W - R}' y1='{Y(t):.1f}' y2='{Y(t):.1f}' class='{'zero' if t == 0 else 'grid'}'/>"
                   f"<text x='{L - 6}' y='{Y(t) + 4:.1f}' class='tick' text-anchor='end'>{E(format(t, yfmt.replace('+', '')))}</text>")
    every = x_every or max(1, math.ceil(n / 9))
    for i, xl in enumerate(xs):
        if i % every == 0 or i == n - 1 and n <= 14:
            out.append(f"<text x='{X(i):.1f}' y='{H - B + 16}' class='tick' text-anchor='middle'>{E(xl)}</text>")
    if band:
        pts_hi = [(X(i), Y(v)) for i, v in enumerate(band[1]) if v is not None and np.isfinite(v)]
        pts_lo = [(X(i), Y(v)) for i, v in enumerate(band[0]) if v is not None and np.isfinite(v)]
        if pts_hi and pts_lo:
            poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts_hi + pts_lo[::-1])
            out.append(f"<polygon points='{poly}' fill='{band[2]}' fill-opacity='0.16' stroke='none'/>")
    if kind == "bar":
        k = len(series)
        gw = pw / n * 0.72
        bw = max(gw / k - 2, 2)
        for si, (lab, vs, col) in enumerate(series):
            for i, v in enumerate(vs):
                if v is None or not np.isfinite(v):
                    continue
                x = X(i) - gw / 2 + si * (bw + 2)
                ya, yb = Y(max(v, 0)), Y(min(v, 0))
                c = col[i] if isinstance(col, (list, tuple)) else col
                out.append(f"<rect x='{x:.1f}' y='{ya:.1f}' width='{bw:.1f}' height='{max(yb - ya, 0.8):.1f}' rx='2.5' fill='{c}'>"
                           f"<title>{E(xs[i])} · {E(lab)}: {E(format(v, yfmt))}</title></rect>")
    else:
        for lab, vs, col in series:
            seg, segs = [], []
            for i, v in enumerate(vs):
                if v is None or not np.isfinite(v):
                    if seg:
                        segs.append(seg)
                    seg = []
                else:
                    seg.append((X(i), Y(v), i, v))
            if seg:
                segs.append(seg)
            for s_ in segs:
                if len(s_) > 1:
                    out.append(f"<polyline points='{' '.join(f'{x:.1f},{y:.1f}' for x, y, _, _ in s_)}' fill='none' "
                               f"stroke='{col}' stroke-width='2' stroke-linejoin='round'/>")
                for x, y, i, v in s_:
                    big = highlight is not None and i == highlight
                    out.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='{5 if big else 3}' fill='{col}' stroke='var(--bg)' stroke-width='1.5'>"
                               f"<title>{E(xs[i])} · {E(lab)}: {E(format(v, yfmt))}</title></circle>")
    for yv, lab, col in rules:
        if yv is None or not np.isfinite(yv):
            continue
        out.append(f"<line x1='{L}' x2='{W - R}' y1='{Y(yv):.1f}' y2='{Y(yv):.1f}' stroke='{col}' stroke-width='1.5' "
                   f"stroke-dasharray='5 4'><title>{E(lab)}: {E(format(yv, yfmt))}</title></line>")
    if legend:
        lx = L
        items = [(lab, col if not isinstance(col, (list, tuple)) else col[0], "dot") for lab, _, col in series] if len(series) > 1 else []
        items += [(lab, col, "dash") for yv, lab, col in rules if yv is not None and np.isfinite(yv)]
        if band:
            items.append((band[3] if len(band) > 3 else "Range", band[2], "band"))
        for lab, col, kind_ in items:
            sym = (f"<circle cx='{lx + 5}' cy='12' r='4.5' fill='{col}'/>" if kind_ == "dot" else
                   f"<line x1='{lx}' x2='{lx + 12}' y1='12' y2='12' stroke='{col}' stroke-width='2' stroke-dasharray='4 3'/>"
                   if kind_ == "dash" else f"<rect x='{lx}' y='7' width='12' height='10' fill='{col}' fill-opacity='0.25'/>")
            out.append(sym + f"<text x='{lx + 16}' y='16' class='lg'>{E(lab)}</text>")
            lx += 26 + 6.3 * len(str(lab))
    if ylab:
        out.append(f"<text x='12' y='{T + ph / 2:.0f}' class='tick' transform='rotate(-90 12 {T + ph / 2:.0f})' text-anchor='middle'>{E(ylab)}</text>")
    out.append("</svg>")
    return "".join(out)


def svg_hbars(labels, values, colors=None, fmt=",.2f", rule=None, rule_label="Network", width=640, notes=None):
    """Horizontal bars (rankings). colors: list or single."""
    n = len(labels)
    if n == 0:
        return "<p class='cap'>No data.</p>"
    vals = [v for v in values if v is not None and np.isfinite(v)]
    if rule is not None and np.isfinite(rule):
        vals.append(rule)
    hi = max(vals + [0]) or 1
    lo = min(vals + [0])
    labels = [str(x) for x in labels]
    short = [x if len(x) <= 44 else x[:43] + "…" for x in labels]
    L, R, row = int(min(max(128, 6.3 * max(len(x) for x in short) + 18), 320)), 60, 22
    H = n * row + 30
    W = width
    pw = W - L - R
    X = lambda v: L + (v - lo) / (hi - lo) * pw if hi != lo else L   # noqa: E731
    out = [f"<svg viewBox='0 0 {W} {H}' class='chart' role='img'>"]
    for i, (lab, v) in enumerate(zip(labels, values)):
        y = 8 + i * row
        c = colors[i] if isinstance(colors, (list, tuple)) else (colors or SNAP_PALETTE[0])
        out.append(f"<text x='{L - 8}' y='{y + 13}' class='tick' text-anchor='end'><title>{E(lab)}</title>{E(short[i])}</text>")
        if v is not None and np.isfinite(v):
            x0, x1 = X(min(v, 0)), X(max(v, 0))
            out.append(f"<rect x='{x0:.1f}' y='{y + 2}' width='{max(x1 - x0, 1):.1f}' height='{row - 8}' rx='3' fill='{c}'>"
                       f"<title>{E(lab)}: {E(format(v, fmt))}{(' · ' + E(notes[i])) if notes else ''}</title></rect>"
                       f"<text x='{x1 + 5:.1f}' y='{y + 13}' class='tick'>{E(format(v, fmt))}</text>")
    if rule is not None and np.isfinite(rule):
        out.append(f"<line x1='{X(rule):.1f}' x2='{X(rule):.1f}' y1='4' y2='{H - 22}' stroke='var(--axis)' stroke-width='1.5' "
                   f"stroke-dasharray='4 4'/><text x='{X(rule):.1f}' y='{H - 8}' class='tick' text-anchor='middle'>"
                   f"{E(rule_label)} {E(format(rule, fmt))}</text>")
    out.append("</svg>")
    return "".join(out)


def svg_ci_bars(labels, rates, lo, hi, color=SNAP_PALETTE[0], fmt=".2f", width=420, height=200, ylab=None):
    """Vertical bars with 95% whiskers (quartile comparisons)."""
    n = len(labels)
    vals = [v for v in list(rates) + list(hi) if v is not None and np.isfinite(v)]
    if not vals:
        return "<p class='cap'>No data.</p>"
    ticks = _nice_ticks(0, max(vals) * 1.05)
    y1 = ticks[-1]
    L, R, T, B = 44, 10, 10, 40
    W, H = width, height
    pw, ph = W - L - R, H - T - B
    X = lambda i: L + (i + 0.5) * pw / n   # noqa: E731
    Y = lambda v: T + ph - v / y1 * ph   # noqa: E731
    out = [f"<svg viewBox='0 0 {W} {H}' class='chart' role='img'>"]
    for t in ticks:
        out.append(f"<line x1='{L}' x2='{W - R}' y1='{Y(t):.1f}' y2='{Y(t):.1f}' class='grid'/>"
                   f"<text x='{L - 5}' y='{Y(t) + 4:.1f}' class='tick' text-anchor='end'>{E(format(t, fmt))}</text>")
    bw = pw / n * 0.55
    for i, lab in enumerate(labels):
        v = rates[i]
        if v is None or not np.isfinite(v):
            continue
        out.append(f"<rect x='{X(i) - bw / 2:.1f}' y='{Y(v):.1f}' width='{bw:.1f}' height='{Y(0) - Y(v):.1f}' rx='3' fill='{color}'>"
                   f"<title>{E(lab)}: {E(format(v, fmt))} (95% {E(format(lo[i], fmt))}–{E(format(hi[i], fmt))})</title></rect>")
        out.append(f"<line x1='{X(i):.1f}' x2='{X(i):.1f}' y1='{Y(hi[i]):.1f}' y2='{Y(max(lo[i], 0)):.1f}' stroke='var(--ink)' stroke-width='1.3'/>")
        for part, k in enumerate(str(lab).split("\n")[:2]):
            out.append(f"<text x='{X(i):.1f}' y='{H - B + 15 + 13 * part}' class='tick' text-anchor='middle'>{E(k)}</text>")
    if ylab:
        out.append(f"<text x='10' y='{T + ph / 2:.0f}' class='tick' transform='rotate(-90 10 {T + ph / 2:.0f})' text-anchor='middle'>{E(ylab)}</text>")
    out.append("</svg>")
    return "".join(out)


def html_table(df, cols, cls="", num_right=True, max_rows=None):
    """cols: list of (column, header, formatter or None). Formatter gets the raw value, returns HTML-safe text."""
    if df is None or len(df) == 0:
        return "<p class='cap'>No rows.</p>"
    d = df if max_rows is None else df.head(max_rows)
    head = "".join(f"<th class='{'t' if f in (None, 'html') else ''}'>{E(h)}</th>" for c, h, f in cols)
    rows = []
    for r in d.to_dict("records"):
        tds = []
        for c, h, f in cols:
            v = r.get(c)
            if f is None:
                tds.append(f"<td class='t'>{E('' if v is None or (isinstance(v, float) and np.isnan(v)) else v)}</td>")
            elif f == "html":
                tds.append(f"<td class='t'>{v if v is not None else ''}</td>")
            elif f == "rhtml":
                tds.append(f"<td>{v if v is not None else ''}</td>")
            else:
                tds.append(f"<td>{f(v)}</td>")
        rows.append("<tr>" + "".join(tds) + "</tr>")
    more = (f"<p class='cap'>Showing {max_rows} of {len(df)} rows.</p>" if max_rows and len(df) > max_rows else "")
    return f"<div class='scroll {cls}'><table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>{more}"


def kpi_card(label, value, delta=None, tone="", tip=""):
    d = f"<span class='d {tone}'>{E(delta)}</span>" if delta else ""
    return f"<div class='kpi' title='{E(tip)}'><small>{E(label)}</small><b>{E(value)}</b>{d}</div>"


def delta_txt(v, p, kind="rate", worse_up=True):
    """(text, tone) for a KPI delta, same wording as the app (counts +N (+X%), rates +0.00 (+X%), pts, hours %)."""
    if v is None or p is None or not np.isfinite(v) or not np.isfinite(p):
        return None, ""
    d = v - p
    tiny = {"count": 0.5, "rate": 0.005, "pts": 0.05}.get(kind, 0.0)
    if abs(d) < max(tiny, 1e-9) or (kind == "hours" and p and abs(d / p) < 0.0005):
        return None, ""
    pct = f" ({d / p:+.0%})" if p else ""
    txt = {"count": f"{d:+,.0f}{pct}", "rate": f"{d:+,.2f}{pct}", "pts": f"{d:+.1f} pts",
           "hours": (f"{d / p:+.1%}" if p else None)}.get(kind, f"{d:+.2f}")
    up = d > 0
    tone = ("bad" if up else "good") if worse_up else ""
    return (("▲ " if up else "▼ ") + txt) if txt else None, tone


# ---------------------------------------------------------------------------------------------------------------
# Snapshot context: everything the file needs, computed once for every site in the sidebar scope
# ---------------------------------------------------------------------------------------------------------------
PEAK_METRIC = "INJ_ALL"          # recordables + first aid: steadier weekly counts than recordables alone


def _wk_of(ts):
    ts = pd.Timestamp(ts)
    return ts - pd.Timedelta(days=(ts.weekday() + 1) % 7)


def sop_plan_rows(sop, ow, pool, cur_wk):
    """S&OP plan ahead, scaled to shipped units per site (calibration from the latest overlap week), with each site's
    average shipped units over its last 4 full weeks (RECENT)."""
    plan_rows = []
    if sop is not None and len(sop) and ow is not None and len(ow):
        pw = sop.groupby(["SITE", "WK"], as_index=False)["PLAN_UNITS"].sum()
        for s in pool:
            ps = pw[pw["SITE"] == s].sort_values("WK")
            of = ow[(ow["SITE"] == s) & (ow["DAYS"] == 7) & (ow["WK"] < cur_wk)].sort_values("WK")
            if ps.empty or of.empty:
                continue
            ov = ps.merge(of[["WK", "UNITS"]], on="WK")
            calib = float(np.clip(ov["UNITS"].iloc[-1] / ov["PLAN_UNITS"].iloc[-1], 0.5, 2.0)) \
                if len(ov) and ov["PLAN_UNITS"].iloc[-1] > 0 else 1.0
            recent = of.tail(4)["UNITS"].mean()
            for r in ps[ps["WK"] >= cur_wk].itertuples():
                plan_rows.append({"SITE": s, "WK": r.WK, "UNITS": r.PLAN_UNITS * calib, "RECENT": recent})
    return pd.DataFrame(plan_rows, columns=["SITE", "WK", "UNITS", "RECENT"])


def outlook_parts(dfact, sw_all, ow, hol, sop, pool, cur_wk):
    """Peak projection (recordable + first aid, at the S&OP plan) and the plan scaled to shipped units, per site.
    Shared by the snapshot and the weekly note."""
    first_nz = g("coverage")(dfact, ["RECORDABLE_INCIDENTS"]).get("RECORDABLE_INCIDENTS", (pd.NaT, pd.NaT))[0]
    x_all = g("load_response")(sw_all, PEAK_METRIC, first_nz if pd.notna(first_nz) else sw_all["WK"].min())
    fit = g("fit_hinge")(x_all)
    a_fit, b_fit, se_b = fit if fit else (0.0, 0.0, np.nan)
    hol_w = g("holiday_weeks")(hol) if hol is not None else pd.DataFrame(columns=["SITE", "WK", "UNITS", "DAYS_U"])
    eps_est, _ = g("hours_elasticity")(sw_all, hol_w)
    eps = round(float(np.clip(eps_est, 0.2, 1.2)), 2) if pd.notna(eps_est) else 0.6
    if sop is not None and len(sop):
        proj, pbase = g("project_peak")(sw_all[sw_all["SITE"].isin(pool)], hol_w, sop, PEAK_METRIC, cur_wk, eps, b_fit)
    else:
        proj, pbase = pd.DataFrame(), pd.DataFrame()
    watch, act = g("threshold")(b_fit, g("WATCH_UPLIFT")), g("threshold")(b_fit, g("ACT_UPLIFT"))

    plan = sop_plan_rows(sop, ow, pool, cur_wk)
    return {"proj": proj, "pbase": pbase, "b": b_fit, "se_b": se_b, "watch": watch, "act": act, "eps": eps, "plan": plan}


def gather_context():
    basis, basis_label = g("basis"), g("basis_label")
    th = g("ovsr_th_current")()
    th_items = tuple(sorted(th.items()))
    wf = g("load_workforce")()
    p_all, notes, dept_h = panel_cached(basis, True)
    dfact = _daily_fact()
    pool = sorted(g("scope")(dfact, g("sel_regions"), g("sel_sites"))["SITE"].unique())
    rl = g("region_lookup")
    regions = {s: rl.get(s, "Other") for s in pool}
    today_ts = pd.Timestamp(g("_today_et")())
    cur_wk = _wk_of(today_ts)
    last_day = dfact["DT"].max()
    hol, _, sop, _ = g("load_plan")()
    ow = g("ops_weeks")(hol)
    sw_all = g("site_weeks")(dfact)
    sw_all["INJ_ALL"] = sw_all["RECORDABLE_INCIDENTS"] + sw_all["FIRST_AID_INCIDENT"]
    full_wks = sorted(w for w in sw_all["WK"].unique() if w < cur_wk)
    lw = full_wks[-1]
    wks12 = full_wks[-12:]
    hist = g("ovsr_history")(g("DAILY"), tuple(wks12) + (cur_wk,), th_items, tuple(pool), cur_wk, last_day)
    try:   # status of every site-week for the last 52 weeks (rates by status tier); one cached build for the whole pool
        hist52 = hist_cached(tuple(full_wks[-52:]), th_items, tuple(pool))
    except Exception:
        hist52 = pd.DataFrame()
    fr_last = g("ovsr_frame")(sw_all, ow, sop, lw, th, pool)
    fr_now = g("ovsr_frame")(sw_all, ow, sop, cur_wk, th, pool)

    # Selected period (sidebar range snapped to whole weeks) and its comparison
    lo, hi = pd.Timestamp(g("start")), pd.Timestamp(g("end"))
    rng = [w for w in full_wks if w >= _wk_of(lo) and w + pd.Timedelta(days=6) <= hi] or full_wks[-4:]
    comp = g("compare")
    if comp == "Previous period":
        prev = [w for w in full_wks if w < rng[0]][-len(rng):]
    elif comp == "Same period last year":
        prev = [w - pd.Timedelta(days=364) for w in rng if (w - pd.Timedelta(days=364)) in set(sw_all["WK"])]
    else:
        prev = []
    if len(prev) < len(rng):
        prev = []

    ol_parts = outlook_parts(dfact, sw_all, ow, hol, sop, pool, cur_wk)

    # Yesterday
    by_day = dfact[dfact["SITE"].isin(pool)].groupby("DT")["TOTAL_HOURS"].sum()
    target = today_ts - pd.Timedelta(days=1)
    loaded = [d for d in by_day[by_day > 0].index if d <= target]
    day = loaded[-1] if loaded else dfact["DT"].max()

    inj, inj_err = load_x("x_inj")
    wnt_all, wnt_err = load_x("x_wnt_all")
    met, _ = load_x("x_met")
    try:   # items and promotions (cached 6 hours for everyone; the snapshot build is an explicit action, so load them)
        items, _, promos, _ = items_data()
    except Exception:
        items, promos = None, None
    try:   # process paths and the department plan (same: explicit build, so load them)
        paths, _ = path_panel(basis)
        dept_plan, _ = load_x("x_dept_plan")
    except Exception:
        paths, dept_plan = None, None
    p_pool = p_all[p_all["SITE"].isin(pool)]
    try:   # evidence ledger over every site in scope (same cached build the app shows)
        ledger, _ = ledger_cached(basis, True, tuple(sorted(p_pool["SITE"].unique())))
    except Exception:
        ledger = pd.DataFrame()
    try:
        nolift = no_lift_triggers(p_pool, ow[ow["SITE"].isin(pool)], th, basis)
    except Exception:
        nolift = pd.DataFrame()
    wf_now = g("wf_site_now")(wf, dfact, cur_wk, th)
    icare = g("icare_frames") or {}
    return {
        "basis": basis, "basis_label": basis_label, "th": th, "th_default": th == {k: float(v[1]) for k, v in g("OVSR_TH").items()},
        "panel": p_all, "notes": notes, "dept_h": dept_h, "dfact": dfact, "pool": pool, "regions": regions,
        "today": today_ts, "cur_wk": cur_wk, "last_day": last_day, "hol": hol, "sop": sop, "ow": ow, "sw": sw_all,
        "full_wks": full_wks, "lw": lw, "wks12": wks12, "hist": hist, "hist52": hist52, "fr_last": fr_last, "fr_now": fr_now,
        "rng": rng, "prev": prev, "compare": comp, **ol_parts, "day": pd.Timestamp(day), "inj": inj, "inj_err": inj_err,
        "wnt_all": wnt_all, "wf": wf, "wf_now": wf_now, "icare": icare, "met": met, "items": items, "promos": promos, "paths": paths, "dept_plan": dept_plan, "ledger": ledger, "nolift": nolift,
        "panel_sites": int(p_pool["SITE"].nunique()),
        "publish": sop["PUBLISH_WEEK"].max() if sop is not None and len(sop) and "PUBLISH_WEEK" in sop else pd.NaT,
    }


def scopes_for(ctx):
    """Network (all sites in scope), each region with 2+ sites, then each site."""
    pool, regions = ctx["pool"], ctx["regions"]
    out = [("ALL", "All sites in scope", pool, "net")]
    pref = g("REGION_PREF") or []
    for r in sorted(set(regions.values()), key=lambda r: (pref.index(r) if r in pref else 99, r)):
        ss = [s for s in pool if regions[s] == r]
        if len(ss) >= 2:
            out.append((f"R-{r}", f"{r} region", ss, "region"))
        sg = g("site_gen")
        if sg is not None:   # East · Gen 1, East · Gen 2, ... (FC generation buckets inside the region)
            for gen in ("Gen 1", "Gen 2"):
                sgs = [s for s in ss if sg(s) == gen]
                if sgs and len(sgs) < len(ss):
                    out.append((f"RG-{r}-{gen}", f"{r} · {gen}", sgs, "region"))
    for s in pool:
        out.append((f"S-{s}", f"{s} · {regions[s]}", [s], "site"))
    return out


# ---------------------------------------------------------------------------------------------------------------
# Snapshot tabs: one HTML fragment per tab per scope
# ---------------------------------------------------------------------------------------------------------------
P = SNAP_PALETTE


def _status_chip(s_):
    col = (g("STATUS_COLOR") or {}).get(s_, "#9a9893")
    return f"<span class='st' style='--c:{col}'><i></i>{E(s_)}</span>" if s_ else "—"


def _tiles(counts):
    out = []
    for t in g("STATUS_ORDER"):
        n = int(counts.get(t, 0) or 0)
        out.append(f"<div class='tile{' zero' if n == 0 else ''}' style='--c:{g('STATUS_COLOR')[t]}' "
                   f"title='{E(g('STATUS_HELP')[t])}'><b>{n}</b><span><i></i>{E(t)}</span></div>")
    return "<div class='tiles'>" + "".join(out) + "</div>"


def _wk(w, f="%b %d"):
    return pd.Timestamp(w).strftime(f) if pd.notna(w) else "—"


def _sum_period(sw, sites, wks):
    d = sw[sw["SITE"].isin(sites) & sw["WK"].isin(wks)]
    return d.select_dtypes(include="number").agg(gov.strict_sum), d


def _rate(n, h, basis):
    return n * basis / h if h and h > 0 else np.nan


def _ops_sum(ow, sites, wks):
    o = ow[ow["SITE"].isin(sites) & ow["WK"].isin(wks)]
    if o.empty:
        return {}
    u, up, h, hp = o["UNITS"].sum(), o["UNITS_PLAN"].sum(), o["HRS"].sum(), o["HRS_PLAN"].sum()
    vol = (u / up - 1) * 100 if up > 0 else np.nan
    hrs = (h / hp - 1) * 100 if hp > 0 else np.nan
    return {"VOL": vol, "HRS": hrs, "PRESS": vol - hrs if np.isfinite(vol) and np.isfinite(hrs) else np.nan,
            "TPH": o["TPH_PCT"].mean(), "BL": o["BL_AGE"].mean(), "IB": ((o["IB"].sum() / o["IB_PLAN"].sum() - 1) * 100
                                                                     if o["IB_PLAN"].sum() > 0 else np.nan)}


def outlook_for(ctx, sites):
    """Plan, projection and loads summed over `sites`."""
    out = {}
    plan = ctx["plan"]
    if len(plan):
        pl = plan[plan["SITE"].isin(sites)]
        if len(pl):
            rec = pl.groupby("SITE")["RECENT"].first().sum()
            pw = pl.groupby("WK", as_index=False)["UNITS"].sum().sort_values("WK")
            pw["VS"] = (pw["UNITS"] / rec - 1) * 100 if rec else np.nan
            out["plan"], out["recent"] = pw, rec
    proj, pbase = ctx["proj"], ctx["pbase"]
    if len(proj):
        # Same site list as the plan lines: sites left out of ops scoring (DFW1) are left out here too.
        ex = set(g("OPS_EXCLUDE") or [])
        pj = proj[proj["SITE"].isin(sites) & ~proj["SITE"].isin(ex)]
        pb = pbase[pbase["SITE"].isin(sites) & ~pbase["SITE"].isin(ex) & pbase["OK"]] if len(pbase) else pbase
        if len(pj) and len(pb):
            pn = pj.groupby("WK", as_index=False)[["HRS", "EXP", "EXP_BASE", "PLAN_UNITS"]].sum().sort_values("WK")
            base_h = pb["BASE_HRS"].sum()
            pn["LOAD"] = pn["HRS"] / base_h if base_h else np.nan
            pn["LO"] = (pn["EXP"] - 1.28 * np.sqrt(pn["EXP"])).clip(lower=0)
            pn["HI"] = pn["EXP"] + 1.28 * np.sqrt(pn["EXP"])
            out["proj"], out["base_h"] = pn, base_h
            out["normal"] = float((pb["BASE_RATE"] * pb["BASE_HRS"]).sum())
            g_ = pj.sort_values("EXP", ascending=False).groupby("SITE").head(1)[["SITE", "WK", "EXP"]]
            agg = pj.groupby("SITE", as_index=False).agg(MAX_LOAD=("LOAD", "max"), TOTAL=("EXP", "sum"),
                                                          N_WATCH=("LOAD", lambda s: int((s >= ctx["watch"]).sum()) if pd.notna(ctx["watch"]) else 0),
                                                          N_ACT=("LOAD", lambda s: int((s >= ctx["act"]).sum()) if pd.notna(ctx["act"]) else 0))
            stb = agg.merge(g_.rename(columns={"WK": "PEAK_WK", "EXP": "PEAK_EXP"}), on="SITE").merge(
                pb[["SITE", "BASE_HRS", "BASE_RATE"]], on="SITE")
            stb["CAP"] = stb["BASE_HRS"] * (ctx["watch"] if pd.notna(ctx["watch"]) else np.nan)
            out["sites"] = stb.sort_values(["N_ACT", "N_WATCH", "PEAK_EXP"], ascending=False)
    met = ctx.get("met")
    if met is not None and len(met):
        m = met[met["SITE"].isin(sites) & (met["WK"] >= ctx["cur_wk"])]
        if len(m):
            mw = m.groupby("WK", as_index=False)[["MET_H", "PLAN_REG_H"]].sum().sort_values("WK")
            mw["PCT"] = mw["MET_H"] / mw["PLAN_REG_H"].where(mw["PLAN_REG_H"] > 0) * 100
            out["met"] = mw
    return out


def tab_overview(ctx, sites, label, kind, out_look, pw):
    basis, sw, ow, hist = ctx["basis"], ctx["sw"], ctx["ow"], ctx["hist"]
    h = hist[hist["SITE"].isin(sites)]
    now = h[h["WK"] == ctx["cur_wk"]]
    last = h[h["WK"] == ctx["lw"]]
    s = [f"<h2>Site status</h2><div class='two'><div><h3>This week to date · {_wk(ctx['cur_wk'])}</h3>{_tiles(now['STATUS'].value_counts())}"
         f"<p class='cap'>Provisional: ops covers the days loaded so far and can't reach Elevated until the week is complete.</p></div>"
         f"<div><h3>Last full week · {_wk(ctx['lw'])}</h3>{_tiles(last['STATUS'].value_counts())}"
         f"<p class='cap'>{len(sites)} site{'s' if len(sites) != 1 else ''} rated. Hover a tile for its definition.</p></div></div>"]
    # Next 4 weeks
    s.append("<h2>Next 4 weeks · what the plan asks</h2>")
    cards = []
    plan, proj = out_look.get("plan"), out_look.get("proj")
    if plan is not None and len(plan):
        for r in plan.head(4).itertuples():
            pr = proj[proj["WK"] == r.WK].iloc[0] if proj is not None and len(proj[proj["WK"] == r.WK]) else None
            load = pr["LOAD"] if pr is not None else np.nan
            mw = out_look.get("met")
            mr = mw[mw["WK"] == r.WK] if mw is not None else None
            met_txt = ("" if mr is None or mr.empty else
                       f"<span>MET planned: {fnum(mr['MET_H'].iloc[0], ',.0f')} h ({fnum(mr['PCT'].iloc[0], '.1f', '%')} of regular)</span>"
                       if mr["MET_H"].iloc[0] > 0 else "<span>No MET planned</span>")
            tone = ("act" if pd.notna(ctx["act"]) and load >= ctx["act"] else "watch" if pd.notna(ctx["watch"]) and load >= ctx["watch"]
                    else "ramp" if np.isfinite(r.VS) and r.VS >= ctx["th"]["ramp"] else "")
            cards.append(f"<div class='wkcard {tone}'><small>Week of {_wk(r.WK)}{' · this week' if r.WK == ctx['cur_wk'] else ''}</small><b>{fnum(r.UNITS, ',.0f')}</b>"
                         f"<span>planned units · {fnum(r.VS, '+.0f', '%')} vs last 4 wks</span>"
                         f"<span>hours {fnum(load, '.2f', '×')} normal"
                         + (f" · ~{fnum(pr['EXP'], '.0f')} injuries (normal {fnum(out_look.get('normal'), '.0f')})" if pr is not None else "")
                         + "</span>" + met_txt + "</div>")
        s.append("<div class='wkcards'>" + "".join(cards) + "</div>"
                 f"<p class='cap'>S&amp;OP outbound plan (published {_wk(ctx['publish'], '%b %d')}) scaled to shipped units. "
                 f"Orange = ramp of {ctx['th']['ramp']:.0f}%+ vs the last 4 full weeks; amber/red = projected hours over the "
                 "watch/act line. Full horizon on the Ops outlook tab.</p>")
    else:
        s.append("<p class='cap'>No S&amp;OP plan for these sites (the plan covers the core FCs only).</p>")
    if out_look.get("met") is not None:
        s.append("<p class='cap'>MET planned = mandatory extra time in the latest network staffing-plan run.</p>")
    # Selected period KPIs
    rng, prev = ctx["rng"], ctx["prev"]
    t, _ = _sum_period(sw, sites, rng)
    c, _ = _sum_period(sw, sites, prev) if prev else (None, None)
    o, op = _ops_sum(ow, sites, rng), (_ops_sum(ow, sites, prev) if prev else {})
    s.append(f"<h2>Selected period · {_wk(rng[0])} – {_wk(rng[-1] + pd.Timedelta(days=6), '%b %d, %Y')}</h2>"
             f"<p class='cap'>{len(rng)} whole week{'s' if len(rng) != 1 else ''} from the app's date range. "
             + ("Δ vs " + ("the weeks before" if ctx["compare"] == "Previous period" else "the same weeks last year") + "."
                if prev else "No comparison.") + "</p><h3>Operations vs plan</h3><div class='kpis'>")
    for key, lab, kind_ in (("PRESS", "Workload pressure", "pts"), ("VOL", "Volume vs plan", "pts"), ("HRS", "Hours vs plan", "pts"),
                            ("TPH", "Pace vs plan", "pts"), ("IB", "Inbound vs plan", "pts"), ("BL", "Oldest backlog (avg)", "h")):
        v, pv_ = o.get(key, np.nan), op.get(key, np.nan) if op else np.nan
        txt = fnum(v, "+.1f", " pts" if key == "PRESS" else "%") if key != "BL" else fnum(v, ".0f", " h")
        dt, tone = delta_txt(v, pv_, "pts", worse_up=key in ("PRESS", "TPH", "BL")) if key != "BL" else (None, "")
        s.append(kpi_card(lab, txt, dt, tone if key in ("PRESS", "TPH") else ""))
    s.append(f"</div><h3>Safety · {E(ctx['basis_label'])}</h3><div class='kpis'>")
    for k, lab in (("RECORDABLE_INCIDENTS", "Recordable rate"), ("FIRST_AID_INCIDENT", "First-aid rate"),
                   ("MSD_INJURIES", "MSD rate"), ("MOE_INCIDENTS", "MOE rate")):
        v = _rate(t.get(k, np.nan), t.get("TOTAL_HOURS", 0), basis)
        pv_ = _rate(c.get(k, np.nan), c.get("TOTAL_HOURS", 0), basis) if c is not None else np.nan
        dt, tone = delta_txt(v, pv_, "rate")
        s.append(kpi_card(lab, fnum(v, ".2f"), dt, tone, f"{fnum(t.get(k, np.nan), ',.0f')} over {g('fmt_hours')(t.get('TOTAL_HOURS', 0))} hours"))
    hv, hpv = t.get("TOTAL_HOURS", 0), (c.get("TOTAL_HOURS", 0) if c is not None else np.nan)
    dt, _ = delta_txt(hv, hpv, "hours", worse_up=False)
    s.append(kpi_card("Hours worked", g("fmt_hours")(hv), dt, ""))
    s.append("</div>")
    # Call first
    fl = ctx["fr_last"]
    fl = fl[fl["SITE"].isin(sites) & fl["STATUS"].isin(["Elevated", "Moderate Elevation", "Watch"])].copy()
    fl["_O"] = fl["STATUS"].map(g("STATUS_ORDER"))
    fl = fl.sort_values(["_O", "N_TRIG", "SITE"], ascending=[True, False, True])
    s.append(f"<h2>Call first · week of {_wk(ctx['lw'])}</h2><div class='call'>")
    if fl.empty:
        s.append("<p>No site is on Watch, Moderate Elevation or Elevated that week.</p>")
    else:
        items = []
        for r in fl.head(5).to_dict("records"):
            f0 = r["FIRED"][0] if len(r["FIRED"]) else None
            ev = g("_ev_text")(f0, r, ctx["th"]) if f0 else ""
            items.append(f"<li><b>{E(r['SITE'])}</b> {_status_chip(r['STATUS'])} · {E(r['TRIGGERS'])}"
                         + (f"<p>Why: {E(r['STATUS_REASON'])}.</p>" if r.get("STATUS_REASON") else "")
                         + (f"<p>First move: {E(g('RESPONSES')[f0][2])}</p><p class='ev'>Evidence: {E(ev)}</p>" if f0 else "") + "</li>")
        s.append("<ol>" + "".join(items) + "</ol>")
        if len(fl) > 5:
            s.append(f"<p>{len(fl) - 5} more flagged sites in the scorecard below.</p>")
    s.append("</div>")
    s.append(evidence_strip_html(ctx, sites))
    # Scorecard
    if len(sites) > 1:
        rows = []
        p = pw[pw["WK"].isin(rng)]
        for site in sites:
            tt, _ = _sum_period(sw, [site], rng)
            oo = _ops_sum(ow, [site], rng)
            ps = p[p["SITE"] == site]
            st_ = fl[fl["SITE"] == site]
            stx = ctx["fr_last"][ctx["fr_last"]["SITE"] == site]
            rows.append({"SITE": site, "STATUS": _status_chip(stx["STATUS"].iloc[0]) if len(stx) else "—",
                         "FIRED": stx["TRIGGERS"].iloc[0] if len(stx) else "", "PRESS": oo.get("PRESS"), "VOL": oo.get("VOL"),
                         "HRS": oo.get("HRS"), "TRIR": _rate(tt.get("RECORDABLE_INCIDENTS", 0), tt.get("TOTAL_HOURS", 0), basis),
                         "FAR": _rate(tt.get("FIRST_AID_INCIDENT", 0), tt.get("TOTAL_HOURS", 0), basis),
                         "MSDR": _rate(tt.get("MSD_INJURIES", 0), tt.get("TOTAL_HOURS", 0), basis),
                         "MOER": _rate(tt.get("MOE_INCIDENTS", 0), tt.get("TOTAL_HOURS", 0), basis),
                         "OT": ps["OT_H"].sum() / ps["REG_H"].sum() * 100 if ps["REG_H"].sum() > 0 else np.nan,
                         "MET": ps["MET_H"].sum() / ps["PLAN_REG_H"].sum() * 100 if ps["PLAN_REG_H"].sum() > 0 else np.nan,
                         "HIRES": ps["NEW_HIRES"].sum() if len(ps) else np.nan,
                         "OVER": ps["CLMS_OVER_TM_PATHS"].sum() / ps["CLMS_STRAIN_TM_PATHS"].sum() * 100
                         if ps["CLMS_STRAIN_TM_PATHS"].sum() > 0 else np.nan, "HOURS": tt.get("TOTAL_HOURS", 0),
                         "_O": g("STATUS_ORDER").get(stx["STATUS"].iloc[0], 9) if len(stx) else 9})
        sc = pd.DataFrame(rows).sort_values(["_O", "MSDR"], ascending=[True, False])
        f2, f1p = (lambda v: fnum(v, ".2f")), (lambda v: fnum(v, "+.1f", "%"))
        s.append(f"<h2>Site scorecard</h2><p class='cap'>Status and fired triggers for the week of {_wk(ctx['lw'])}; "
                 "everything else covers the selected period. OT is % of UKG regular hours; MET is scheduled MET as % of planned "
                 "regular hours (staffing plan); over-utilized = "
                 "high-strain TM-paths over 106 hrs in 4 weeks (CLMS).</p>")
        s.append(html_table(sc, [("SITE", "Site", None), ("STATUS", "Status", "html"), ("FIRED", "Fired that week", None),
                                 ("PRESS", "Pressure", lambda v: fnum(v, "+.1f", " pts")), ("VOL", "Volume vs plan", f1p),
                                 ("HRS", "Hours vs plan", f1p), ("TRIR", "Recordable rate", f2), ("FAR", "First-aid rate", f2),
                                 ("MSDR", "MSD rate", f2), ("MOER", "MOE rate", f2), ("OT", "OT %", lambda v: fnum(v, ".1f", "%")),
                                 ("MET", "MET %", lambda v: fnum(v, ".2f", "%")), ("HIRES", "New hires", lambda v: fnum(v, ",.0f")),
                                 ("OVER", "Over-utilized", lambda v: fnum(v, ".0f", "%")), ("HOURS", "Hours", lambda v: fnum(v, ",.0f"))],
                          cls="sc"))
    # Status by week grid
    hh = h[h["WK"].isin(ctx["wks12"])]
    if len(hh):
        cell = {(r["SITE"], r["WK"]): r for r in hh.to_dict("records")}
        head = "".join(f"<th>{_wk(w, '%m/%d')}</th>" for w in ctx["wks12"])
        rows = []
        for site in sites:
            tds = []
            for w in ctx["wks12"]:
                r = cell.get((site, w))
                if r is None:
                    tds.append("<td></td>")
                    continue
                tip = f"{site} · week of {_wk(w)} · {r['STATUS']} · fired: {r.get('TRIGGERS') or 'none'}"
                tds.append(f"<td><span class='cell' style='--c:{g('STATUS_COLOR').get(r['STATUS'], '#9a9893')}' title='{E(tip)}'></span></td>")
            rows.append(f"<tr><td class='s'>{E(site)}</td>{''.join(tds)}</tr>")
        s.append("<h2>Status by week</h2><div class='lg'>" + "".join(
            f"<span style='--c:{g('STATUS_COLOR')[t_]}'><i></i>{t_}</span>" for t_ in g("STATUS_ORDER")) + "</div>"
            f"<div class='scroll gw'><table class='grid'><thead><tr><th></th>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
            "<p class='cap'>Hover a cell for what fired.</p>")
    return "".join(s)


def tab_outlook(ctx, sites, label, kind, ol, pw):
    s = []
    th = ctx["th"]
    plan, proj = ol.get("plan"), ol.get("proj")
    s.append("<h2>Planned outbound volume · S&amp;OP</h2>")
    if plan is None or plan.empty:
        s.append("<p class='cap'>No S&amp;OP outbound plan for these sites (the plan covers the core FCs only).</p>")
    else:
        pk = plan.loc[plan["UNITS"].idxmax()]
        cols = [P[1] if np.isfinite(v) and v >= th["ramp"] else "#86b6ef" for v in plan["VS"]]
        s.append("<div class='kpis'>" + kpi_card("Recent weekly shipped", fnum(ol["recent"], ",.0f"), None, "", "Average of the last 4 full weeks")
                 + kpi_card(f"Plan peak · week of {_wk(pk['WK'])}", fnum(pk["UNITS"], ",.0f"), f"{pk['VS']:+.0f}% vs recent", "")
                 + kpi_card("Weeks at a ramp", f"{int((plan['VS'] >= th['ramp']).sum())} of {len(plan)}", None, "",
                            f"Weeks {th['ramp']:.0f}%+ above recent shipped") + "</div>")
        s.append(svg_chart([_wk(w, "%m/%d") for w in plan["WK"]], [("Planned units", list(plan["UNITS"]), cols)], kind="bar",
                           yfmt=",.0f", rules=[(ol["recent"], "Last 4 weeks shipped (avg)", "var(--axis)")], height=300,
                           width=1200, title="Planned outbound units by week"))
        s.append(f"<p class='cap'>Latest publish ({_wk(ctx['publish'])}), each FC's plan scaled to shipped units with its latest "
                 f"overlap week. Orange = {th['ramp']:.0f}%+ above the last 4 full weeks shipped.</p>")
    mw = ol.get("met")
    if mw is not None and len(mw):
        if mw["MET_H"].sum() > 0:
            pkm = mw.loc[mw["MET_H"].idxmax()]
            n_met = int((mw["MET_H"] > 0).sum())
            s.append("<h2>MET scheduled ahead · staffing plan</h2><div class='kpis'>"
                     + kpi_card("Weeks with MET planned", f"{n_met} of {len(mw)}")
                     + kpi_card(f"Most MET · week of {_wk(pkm['WK'])}", fnum(pkm["MET_H"], ",.0f", " h"),
                                f"{fnum(pkm['PCT'], '.1f', '%')} of planned regular hours", "")
                     + kpi_card("MET planned, all weeks", fnum(mw["MET_H"].sum(), ",.0f", " h")) + "</div>"
                     + svg_chart([_wk(w, "%m/%d") for w in mw["WK"]], [("MET hours", list(mw["MET_H"]), P[7])], kind="bar",
                                 yfmt=",.0f", height=230, width=1200, legend=False, title="MET hours planned by week")
                     + "<p class='cap'>Mandatory extra time in the latest network staffing-plan run "
                       "(STAFF_DAILY_DEPARTMENT), by week. UKG has no separate MET pay code, so the plan is the MET source.</p>")
        else:
            s.append("<h2>MET scheduled ahead · staffing plan</h2><p class='cap'>No MET in the latest staffing-plan run for "
                     "these sites.</p>")
    if proj is not None and len(proj):
        s.append("<h2>Projected hours and injuries through peak</h2><div class='two'>")
        rules = [(1.0, "Normal", "var(--axis)")]
        if pd.notna(ctx["watch"]):
            rules += [(ctx["watch"], "Model scenario (+10% rate)", P[3]), (ctx["act"], "Model scenario (+25% rate)", P[7])]
        s.append("<div><h3>Weekly hours ÷ normal</h3>" + svg_chart([_wk(w, "%m/%d") for w in proj["WK"]],
                 [("Projected hours load", list(proj["LOAD"]), P[1])], yfmt=".2f", zero=False, rules=rules, height=230) + "</div>")
        sw = ctx["sw"][ctx["sw"]["SITE"].isin(sites)]
        first = ctx["cur_wk"] - pd.Timedelta(weeks=13)
        act_w = sw[(sw["WK"] >= first) & (sw["WK"] < ctx["cur_wk"])].groupby("WK")[PEAK_METRIC].sum()
        ly = sw.assign(WK=sw["WK"] + pd.Timedelta(days=364)).groupby("WK")[PEAK_METRIC].sum()
        weeks = sorted(set(act_w.index) | set(proj["WK"]))
        pj = proj.set_index("WK")
        s.append("<div><h3>Injuries per week · recordable + first aid</h3>" + svg_chart(
            [_wk(w, "%m/%d") for w in weeks],
            [("Actual", [act_w.get(w, np.nan) for w in weeks], P[0]),
             ("Projected", [pj["EXP"].get(w, np.nan) for w in weeks], P[1]),
             ("Last year", [ly.get(w, np.nan) for w in weeks], "#9a9893")],
            yfmt=",.0f", height=230, band=([pj["LO"].get(w, np.nan) for w in weeks], [pj["HI"].get(w, np.nan) for w in weeks], P[1], "80% process-noise range"))
            + "</div></div>")
        pk = proj.loc[proj["EXP"].idxmax()]
        s.append(f"<p class='cap'>Hours from the plan (hours grow with units^{ctx['eps']:.2f}); risk from the network's own "
                 f"hours-to-injury curve since Jun 2025 (each 10% of hours above normal adds about {np.exp(ctx['b'] * 0.1) - 1:.0%}). "
                 f"Normal week ≈ {fnum(ol.get('normal'), '.0f')} injuries; highest projected week {_wk(pk['WK'])} at "
                 f"{fnum(pk['EXP'], '.0f')} (80% process-noise range {fnum(pk['LO'], '.0f')}–{fnum(pk['HI'], '.0f')}). Scenario lines apply to the 2-week average.</p>")
    # Execution vs plan, last 12 weeks
    a = scope_weekly(pw[pw["SITE"].isin(sites)]).tail(12) if len(pw) else pd.DataFrame()
    if len(a):
        x = [_wk(w, "%m/%d") for w in a["WK"]]
        s.append("<h2>Last 12 weeks · execution vs plan</h2><div class='two'>")
        s.append("<div><h3>Volume and hours vs plan (%)</h3>" + svg_chart(x, [("Volume vs plan", list(a["VOL_PCT"]), P[0]),
                 ("Hours vs plan", list(a["HRS_PCT"]), P[1])], yfmt="+.1f", rules=[(0, "Plan", "var(--axis)")], height=210) + "</div>")
        s.append("<div><h3>Workload pressure (pts)</h3>" + svg_chart(x, [("Pressure", list(a["PRESSURE"]), P[0])], kind="bar",
                 yfmt="+.1f", rules=[(th["press_watch"], "Watch", P[3]), (th["press_elev"], "High", "#d76b1c")], height=210) + "</div></div>")
        s.append("<div class='two'><div><h3>Pace and inbound vs plan (%)</h3>" + svg_chart(
            x, [("Pace vs plan", list(a["TPH_PCT"]), P[1]), ("Inbound vs plan", list(a["IB_PCT"]), P[2])], yfmt="+.1f",
            rules=[(0, "Plan", "var(--axis)")], height=210) + "</div><div><h3>Oldest backlog container (h)</h3>" + svg_chart(
            x, [("Oldest container", list(a["BL_AGE"]), P[6])], kind="bar", yfmt=",.0f", rules=[(th["bl_age"], "Backlog line", P[7])],
            height=210) + "</div></div>")
        # Productivity table
        prod = a[["WK", "OB_UPH", "OB_TPH", "OB_TLPH", "OB_CPLH", "IB_UPH", "IB_TPH", "IB_TLPH", "IB_CPLH"]].iloc[::-1].copy()
        prod["WK"] = prod["WK"].dt.strftime("%b %d, %Y")
        f0 = lambda v: fnum(v, ",.0f")   # noqa: E731
        s.append("<h2>Productivity · inbound and outbound</h2>" + html_table(prod, [
            ("WK", "Week of", None), ("OB_UPH", "OB UPH", f0), ("OB_TPH", "OB TPH", f0), ("OB_TLPH", "OB TLPH", f0),
            ("OB_CPLH", "OB CPLH", lambda v: fnum(v, ",.1f")), ("IB_UPH", "IB UPH", f0), ("IB_TPH", "IB TPH", f0),
            ("IB_TLPH", "IB TLPH", f0), ("IB_CPLH", "IB CPLH", lambda v: fnum(v, ",.1f"))]))
        s.append("<p class='cap'>CLMS labor: UPH and TPH use direct hours; TLPH uses all labor hours (direct + indirect). "
                 "CPLH = shipped containers (outbound) or case-level transactions (inbound) per labor hour.</p>")
        # Labor into peak
        s.append("<h2>Labor going into peak</h2><div class='two'><div><h3>Extra time and VTO · % of regular hours</h3>"
                 + svg_chart(x, [("Overtime", list(a["OT_PCT"]), P[0]), ("MET", list(a["MET_PCT"]), P[7]),
                                 ("VET", list(a["VET_PCT"]), P[2]), ("VTO", list(a["VTO_PCT"]), "#9a9893")], yfmt=".1f", height=210)
                 + "</div><div><h3>New hires and high-strain over-utilization</h3><p class='pcap'>New hires per week</p>"
                 + svg_chart(x, [("New hires", list(a["NEW_HIRES"]), P[2])], kind="bar", yfmt=",.0f", height=95, legend=False)
                 + "<p class='pcap'>Over-utilized high-strain TM-paths (%, over 106 h in 4 weeks)</p>"
                 + svg_chart(x, [("Over-utilized TM-paths %", list(a["OVER_UTIL"]), P[1])], yfmt=".0f", height=95, legend=False)
                 + "</div></div>")
    # Worked-not-trained, all paths
    wn = ctx["wnt_all"]
    if wn is not None and len(wn):
        wl = wn[(wn["LVL"] == "L28") & wn["SITE"].isin(sites)].groupby("TRAINING", as_index=False)[["HOURS", "PEOPLE"]].sum()
        wl = wl.sort_values("HOURS", ascending=False).head(10)
        if len(wl):
            s.append("<h2>Logged into a path without the training · last 28 days</h2>"
                     + svg_hbars(list(wl["TRAINING"].str.replace(r"^FC Gen\s?\d\s?-\s?", "", regex=True).str.slice(0, 34)),
                                 list(wl["HOURS"]), P[7], ",.0f", notes=[f"{int(p_)} people" for p_ in wl["PEOPLE"]], width=1200)
                     + "<p class='cap'>PUMA worked-not-trained, every process path (not just powered equipment). Hours by "
                       "people without the matching training on record.</p>")
    # Sites through peak
    stb = ol.get("sites")
    if stb is not None and len(stb) and kind != "site":
        s.append("<h2>Sites through peak</h2>" + html_table(stb, [
            ("SITE", "Site", None), ("PEAK_WK", "Peak week", lambda v: _wk(v)), ("PEAK_EXP", "Peak-week injuries", lambda v: fnum(v, ".1f")),
            ("MAX_LOAD", "Max 2-wk hours", lambda v: fnum(v, ".2f", "×")), ("N_WATCH", "Wks ≥ watch", lambda v: fnum(v, ".0f")),
            ("N_ACT", "Wks ≥ act", lambda v: fnum(v, ".0f")), ("BASE_HRS", "Normal hrs/wk", lambda v: fnum(v, ",.0f")),
            ("CAP", "Scenario hours/wk", lambda v: fnum(v, ",.0f")), ("TOTAL", "Through peak", lambda v: fnum(v, ".0f"))]))
    # Paths forward
    recs = []
    if proj is not None and len(proj):
        pk = proj.loc[proj["EXP"].idxmax()]
        recs.append(f"<b>Plan for the week of {_wk(pk['WK'])} as the highest projected-count week:</b> about {fnum(pk['EXP'], '.0f')} injuries "
                    f"projected vs {fnum(ol.get('normal'), '.0f')} in a normal week. Put extra EHS floor coverage and daily PIT "
                    "huddles on that week and the one after.")
    if stb is not None and len(stb):
        ha = stb[stb["N_ACT"] > 0]["SITE"].tolist()
        hw = stb[(stb["N_WATCH"] > 0) & (stb["N_ACT"] == 0)]["SITE"].tolist()
        if ha:
            recs.append(f"<b>Over the act line: {E(', '.join(ha))}.</b> Spread volume (pre-build, added shifts or temps) or cap "
                        "weekly hours near the scenario hours.")
        if hw:
            recs.append(f"<b>Over the watch line: {E(', '.join(hw))}.</b> Schedule breaks and task rotation for extended shifts.")
    if plan is not None and len(plan):
        ramp = plan[plan["VS"] >= th["ramp"]]
        if len(ramp):
            recs.append(f"<b>Ramp ahead:</b> {len(ramp)} plan week(s) {th['ramp']:.0f}%+ above recent volume, first the week of "
                        f"{_wk(ramp['WK'].iloc[0])}. Staff for the ramp week: hiring and training lead times, PIT certifications, equipment counts.")
    if len(a):
        l4, p13 = a.tail(4), a.iloc[:-4]
        ot4, ot13 = l4["OT_H"].sum() / max(l4["REG_H"].sum(), 1) * 100, p13["OT_H"].sum() / max(p13["REG_H"].sum(), 1) * 100
        if ot13 > 0 and ot4 >= 1.25 * ot13:
            recs.append(f"<b>Overtime is climbing:</b> {ot4:.1f}% of regular hours over the last 4 weeks vs {ot13:.1f}% before. "
                        "Cap back-to-back MET days and watch PIT operators late in extended shifts.")
        ou = l4["CLMS_OVER_TM_PATHS"].sum() / max(l4["CLMS_STRAIN_TM_PATHS"].sum(), 1) * 100
        if ou >= 30:
            recs.append(f"<b>Rotation:</b> {ou:.0f}% of high-strain TM-paths ran over 106 hours in 4 weeks. Rotate truck/line "
                        "loading and multis ground picking with lighter paths (Network Rotation Tool).")
    if mw is not None and len(mw) and mw["MET_H"].sum() > 0:
        mweeks = mw[mw["MET_H"] > 0]
        recs.append(f"<b>MET planned in {len(mweeks)} week(s)</b>, first the week of {_wk(mweeks['WK'].iloc[0])} and heaviest the week "
                    f"of {_wk(pkm['WK'])} ({fnum(pkm['PCT'], '.1f', '%')} of planned regular hours). Cap back-to-back MET days, "
                    "rotate high-strain paths inside MET shifts and add PIT spot checks late in the shift.")
    if wn is not None and len(wn):
        wsum = wn[(wn["LVL"] == "L28") & wn["SITE"].isin(sites)]["HOURS"].sum()
        if wsum > 0:
            recs.append(f"<b>Training gaps:</b> {wsum:,.0f} hours in the last 28 days were logged into paths without the matching "
                        "training on record. Reconcile path assignments with LMS before peak hiring adds more.")
    if recs:
        s.append("<h2>Paths forward</h2><div class='call'><ol>" + "".join(f"<li>{r}</li>" for r in recs) + "</ol></div>")
    return "".join(s) or "<p class='cap'>No outlook data for these sites.</p>"


def _corr_cell(r, p_):
    if r is None or not np.isfinite(r):
        return "<td class='hm'>—</td>"
    a = min(abs(r) / 0.4, 1)
    col = f"rgba(227,73,72,{0.12 + 0.6 * a:.2f})" if r > 0 else f"rgba(42,120,214,{0.12 + 0.6 * a:.2f})"
    sig = "<span class='sig'>●</span>" if p_ < 0.05 else ""
    return f"<td class='hm' style='background:{col}' title='r = {r:+.2f}, p = {p_:.3f}'>{r:+.2f}{sig}</td>"


def tab_xsafety(ctx, sites, label, kind, ol, pw):
    p = pw[pw["SITE"].isin(sites)]
    basis = ctx["basis"]
    s = ["<h2>How operations, labor and rotation move with injuries</h2>"]
    if len(p) < 30:
        return s[0] + "<p class='cap'>Not enough full site-weeks for these sites.</p>"
    multi = p["SITE"].nunique() > 1
    rkey, pkey = ("R_WITHIN", "P_WITHIN") if multi else ("RHO", "P")
    s.append(f"<p class='cap'>{p['SITE'].nunique()} site{'s' if multi else ''} · {len(p):,} full site-weeks "
             f"({_wk(p['WK'].min(), '%b %Y')} – {_wk(p['WK'].max(), '%b %d, %Y')}). "
             + ("Each site's weeks are compared with its own average, so big and small sites don't drive the result. "
                if multi else "One site: its weeks ranked against each other. ")
             + "Correlation shows direction and strength, not cause: under 0.1 negligible, 0.1–0.3 weak, 0.3–0.5 moderate, 0.5+ strong.</p>")
    mats_by_lag = {lag: {k: correlations(p, k, lag) for k in OUTCOMES} for lag in (0, 1)}
    find = []
    for lag in (0, 1):
        for oc in ("INJ_RATE", "MSDR", "MOER"):
            find += top_findings(mats_by_lag[lag][oc], OUTCOMES[oc][0], lag, k=2)
    s.append("<div class='call'><b>Strongest relationships</b><ul>" + ("".join(f"<li>{E(f)}</li>" for f in find[:8]) if find else
             "<li>No driver clears r ≥ 0.10 with p &lt; 0.05 on 30+ site-weeks yet.</li>") + "</ul></div>")
    # Correlation map, same week / next week toggle
    for lag in (0, 1):
        mats = mats_by_lag[lag]
        head = "".join(f"<th>{E(v[0])}</th>" for v in OUTCOMES.values())
        rows = []
        for grp in DRIVER_GROUPS:
            rows.append(f"<tr class='grp'><td colspan='{len(OUTCOMES) + 2}'>{E(grp)}</td></tr>")
            for d, (lab, g_, unit, f_, note) in DRIVERS.items():
                if g_ != grp:
                    continue
                cells, nn = [], 0
                for k in OUTCOMES:
                    m = mats[k]
                    r = m[m["DRIVER"] == d]
                    if len(r):
                        cells.append(_corr_cell(r[rkey].iloc[0], r[pkey].iloc[0]))
                        nn = max(nn, int(r["N"].iloc[0]))
                    else:
                        cells.append("<td class='hm'>—</td>")
                rows.append(f"<tr><td class='t' title='{E(note)}'>{E(lab)}</td>{''.join(cells)}<td>{nn:,}</td></tr>")
        s.append(f"<div class='lagpane' data-lag='{lag}'{'' if lag == 0 else ' hidden'}><div class='scroll'><table class='hmt'>"
                 f"<thead><tr><th class='t'>Driver</th>{head}<th>Site-weeks</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></div>")
    s.insert(len(s) - 2, "<h2>Correlation map</h2><div class='seg' role='tablist'><button class='on' data-lagbtn='0'>Same week</button>"
                         "<button data-lagbtn='1'>Next week</button></div>")
    s.append("<p class='cap'>Red = injuries tend to rise with the driver; blue = fall. ● = p &lt; 0.05. Next week = the driver "
             "this week against injuries the following week at the same site. Hover a row name for how it's measured.</p>")
    s.append(ledger_html(ctx, sites, kind))
    # Quartiles for the top drivers
    ct0 = mats_by_lag[0]["INJ_RATE"]
    if len(ct0):
        ct0 = ct0.assign(_A=ct0[rkey].abs()).sort_values("_A", ascending=False)
        figs = []
        for d in ct0["DRIVER"].head(4):
            qr = quartile_rates(p, d, "INJ_RATE", 0, basis)
            if qr.empty:
                continue
            lab, _g, unit, f_, _n = DRIVERS[d]
            labs = [f"Q{int(q)}\n{fnum(a, f_)}–{fnum(b, f_)}" for q, a, b in zip(qr["Q"], qr["LO_V"], qr["HI_V"])]
            figs.append(f"<div><h3>{E(lab)}{(' (' + E(unit) + ')') if unit else ''}</h3>"
                        + svg_ci_bars(labs, list(qr["RATE"]), list(qr["CI_LO"]), list(qr["CI_HI"]), P[0], ".1f", width=600, height=230, ylab="Injury rate")
                        + "</div>")
        if figs:
            s.append("<h2>Injury rate by quarter of the strongest drivers</h2><div class='grid2'>" + "".join(figs) + "</div>"
                     f"<p class='cap'>Recordable + first-aid injuries per {basis:,.0f} hours, pooled for the site-weeks in each quarter of "
                     "the driver; whiskers = 95% interval.</p>")
    # Injuries and the strongest drivers in one chart each (ledger rows first, then the strongest correlations)
    led = scope_ledger(ctx, sites, kind)
    rows_ = []
    if led is not None and len(led):
        tl = led[led["VERDICT"].isin(["Exploratory after correction", "Suggestive"]) & np.isfinite(led["IRR"])].copy()
        tl["_A"] = np.abs(np.log(tl["IRR"]))
        rows_ = tl.sort_values(["Q", "_A"], ascending=[True, False]).drop_duplicates("DRIVER").head(4).to_dict("records")
    if len(rows_) < 2 and len(ct0):
        seen = {r_["DRIVER"] for r_ in rows_}
        rows_ += [{"DRIVER": d_, "OUTCOME": "INJ_RATE", "LAG": 0} for d_ in ct0["DRIVER"] if d_ not in seen][:4 - len(rows_)]
    s.append(paired_block_html(p, rows_, ctx["lw"], weeks=52, title="Injuries and the strongest drivers by week"))
    # Triggers vs non-triggers
    ow = ctx["ow"][ctx["ow"]["SITE"].isin(sites)]
    tc = trigger_compare(p, ow, ctx["th"], basis, 0)
    tc1 = trigger_compare(p, ow, ctx["th"], basis, 1)
    if len(tc):
        nx = {"INJ_RATE_RATIO": "INJ_NEXT", "INJ_RATE_LO": "INJ_NEXT_LO", "INJ_RATE_HI": "INJ_NEXT_HI",
              "MSDR_RATIO": "MSD_NEXT", "MSDR_LO": "MSD_NEXT_LO", "MSDR_HI": "MSD_NEXT_HI"}
        tc = tc.merge(tc1[["RULE"] + list(nx)].rename(columns=nx), on="RULE", how="left")
        tc["CI"] = [f"<span class='nw'>{a:.2f}–{b:.2f}</span>" if np.isfinite(a) else "—"
                    for a, b in zip(tc["INJ_RATE_LO"], tc["INJ_RATE_HI"])]

        def rx(v, lo, hi):   # colour only when the 95% interval clears 1.00
            if v is None or not np.isfinite(v):
                return "—"
            tone = "bad" if np.isfinite(lo) and lo > 1 else "good" if np.isfinite(hi) and hi < 1 else ""
            return f"<b class='{tone}'>{fnum(v, '.2f', '×')}</b>"
        for col, lo, hi, out in [("INJ_RATE_RATIO", "INJ_RATE_LO", "INJ_RATE_HI", "R1"), ("INJ_NEXT", "INJ_NEXT_LO", "INJ_NEXT_HI", "R2"),
                                 ("MSDR_RATIO", "MSDR_LO", "MSDR_HI", "R3"), ("MSD_NEXT", "MSD_NEXT_LO", "MSD_NEXT_HI", "R4")]:
            tc[out] = [rx(v, l_, h_) for v, l_, h_ in zip(tc[col], tc[lo], tc[hi])]
        f2 = lambda v: fnum(v, ".2f")   # noqa: E731
        s.append("<h2>Triggers vs non-triggers</h2>" + html_table(tc, [
            ("TITLE", "Trigger", None), ("FIRED", "FC-weeks fired", lambda v: fnum(v, ",.0f")),
            ("INJ_RATE_F", "Injury rate when fired", f2), ("INJ_RATE_NF", "Injury rate otherwise", f2), ("R1", "Ratio", "rhtml"),
            ("CI", "95%", "rhtml"), ("R2", "Ratio next week", "rhtml"),
            ("MSDR_F", "MSD rate when fired", f2), ("MSDR_NF", "MSD rate otherwise", f2), ("R3", "MSD ratio", "rhtml"),
            ("R4", "MSD ratio next week", "rhtml")]))
        s.append(f"<p class='cap'>Pooled rates (per {basis:,.0f} hours) in FC-weeks where each trigger fired vs every other FC-week. "
                 f"A ratio of 1.25× means 25% more injuries per hour when it fired. Red or green only when the 95% interval clears 1.00; "
                 f"ratios need {MIN_FIRED}+ fired FC-weeks.</p>")
    h52 = ctx.get("hist52")
    sr = status_rates(p, h52[h52["SITE"].isin(sites)], basis) if h52 is not None and len(h52) else pd.DataFrame()
    if len(sr):
        f2 = lambda v: fnum(v, ".2f")   # noqa: E731
        sr["CHIP"] = sr["STATUS"].map(_status_chip)
        s.append("<h3>Rates by OVSR status · last 52 weeks</h3>" + html_table(sr, [
            ("CHIP", "Status that week", "html"), ("WEEKS", "Site-weeks", lambda v: fnum(v, ",.0f")),
            ("INJ_RATE", "Injury rate", f2), ("TRIR", "Recordable rate", f2), ("MSDR", "MSD rate", f2), ("MOER", "MOE rate", f2)]))
    # Mediation
    meds = []
    for x, m, y in MEDIATION_PATHS:
        r = mediation(p, x, m, y, 0, boots=200)
        if r:
            meds.append(r)
    if meds:
        meds.sort(key=lambda r: (not r["ind_sig"], -abs(r["ind"])))
        s.append("<h2>Mediation · does the effect run through a middle step?</h2><p class='cap'>Predictor (X) = the starting "
                 "cause or exposure; Mediator (M) = the mechanism that carries its effect; Outcome (Y) = the result. a = X → M, "
                 "b = M → Y holding X fixed, c′ = direct effect, c = total. Standardized; * p &lt; 0.05, ** p &lt; 0.01. "
                 "The indirect effect a×b counts when its 95% bootstrap interval excludes zero.</p><div class='grid2'>")
        for r in meds[:2]:
            s.append(f"<div>{mediation_svg(r)}<p class='cap'>{E(mediation_sentence(r))}</p></div>")
        s.append("</div>")
        md = pd.DataFrame([{"X": _lab(r["x"]), "M": _lab(r["m"]), "Y": _lab(r["y"]), "a": r["a"], "b": r["b"], "c": r["c"],
                            "cp": r["cp"], "ind": r["ind"], "ci": f"{r['ind_lo']:+.3f} to {r['ind_hi']:+.3f}",
                            "sig": "Yes" if r["ind_sig"] else "No", "n": r["n"]} for r in meds])
        f3 = lambda v: fnum(v, "+.2f")   # noqa: E731
        s.append(html_table(md, [("X", "Predictor (X)", None), ("M", "Mediator (M)", None), ("Y", "Outcome (Y)", None),
                                 ("a", "a", f3), ("b", "b", f3), ("c", "Total c", f3), ("cp", "Direct c′", f3),
                                 ("ind", "Indirect a×b", lambda v: fnum(v, "+.3f")), ("ci", "95%", lambda v: E(v)),
                                 ("sig", "Path holds?", lambda v: E(v)), ("n", "Site-weeks", lambda v: fnum(v, ",.0f"))]))
    # Injury profile for the selected period
    inj = ctx["inj"]
    if inj is not None and len(inj):
        rng = ctx["rng"]
        d = injury_profile(inj, sites, rng[0], rng[-1])
        try:
            s.append(mech_html(ctx, sites))
        except Exception as exc:
            s.append(f"<p class='cap'>Mechanism section couldn't be built: {E(type(exc).__name__)}.</p>")
        try:
            s.append(paths_html(ctx, sites))
        except Exception as exc:
            s.append(f"<p class='cap'>Paths section couldn't be built: {E(type(exc).__name__)}.</p>")
        try:
            s.append(items_html(ctx, sites))
        except Exception as exc:
            s.append(f"<p class='cap'>Items section couldn't be built: {E(type(exc).__name__)}.</p>")
        s.append(f"<h2>Injury profile · {_wk(rng[0])} – {_wk(rng[-1] + pd.Timedelta(days=6), '%b %d, %Y')}</h2>"
                 f"<p class='cap'>{int(d['INJURIES'].sum()):,} work-related injuries (non-occupational excluded). No names, "
                 "narratives or IDs.</p>")
        dc = dim_counts(d, "DEPT")
        dh = ctx["dept_h"]
        if dh is not None and len(dh):
            hh = dh[dh["SITE"].isin(sites) & dh["WK"].isin(rng)].groupby("DEPT")["HRS"].sum()
            dc["RATE"] = dc["INJURIES"] * basis / dc["DEPT"].map(hh).where(lambda v: v > 0)
        fi = lambda v: fnum(v, ",.0f")   # noqa: E731
        fs_ = lambda v: fnum(v, ".0f", "%")   # noqa: E731
        s.append("<div class='two'><div><h3>By department</h3>" + html_table(dc, [
            ("DEPT", "Department", None), ("INJURIES", "Injuries", fi), ("MSD", "MSD", fi), ("RECORDABLES", "Recordable", fi),
            ("SHARE", "Share", fs_), ("RATE", "Rate per 200K CLMS hrs", lambda v: fnum(v, ".1f"))]) + "</div>"
            + "<div><h3>By position</h3>" + html_table(dim_counts(d, "ROLE", 8), [
                ("ROLE", "Position", None), ("INJURIES", "Injuries", fi), ("MSD", "MSD", fi), ("SHARE", "Share", fs_)]) + "</div></div>")
        et = dim_counts(d, "EXTRA_TIME")
        ps = p[p["WK"].isin(rng)]
        n_ext = et.loc[et["EXTRA_TIME"].isin(["MET", "VET"]), "INJURIES"].sum()
        n_reg = et.loc[et["EXTRA_TIME"] == "Regular time", "INJURIES"].sum()
        re_ = _rate(n_ext, ps["OT_H"].sum(), basis)
        rr_ = _rate(n_reg, ps["REG_H"].sum(), basis)
        s.append("<h3>Extra time</h3><div class='kpis'>"
                 + kpi_card("Injuries on MET", fnum(et.loc[et["EXTRA_TIME"] == "MET", "INJURIES"].sum(), ",.0f"))
                 + kpi_card("Injuries on VET", fnum(et.loc[et["EXTRA_TIME"] == "VET", "INJURIES"].sum(), ",.0f"))
                 + kpi_card("Rate on extra time", fnum(re_, ".1f"),
                            (f"{re_ / rr_:.2f}× regular" if np.isfinite(re_) and np.isfinite(rr_) and rr_ > 0 else None),
                            "bad" if np.isfinite(re_) and np.isfinite(rr_) and re_ > rr_ else "", "MET + VET injuries ÷ UKG Overtime hours")
                 + kpi_card("Rate on regular time", fnum(rr_, ".1f"), None, "", "Regular-time injuries ÷ UKG Regular hours") + "</div>")
        hp = dim_counts(d, "HOURS_IN_PATH").set_index("HOURS_IN_PATH").reindex(["0-1 h", "2-3 h", "4-5 h", "6-7 h", "8 h +"]).fillna(0)
        tn = dim_counts(d, "TENURE").set_index("TENURE").reindex(["Under 30 days", "30-89 days", "90 days - 1 year", "1 year +"]).fillna(0)
        s.append("<div class='two'><div><h3>Hours into the path when injured</h3>" + svg_chart(
            list(hp.index), [("Injuries", list(hp["INJURIES"]), P[1]), ("MSD", list(hp["MSD"]), P[0])], kind="bar", yfmt=",.0f",
            height=200) + "</div><div><h3>Tenure at injury</h3>" + svg_chart(
            list(tn.index), [("Injuries", list(tn["INJURIES"]), P[2])], kind="bar", yfmt=",.0f", height=200, legend=False) + "</div></div>")
        bits = []
        for dim, title in (("IN_PATH", "Job path"), ("SHARED", "Labor sharing"), ("PRODUCT", "Chewy product")):
            t_ = dim_counts(d, dim)
            bits.append("<div><h3>" + E(title) + "</h3>" + html_table(t_, [(dim, "", None), ("INJURIES", "Injuries", fi), ("SHARE", "Share", fs_)]) + "</div>")
        s.append("<div class='grid3'>" + "".join(bits) + "</div>")
        s.append("<div class='two'><div><h3>Top injury types</h3>" + html_table(dim_counts(d, "INJURY_TYPE", 6), [
            ("INJURY_TYPE", "Type", None), ("INJURIES", "Injuries", fi), ("MSD", "MSD", fi)]) + "</div><div><h3>Top causes</h3>"
            + html_table(dim_counts(d, "CAUSE", 6), [("CAUSE", "Cause", None), ("INJURIES", "Injuries", fi), ("MSD", "MSD", fi)])
            + "</div></div>")
    return "".join(s)


def tab_site(ctx, sites, label, kind, ol, pw):
    if kind != "site":
        fr = ctx["fr_last"][ctx["fr_last"]["SITE"].isin(sites)].copy()
        fr["_O"] = fr["STATUS"].map(g("STATUS_ORDER"))
        fr = fr.sort_values(["_O", "N_TRIG"], ascending=[True, False])
        links = "".join(f"<button class='sitebtn' data-goto='S-{E(r['SITE'])}'><b>{E(r['SITE'])}</b> {_status_chip(r['STATUS'])}"
                        f"<small>{E(r['TRIGGERS'] or 'No triggers')}</small></button>" for r in fr.to_dict("records"))
        return (f"<h2>Pick a site</h2><p class='cap'>The Site tab shows one site's full picture. Choose a site in the picker at "
                f"the top, or below (status for the week of {_wk(ctx['lw'])}).</p><div class='sitegrid'>{links}</div>")
    site = sites[0]
    th, basis = ctx["th"], ctx["basis"]
    row = ctx["fr_last"][ctx["fr_last"]["SITE"] == site]
    now = ctx["fr_now"][ctx["fr_now"]["SITE"] == site]
    r = row.iloc[0].to_dict() if len(row) else {}
    status = r.get("STATUS", "No ops data")
    s = [f"<div class='card status'><div><h2 style='margin:0'>{E(site)}</h2>{_status_chip(status)}<p class='cap'>"
         f"{E(ctx['regions'].get(site, ''))} · week of {_wk(ctx['lw'])} (complete)"
         + (f" · this week so far: {_status_chip(now['STATUS'].iloc[0])}" if len(now) else "") + "</p></div><div>"]
    why = r.get("WHY") or []
    fired = r.get("FIRED") or []
    if status == "Elevated":
        s.append(f"<p><b>Elevated</b> because {E('; '.join(why))}. Prioritize the response, give each action an owner and a date, "
                 "and re-check next week.</p>")
    elif status == "Moderate Elevation":
        s.append(f"<p><b>Moderate Elevation</b> because {E('; '.join(why))}. Validate the conditions, assign targeted actions and "
                 "re-check next week.</p>")
    elif status == "Watch":
        s.append(f"<p><b>Watch</b>: {len(fired)} trigger{'s' if len(fired) != 1 else ''} fired. Act on it this week so it doesn't "
                 "stack with another one.</p>")
    elif status == "Normal":
        s.append("<p><b>Normal</b>: no trigger fired. Keep the standard cadence (daily safety huddle, PIT pre-shift inspections, "
                 "weekly EHS walk).</p>")
    else:
        s.append("<p><b>No ops data</b>: no safety signal fired, and there's no operating data to score this site's variance.</p>")
    s.append("</div></div><h2>Recommended actions</h2>")
    if fired:
        s.append("<ol class='acts'>" + "".join(
            f"<li><b>{E(g('RESPONSES')[k][0])}</b> · <i>{E(g('RESPONSES')[k][1])}</i><p>{E(g('RESPONSES')[k][2])}</p>"
            f"<p class='ev'>Evidence: {E(g('_ev_text')(k, r, th))}</p></li>" for k in fired) + "</ol>")
    else:
        s.append("<p>Nothing to act on from the triggers that week.</p>")
    wr = ctx["wf_now"].reindex([site]).iloc[0] if ctx["wf_now"] is not None and site in ctx["wf_now"].index else None
    if wr is not None:
        ctxr = [k for k in g("WF_RULES") if pd.notna(wr.get(k, False)) and bool(wr.get(k, False))]
        if wr.get("WNT_H", 0) and wr.get("WNT_H", 0) > 0 and "wnt" not in ctxr:
            ctxr.append("wnt")
        for k in ctxr:
            s.append(f"<p><b>Current context: {E(g('RESPONSES')[k][0])}.</b> {E(g('RESPONSES')[k][2])}</p>")
    s.append(site_drivers_html(ctx, site))
    # KPIs: last full week vs the week before; 13-week rates vs network
    sw = ctx["sw"]
    lw = ctx["lw"]
    t, _ = _sum_period(sw, [site], [lw])
    pv, _ = _sum_period(sw, [site], [lw - pd.Timedelta(weeks=1)])
    s.append(f"<h2>Week of {_wk(lw)}</h2><div class='kpis'>")
    for k, lab in (("RECORDABLE_INCIDENTS", "Recordables"), ("FIRST_AID_INCIDENT", "First aid"), ("MSD_INJURIES", "MSD injuries"),
                   ("MOE_INCIDENTS", "MOE incidents")):
        dt, tone = delta_txt(t.get(k, np.nan), pv.get(k, 0), "count")
        s.append(kpi_card(lab, fnum(t.get(k, np.nan), ",.0f"), dt, tone))
    dt, _ = delta_txt(t.get("TOTAL_HOURS", 0), pv.get("TOTAL_HOURS", 0), "hours", worse_up=False)
    s.append(kpi_card("Hours worked", g("fmt_hours")(t.get("TOTAL_HOURS", 0)), dt) + "</div>")
    w13 = ctx["full_wks"][-13:]
    ts_, _ = _sum_period(sw, [site], w13)
    tn_, _ = _sum_period(sw, ctx["pool"], w13)
    s.append(f"<h3>13-week rates vs all sites in scope · {E(ctx['basis_label'])}</h3><div class='kpis'>")
    for k, lab in (("RECORDABLE_INCIDENTS", "Recordable rate"), ("FIRST_AID_INCIDENT", "First-aid rate"), ("MSD_INJURIES", "MSD rate"),
                   ("MOE_INCIDENTS", "MOE rate"), ("PIT_INCIDENTS", "PIT rate")):
        v, n_ = _rate(ts_.get(k, 0), ts_.get("TOTAL_HOURS", 0), basis), _rate(tn_.get(k, 0), tn_.get("TOTAL_HOURS", 0), basis)
        dt, tone = delta_txt(v, n_, "rate")
        s.append(kpi_card(lab, fnum(v, ".2f"), (dt.replace("▲", "▲ vs network").replace("▼", "▼ vs network") if dt else None), tone))
    s.append("</div>")
    o, op = _ops_sum(ctx["ow"], [site], [lw]), _ops_sum(ctx["ow"], [site], [lw - pd.Timedelta(weeks=1)])
    if o:
        s.append("<h3>Operations vs plan · Δ vs the week before</h3><div class='kpis'>")
        for key, lab in (("VOL", "Volume vs plan"), ("HRS", "Hours vs plan"), ("PRESS", "Pressure"), ("TPH", "Pace vs plan"),
                         ("IB", "Inbound vs plan"), ("BL", "Oldest backlog")):
            v, pv_ = o.get(key, np.nan), op.get(key, np.nan) if op else np.nan
            txt = fnum(v, ".0f", " h") if key == "BL" else fnum(v, "+.1f", " pts" if key == "PRESS" else "%")
            d_ = (f"{round(v - pv_, 1) + 0.0:+.1f}{' h' if key == 'BL' else ' pts'}"
                  if np.isfinite(v) and np.isfinite(pv_) and abs(v - pv_) >= 0.05 else None)
            s.append(kpi_card(lab, txt, d_, "bad" if key in ("PRESS", "TPH", "BL") and d_ and v > pv_ else ""))
        s.append("</div>")
    # Weekly picture
    h = ctx["hist"][ctx["hist"]["SITE"] == site]
    strip = []
    for w in ctx["wks12"]:
        rr = h[h["WK"] == w]
        st_ = rr["STATUS"].iloc[0] if len(rr) else None
        tip = f"Week of {_wk(w)} · {st_ or 'no rating'} · fired: {(rr['TRIGGERS'].iloc[0] or 'none') if len(rr) else '—'}"
        strip.append(f"<td><span class='cell big' style='--c:{g('STATUS_COLOR').get(st_, '#e0e0e0')}' title='{E(tip)}'></span>"
                     f"<small>{_wk(w, '%m/%d')}</small></td>")
    s.append("<h2>Last 12 weeks</h2><div class='scroll'><table class='grid strip'><tr>" + "".join(strip) + "</tr></table></div>")
    sh = sw[(sw["SITE"] == site) & sw["WK"].isin(ctx["wks12"])].sort_values("WK")
    x = [_wk(w, "%m/%d") for w in sh["WK"]]
    s.append("<div class='two'><div><h3>Injuries by week</h3>" + svg_chart(x, [
        ("Recordables", list(sh["RECORDABLE_INCIDENTS"]), P[0]), ("First aid", list(sh["FIRST_AID_INCIDENT"]), P[1]),
        ("MSD injuries", list(sh["MSD_INJURIES"]), P[2])], yfmt=",.0f", height=210) + "</div><div><h3>MOE / PIT by week</h3>"
        + svg_chart(x, [("MOE incidents", list(sh["MOE_INCIDENTS"]), P[0]), ("PIT incidents", list(sh["PIT_INCIDENTS"]), P[1]),
                        ("PIT property damage", list(sh["PIT_PROPERTY_DAMAGE"]), P[2])], yfmt=",.0f", height=210) + "</div></div>")
    oh = ctx["ow"][(ctx["ow"]["SITE"] == site) & ctx["ow"]["WK"].isin(ctx["wks12"])].sort_values("WK")
    if len(oh):
        xo = [_wk(w, "%m/%d") for w in oh["WK"]]
        s.append("<div class='two'><div><h3>Volume and hours vs plan (%)</h3>" + svg_chart(xo, [
            ("Volume vs plan", list(oh["VOL_PCT"]), P[0]), ("Hours vs plan", list(oh["HRS_PCT"]), P[1])], yfmt="+.1f",
            rules=[(0, "Plan", "var(--axis)"), (th["hrs_short"], "Hours-short line", P[7])], height=210)
            + "</div><div><h3>Workload pressure (pts)</h3>" + svg_chart(xo, [("Pressure", list(oh["PRESSURE"]), P[0])], kind="bar",
            yfmt="+.1f", rules=[(th["press_watch"], "Watch", P[3]), (th["press_elev"], "High", "#d76b1c")], height=210) + "</div></div>")
    ps = pw[pw["SITE"] == site].sort_values("WK").tail(12)
    if len(ps):
        xp = [_wk(w, "%m/%d") for w in ps["WK"]]
        s.append("<div class='two'><div><h3>Extra time · % of regular hours</h3>" + svg_chart(xp, [
            ("Overtime", list(ps["OT_PCT"]), P[0]), ("MET", list(ps["MET_PCT"]), P[7]), ("VTO", list(ps["VTO_PCT"]), "#9a9893")],
            yfmt=".1f", height=200) + "</div><div><h3>Outbound UPH and high-strain over-utilization</h3>"
            + "<p class='pcap'>Outbound UPH</p>"
            + svg_chart(xp, [("Outbound UPH", list(ps["OB_UPH"]), P[0])], yfmt=",.0f", height=95, legend=False, zero=False)
            + "<p class='pcap'>Over-utilized high-strain TM-paths (%, over 106 h in 4 weeks)</p>"
            + svg_chart(xp, [("Over-utilized %", list(ps["OVER_UTIL"]), P[1])], yfmt=".0f", height=95, legend=False) + "</div></div>")
    if wr is not None:
        s.append("<h3>Workforce · latest</h3><div class='kpis'>"
                 + kpi_card("Attrition vs site norm", fnum(wr.get("ATTR_NOW"), ".2f", "×"))
                 + kpi_card("Attrition, annualized", fnum(wr.get("ATTR_ANN"), ".0f", "%"))
                 + kpi_card("Hourly under 90 days", fnum(wr.get("NEW90_NOW"), ".1f", "%"))
                 + kpi_card("Worked-not-trained hrs (28 d, PIT)", fnum(wr.get("WNT_H"), ",.0f")) + "</div>")
    return "".join(s)


def tab_yesterday(ctx, sites, label, kind, ol, pw):
    d = ctx["dfact"][ctx["dfact"]["SITE"].isin(sites)]
    D = ctx["day"]
    R = D - pd.Timedelta(days=7)
    basis = ctx["basis"]
    sums = [c for c in g("ALL_COUNTS") + ["TOTAL_HOURS"] if c in d.columns]
    t, r = d[d["DT"] == D][sums].sum(), d[d["DT"] == R][sums].sum()
    s = [f"<h2>{D:%A, %B %d, %Y}</h2><p class='cap'>Δ vs the same day last week ({R:%a %b %d}). Daily OBR table (FC &amp; Rx sites).</p>"
         "<div class='kpis'>"]
    for k, lab in (("RECORDABLE_INCIDENTS", "Recordables"), ("FIRST_AID_INCIDENT", "First aid"), ("MSD_INJURIES", "MSD injuries"),
                   ("MOE_INCIDENTS", "MOE incidents"), ("PIT_INCIDENTS", "PIT incidents"), ("PIT_PROPERTY_DAMAGE", "PIT prop. damage")):
        dt, tone = delta_txt(t.get(k, np.nan), r.get(k, 0), "count")
        s.append(kpi_card(lab, fnum(t.get(k, np.nan), ",.0f"), dt, tone))
    s.append(kpi_card("Hours worked", g("fmt_hours")(t.get("TOTAL_HOURS", 0))) + "</div>")
    # Site signals (same rules as the app's Yesterday tab)
    d7s = D - pd.Timedelta(days=6)
    w7 = d[(d["DT"] >= d7s) & (d["DT"] <= D)]
    p28 = d[(d["DT"] >= d7s - pd.Timedelta(days=28)) & (d["DT"] < d7s)]
    a7 = w7.groupby("SITE").agg(MOE7=("MOE_INCIDENTS", "sum"), HRS7=("TOTAL_HOURS", "sum"),
                                DAYS7=("MOE_INCIDENTS", lambda x: int((x > 0).sum())), NM7=("NEAR_MISS", "sum"))
    a28 = p28.groupby("SITE").agg(MOE28=("MOE_INCIDENTS", "sum"), HRS28=("TOTAL_HOURS", "sum"))
    ts = a7.join(a28, how="left").fillna(0).reset_index()
    nxt = []
    if len(ts):
        ts["EXP7"] = np.where(ts["HRS28"] > 0, ts["MOE28"] / ts["HRS28"].where(ts["HRS28"] > 0, 1) * ts["HRS7"], ts["MOE28"] / 4)
        dd = d[d["DT"] == D].groupby("SITE")[["MOE_INCIDENTS", "TOTAL_HOURS"]].sum()
        ts["TODAY"] = ts["SITE"].map(dd["MOE_INCIDENTS"]).fillna(0)
        ts["EXP_D"] = np.where(ts["HRS28"] > 0, ts["MOE28"] / ts["HRS28"].where(ts["HRS28"] > 0, 1) * ts["SITE"].map(dd["TOTAL_HOURS"]).fillna(0),
                               ts["MOE28"] / 28)
        ps_ = g("_pois_sf")
        ts["P7"] = [ps_(n, l_) for n, l_ in zip(ts["MOE7"], ts["EXP7"])]
        ts["PD"] = [ps_(n, l_) for n, l_ in zip(ts["TODAY"], ts["EXP_D"])]
        ts["EXP_DAYS"] = 7 * (1 - np.exp(-ts["EXP7"] / 7))
        ts["RISING"] = (ts["MOE7"] >= 3) & (ts["P7"] < 0.05)
        ts["REPEAT"] = (ts["DAYS7"] >= 3) & (ts["DAYS7"] >= ts["EXP_DAYS"] + 2)
        ts["SPIKE"] = (ts["TODAY"] >= 2) & (ts["PD"] < 0.05)
        for x in ts[ts["RISING"] | ts["REPEAT"] | ts["SPIKE"]].sort_values("MOE7", ascending=False).head(6).to_dict("records"):
            why = []
            if x["SPIKE"]:
                why.append(f"{x['TODAY']:.0f} MOE incidents on {D:%b %d} vs about {x['EXP_D']:.1f} on a normal day")
            if x["RISING"]:
                why.append(f"{x['MOE7']:.0f} in the last 7 days vs about {x['EXP7']:.1f} expected")
            if x["REPEAT"]:
                why.append(f"MOE incidents on {x['DAYS7']:.0f} of the last 7 days")
            todo = ("Pull the Intelex reports, confirm root cause and a named owner and due date for each corrective action, and "
                    "hold a PIT/MOE huddle on the affected shifts." if (x["RISING"] or x["SPIKE"]) else
                    "Walk the MOE zones (pedestrian separation, rack-end protection, travel speed) and check for a shared cause.")
            nxt.append(f"<b>{E(x['SITE'])}</b>: {E('; '.join(why))}. {E(todo)}")
        n7, nm7 = ts["MOE7"].sum(), ts["NM7"].sum()
        if n7 > 0 and nm7 < n7:
            nxt.append(f"<b>Near-miss reporting is thin</b>: {nm7:.0f} near misses vs {n7:.0f} MOE incidents in the last 7 days. "
                       "Push good-catch reporting on PIT routes.")
    s.append("<h2>What needs attention</h2><div class='call'>" + ("<ol>" + "".join(f"<li>{x}</li>" for x in nxt) + "</ol>" if nxt else
             "<p>No site is running above its own normal MOE rate, and none had repeat MOE days in the last 7 days.</p>") + "</div>")
    win = d[(d["DT"] > D - pd.Timedelta(days=28)) & (d["DT"] <= D)].groupby("DT")[["MOE_INCIDENTS", "RECORDABLE_INCIDENTS",
                                                                                 "FIRST_AID_INCIDENT", "TOTAL_HOURS"]].sum()
    if len(win):
        x = [w.strftime("%m/%d") for w in win.index]
        cols = [P[0] if w == D else "#86b6ef" for w in win.index]
        s.append("<div class='two'><div><h3>MOE incidents · last 28 days</h3>" + svg_chart(
            x, [("MOE incidents", list(win["MOE_INCIDENTS"]), cols)], kind="bar", yfmt=",.0f", height=200, legend=False,
            rules=[(win["MOE_INCIDENTS"].iloc[:-1].mean() if len(win) > 1 else np.nan, "Prior average", "var(--axis)")])
            + "</div><div><h3>Injuries · last 28 days</h3>" + svg_chart(
            x, [("Recordables", list(win["RECORDABLE_INCIDENTS"]), P[0]), ("First aid", list(win["FIRST_AID_INCIDENT"]), P[1])],
            kind="bar", yfmt=",.0f", height=200) + "</div></div>")
    if kind != "site":
        sd = d[d["DT"] == D].groupby("SITE", as_index=False)[sums].sum()
        sd = sd[sd[["MOE_INCIDENTS", "PIT_INCIDENTS", "RECORDABLE_INCIDENTS", "FIRST_AID_INCIDENT", "MSD_INJURIES"]].sum(axis=1) > 0]
        fi = lambda v: fnum(v, ",.0f")   # noqa: E731
        s.append(f"<h3>Sites with an event · {D:%a %b %d}</h3>" + html_table(sd.sort_values("MOE_INCIDENTS", ascending=False), [
            ("SITE", "Site", None), ("MOE_INCIDENTS", "MOE", fi), ("PIT_INCIDENTS", "PIT", fi), ("RECORDABLE_INCIDENTS", "Recordables", fi),
            ("FIRST_AID_INCIDENT", "First aid", fi), ("MSD_INJURIES", "MSD", fi), ("TOTAL_HOURS", "Hours", fi)]))
    return "".join(s)


def tab_safety(ctx, sites, label, kind, ol, pw):
    sw, basis, rng, prev = ctx["sw"], ctx["basis"], ctx["rng"], ctx["prev"]
    t, cur = _sum_period(sw, sites, rng)
    for unavailable_key in g("fact").attrs.get("metric_unavailable", {}):
        t[unavailable_key] = np.nan
    c, _ = _sum_period(sw, sites, prev) if prev else (None, None)
    s = [f"<h2>Selected period · {_wk(rng[0])} – {_wk(rng[-1] + pd.Timedelta(days=6), '%b %d, %Y')}</h2>"
         + ("<p class='cap'>Δ vs " + ("the weeks before" if ctx["compare"] == "Previous period" else "the same weeks last year")
            + ". Red ▲ = more events or a higher rate.</p>" if prev else "<p class='cap'>No comparison.</p>")]
    for grp, keys in (("Injuries", ["RECORDABLE_INCIDENTS", "FIRST_AID_INCIDENT", "MSD_INJURIES", "MSD_RECORDABLES"]),
                      ("MOE / PIT", ["MOE_INCIDENTS", "PIT_INCIDENTS", "PIT_PROPERTY_DAMAGE"])):
        s.append(f"<h3>{grp} · counts</h3><div class='kpis'>")
        for k in keys:
            lab = (g("METRICS").get(k) or (k,))[0]
            dt, tone = delta_txt(t.get(k, np.nan), c.get(k, np.nan) if c is not None else np.nan, "count")
            s.append(kpi_card(lab, fnum(t.get(k, np.nan), ",.0f"), dt, tone))
        s.append(f"</div><h3>{grp} · rates {E(ctx['basis_label'])}</h3><div class='kpis'>")
        for k in keys:
            lab = (g("METRICS").get(k) or (k,))[0]
            v = _rate(t.get(k, np.nan), t.get("TOTAL_HOURS", 0), basis)
            pv_ = _rate(c.get(k, np.nan), c.get("TOTAL_HOURS", 0), basis) if c is not None else np.nan
            dt, tone = delta_txt(v, pv_, "rate")
            s.append(kpi_card(lab + " rate", fnum(v, ".2f"), dt, tone))
        s.append("</div>")
    share = t.get("MOE_INCIDENTS", 0) / t.get("TOTAL_EVENTS", 0) if t.get("TOTAL_EVENTS", 0) else np.nan
    s.append("<div class='kpis'>" + kpi_card("MOE % of events", fnum(share * 100, ".1f", "%"))
             + kpi_card("Hours worked", g("fmt_hours")(t.get("TOTAL_HOURS", 0))) + "</div>")
    wk = cur.groupby("WK")[["RECORDABLE_INCIDENTS", "FIRST_AID_INCIDENT", "MSD_INJURIES", "MOE_INCIDENTS", "TOTAL_HOURS"]].sum().sort_index()
    if len(wk):
        x = [w.strftime("%m/%d") for w in wk.index]
        r_ = lambda k: list(wk[k] * basis / wk["TOTAL_HOURS"].where(wk["TOTAL_HOURS"] > 0))   # noqa: E731
        s.append("<div class='two'><div><h3>Injury rates by week</h3>" + svg_chart(x, [
            ("Recordable", r_("RECORDABLE_INCIDENTS"), P[0]), ("First aid", r_("FIRST_AID_INCIDENT"), P[1]),
            ("MSD", r_("MSD_INJURIES"), P[2])], yfmt=".1f", height=210) + "</div><div><h3>MOE rate by week</h3>" + svg_chart(
            x, [("MOE rate", r_("MOE_INCIDENTS"), P[0])], yfmt=".1f", height=210, legend=False,
            rules=[(_rate(t.get("MOE_INCIDENTS", 0), t.get("TOTAL_HOURS", 0), basis), "Rate for the period", "var(--axis)")]) + "</div></div>")
    if kind != "site":
        bys = cur.groupby("SITE", as_index=False)[["RECORDABLE_INCIDENTS", "FIRST_AID_INCIDENT", "MSD_INJURIES", "MOE_INCIDENTS",
                                                    "TOTAL_HOURS"]].sum()
        for k, nm in (("RECORDABLE_INCIDENTS", "TRIR"), ("FIRST_AID_INCIDENT", "FAR"), ("MSD_INJURIES", "MSDR"), ("MOE_INCIDENTS", "MOER")):
            bys[nm] = bys[k] * basis / bys["TOTAL_HOURS"].where(bys["TOTAL_HOURS"] > 0)
        bys = bys.sort_values("MSDR", ascending=False)
        net = _rate(t.get("MOE_INCIDENTS", 0), t.get("TOTAL_HOURS", 0), basis)
        b2 = bys.sort_values("MOER", ascending=False)
        s.append("<div class='two'><div><h3>MOE rate by site</h3>" + svg_hbars(list(b2["SITE"]), list(b2["MOER"]),
                 [P[0] if ctx["regions"].get(x) == "East" else P[1] if ctx["regions"].get(x) == "West" else "#9a9893" for x in b2["SITE"]],
                 ".2f", rule=net, rule_label="All sites") + "</div><div><h3>Injuries by site</h3>" + html_table(bys, [
            ("SITE", "Site", None), ("RECORDABLE_INCIDENTS", "Rec.", lambda v: fnum(v, ",.0f")), ("TRIR", "Rec. rate", lambda v: fnum(v, ".2f")),
            ("FIRST_AID_INCIDENT", "FA", lambda v: fnum(v, ",.0f")), ("MSD_INJURIES", "MSD", lambda v: fnum(v, ",.0f")),
            ("MSDR", "MSD rate", lambda v: fnum(v, ".2f")), ("TOTAL_HOURS", "Hours", lambda v: fnum(v, ",.0f"))]) + "</div></div>")
    ic = ctx["icare"]
    act, cas = ic.get("activity"), ic.get("cases")
    if act is not None or cas is not None:
        a_ = act[act["SITE"].isin(sites) & (act["DT"] >= rng[0]) & (act["DT"] <= rng[-1] + pd.Timedelta(days=6))] if act is not None else None
        c_ = cas[cas["SITE"].isin(sites)] if cas is not None else None
        s.append("<h3>iCare follow-up</h3><div class='kpis'>"
                 + kpi_card("Follow-up records", fnum(a_["CONTACTS"].sum() if a_ is not None else np.nan, ",.0f"))
                 + kpi_card("Pain not improving", fnum(a_["NOT_IMPROVING"].sum() if a_ is not None else np.nan, ",.0f"))
                 + kpi_card("Open cases · current", fnum(c_["OPEN_CASES"].sum() if c_ is not None else np.nan, ",.0f"))
                 + kpi_card("Open, no first iCare", fnum(c_["OPEN_NO_FOLLOWUP"].sum() if c_ is not None else np.nan, ",.0f"))
                 + "</div><p class='cap'>Follow-ups dated in the period; open cases = the current source snapshot across all injury "
                   "dates. iCare never changes an OVSR status.</p>")
    return "".join(s)


def tab_trends(ctx, sites, label, kind, ol, pw):
    sw, basis = ctx["sw"], ctx["basis"]
    d = sw[sw["SITE"].isin(sites) & (sw["DAYS"] == 7)].groupby("WK")[[
        "RECORDABLE_INCIDENTS", "FIRST_AID_INCIDENT", "MSD_INJURIES", "MOE_INCIDENTS", "TOTAL_HOURS"]].sum().sort_index()
    d = d[d.index >= pd.Timestamp("2025-06-01")]
    if d.empty:
        return "<p class='cap'>No weekly data.</p>"
    roll = d.rolling(4, min_periods=1).sum()
    x = [w.strftime("%m/%d/%y") for w in d.index]
    figs = []
    for k, lab, col in (("RECORDABLE_INCIDENTS", "Recordable rate", P[0]), ("FIRST_AID_INCIDENT", "First-aid rate", P[1]),
                        ("MSD_INJURIES", "MSD rate", P[2]), ("MOE_INCIDENTS", "MOE rate", P[6])):
        wkly = list(d[k] * basis / d["TOTAL_HOURS"].where(d["TOTAL_HOURS"] > 0))
        r4 = list(roll[k] * basis / roll["TOTAL_HOURS"].where(roll["TOTAL_HOURS"] > 0))
        figs.append(f"<div><h3>{E(lab)}</h3>" + svg_chart(x, [("Weekly", wkly, "#c9d6ea"), ("4-week rolling", r4, col)],
                                                          yfmt=".1f", height=200, x_every=max(1, len(x) // 6)) + "</div>")
    s = [f"<h2>Weekly rates since Jun 2025 · {E(ctx['basis_label'])}</h2><div class='grid2'>" + "".join(figs) + "</div>"]
    m = d.copy()
    m["M"] = m.index.to_period("M")
    mt = m.groupby("M").sum(numeric_only=True)
    mt["TRIR"] = mt["RECORDABLE_INCIDENTS"] * basis / mt["TOTAL_HOURS"]
    mt["MSDR"] = mt["MSD_INJURIES"] * basis / mt["TOTAL_HOURS"]
    mt["MOER"] = mt["MOE_INCIDENTS"] * basis / mt["TOTAL_HOURS"]
    mt = mt.reset_index().iloc[::-1]
    mt["MONTH"] = mt["M"].astype(str)
    f2 = lambda v: fnum(v, ".2f")   # noqa: E731
    s.append("<h3>By month (weeks assigned to the month they start in)</h3>" + html_table(mt, [
        ("MONTH", "Month", None), ("TOTAL_HOURS", "Hours", lambda v: fnum(v, ",.0f")),
        ("RECORDABLE_INCIDENTS", "Recordables", lambda v: fnum(v, ",.0f")), ("TRIR", "Recordable rate", f2),
        ("MSD_INJURIES", "MSD", lambda v: fnum(v, ",.0f")), ("MSDR", "MSD rate", f2),
        ("MOE_INCIDENTS", "MOE", lambda v: fnum(v, ",.0f")), ("MOER", "MOE rate", f2)]))
    return "".join(s)


def tab_rules(ctx):
    """Use the same current guide as the live dashboard, with snapshot-specific data notes."""
    return ovsr_guide.render_guide(ctx["th"], g("OVSR_TH"), DRIVERS, g("STATUS_COLOR"),
                                  basis=ctx["basis"], snapshot=True)


# ---------------------------------------------------------------------------------------------------------------
# Snapshot: assemble every scope, compress, wrap in the page shell; the Streamlit button that builds it
# ---------------------------------------------------------------------------------------------------------------
SNAP_TABS = [("overview", "Overview"), ("note", "Weekly note"), ("outlook", "Ops outlook"), ("xsafety", "Ops × Safety"),
             ("site", "Site"), ("safety", "Safety KPIs"), ("rules", "How OVSR Works")]
_TAB_FN = {"overview": "tab_overview", "note": "tab_note", "outlook": "tab_outlook", "xsafety": "tab_xsafety", "site": "tab_site",
           "safety": "tab_safety_all"}
_SAFETY_PARTS = (("kpis", "Rates & counts", "tab_safety"), ("trends", "Trends", "tab_trends"), ("yday", "Yesterday", "tab_yesterday"))


def tab_safety_all(ctx, sites, label, kind, ol, pw):
    """Safety KPIs with Trends and Yesterday as a switch inside the tab (fewer top-level tabs)."""
    on = " class='on'"
    bar = "<div class='seg' role='tablist'>" + "".join(
        f"<button{on if i == 0 else ''} data-grpbtn='saf:{k}'>{E(n)}</button>" for i, (k, n, _) in enumerate(_SAFETY_PARTS)) + "</div>"
    panes = []
    for i, (k, n, fn) in enumerate(_SAFETY_PARTS):
        try:
            body = globals()[fn](ctx, sites, label, kind, ol, pw)
        except Exception as exc:   # one part never sinks the tab
            body = f"<p class='cap'>{E(n)} couldn't be built: {E(type(exc).__name__)}.</p>"
        panes.append(f"<div data-pgrp='saf' data-pval='{k}'{'' if i == 0 else ' hidden'}>{body}</div>")
    return bar + "".join(panes)


def build_snapshot(progress=None):
    ctx = gather_context()
    pw = ctx["panel"]
    scopes = scopes_for(ctx)
    data, errors = {}, []
    for i, (sid, label, sites, kind) in enumerate(scopes):
        if progress:
            progress(i / len(scopes), f"Building {label}…")
        ol = outlook_for(ctx, sites)
        tabs = {}
        for tid, _ in SNAP_TABS:
            if tid == "rules":
                continue
            try:
                tabs[tid] = globals()[_TAB_FN[tid]](ctx, sites, label, kind, ol, pw)
            except Exception as exc:   # one bad section never sinks the file
                errors.append(f"{label} · {tid}: {exc!r}"[:300])
                tabs[tid] = f"<p class='cap'>This section couldn't be built: {E(type(exc).__name__)}.</p>"
        raw = json.dumps(tabs, separators=(",", ":")).encode("utf-8")
        data[sid] = base64.b64encode(gzip.compress(raw, 9)).decode("ascii")
    gen = pd.Timestamp.now(tz="America/New_York") if True else None
    meta = {"generated": f"{gen:%b %d, %Y %I:%M %p} ET", "through": f"{pd.Timestamp(g('anchor') if g('anchor') is not None else ctx['last_day']):%b %d, %Y}",
            "scopeText": (", ".join(g("sel_regions")) if g("sel_regions") else "All regions")
                         + (" · " + ", ".join(g("sel_sites")) if g("sel_sites") else "")
                         + (" · " + ", ".join(g("sel_gens")) if g("sel_gens") else "") + f" · {len(ctx['pool'])} sites",
            "range": f"{ctx['rng'][0]:%b %d} – {ctx['rng'][-1] + pd.Timedelta(days=6):%b %d, %Y}",
            "basis": ctx["basis_label"], "errors": len(errors)}
    meta.update(gov.metadata(_namespace()))
    meta["range"] += " · release " + gov.RELEASE_ID + " · config " + gov.config_id(g("ovsr_th_current")())
    shell = (SNAP_SHELL.replace("/*__SCOPES__*/[]", json.dumps([{"id": s, "label": l, "kind": k} for s, l, _, k in scopes]))
             .replace("/*__DATA__*/{}", json.dumps(data))
             .replace("/*__META__*/{}", json.dumps(meta))
             .replace("<!--__TABS__-->", "".join(f"<button role='tab' data-tab='{t}'>{E(n)}</button>" for t, n in SNAP_TABS))
             .replace("<!--__RULES__-->", tab_rules(ctx))
             .replace("__TITLE__", f"OVSR site snapshot · {E(meta['through'])}"))
    if progress:
        progress(1.0, "Done")
    return shell.encode("utf-8"), meta, errors


def _snap_key():
    return (g("grain"), str(g("start")), str(g("end")), g("compare"), tuple(g("sel_regions") or ()), tuple(g("sel_sites") or ()),
            g("basis"), tuple(sorted(g("ovsr_th_current")().items())))


SNAP_FILE = "OVSR_site_snapshot.html"   # same name as the file the SharePoint OVSR page embeds, so a new copy replaces it


def _auto_download(data, name):
    """Start the browser download as soon as the snapshot is built (no second click)."""
    import streamlit.components.v1 as components
    b64 = base64.b64encode(data).decode("ascii")
    components.html(
        "<script>(function(){try{"
        f"const s=atob('{b64}');const u=new Uint8Array(s.length);for(let i=0;i<s.length;i++)u[i]=s.charCodeAt(i);"
        "const url=URL.createObjectURL(new Blob([u],{type:'text/html'}));"
        f"const a=document.createElement('a');a.href=url;a.download={json.dumps(name)};"
        "document.body.appendChild(a);a.click();setTimeout(function(){URL.revokeObjectURL(url);},60000);"
        "}catch(e){console.log('OVSR auto-download failed',e);}})();</script>", height=0)


def snapshot_ui(slot):
    """Build button at the top of the OVSR overview: builds the snapshot and downloads it straight away."""
    with slot:
        c1, c2 = st.columns([1.3, 3])
        key = _snap_key()
        have = st.session_state.get("ovsr_snap")
        if c1.button("Build site snapshot (HTML)", key="snap_build", type="primary",
                     help="Builds one HTML file for sites without Snowflake and downloads it as " + SNAP_FILE + ": a site picker "
                          "(all sites, regions, each site) and tabs for Overview, Weekly note, Ops outlook, Ops × Safety, Site, "
                          "Safety KPIs (with Trends and Yesterday) and How OVSR works. Uses the sidebar filters. A copy of today's "
                          "numbers, not live. Internal use."):
            with st.spinner("Loading workforce signals first (the first load of the day can take a minute or two)…"):
                g("load_workforce")()
                st.session_state["wf_load"] = True
            bar = c2.progress(0.0, text="Building…")
            data, meta, errors = build_snapshot(lambda f, t: bar.progress(min(max(f, 0.0), 1.0), text=t))
            bar.empty()
            st.session_state["ovsr_snap"] = have = {"key": key, "data": data, "meta": meta, "errors": errors, "name": SNAP_FILE}
            st.session_state["ovsr_snap_autodl"] = True
        if have and have.get("key") == key:
            if st.session_state.pop("ovsr_snap_autodl", False):
                _auto_download(have["data"], have["name"])
            c1.download_button("Download again", have["data"], file_name=have["name"], mime="text/html", key="snap_dl",
                               help="The build downloads the file automatically; use this if your browser blocked it.")
            m = have["meta"]
            c2.caption(f"Downloaded {have['name']}: {m['scopeText']} · data through {m['through']} · built {m['generated']} · "
                       f"{len(have['data']) / 1e6:.1f} MB." + (f" {m['errors']} section(s) couldn't be built." if m["errors"] else "")
                       + " Snapshot, not live; internal use.")
        else:
            c2.caption("Builds one HTML file for sites and downloads it as " + SNAP_FILE + ": pick all sites, a region or a "
                       "site inside the file. Workforce signals load first. Uses the sidebar filters, so rebuild after changing them.")


SNAP_SHELL = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#ffffff;--surface:#f6f7fa;--ink:#1f2328;--mute:#5c6066;--line:#dfe3ea;--grid:#eceff4;--axis:#8a8f98;
 --brand:#1641c7;--brand2:#2f62f0;--bad:#c62f2e;--good:#137a55;--chip:#eef2ff}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#15171b;--surface:#1c2027;--ink:#e8eaee;--mute:#a4a9b2;
 --line:#2c313a;--grid:#262b33;--axis:#7d838d;--brand:#7d9bff;--brand2:#9db4ff;--bad:#ff7b7a;--good:#4fd1a1;--chip:#232a3b}}
:root[data-theme=dark]{--bg:#15171b;--surface:#1c2027;--ink:#e8eaee;--mute:#a4a9b2;--line:#2c313a;--grid:#262b33;--axis:#7d838d;
 --brand:#7d9bff;--brand2:#9db4ff;--bad:#ff7b7a;--good:#4fd1a1;--chip:#232a3b}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--ink)}
body{font:14.5px/1.5 -apple-system,"Segoe UI",Calibri,Roboto,Arial,sans-serif}
header{position:sticky;top:0;z-index:20;background:var(--bg);border-bottom:1px solid var(--line)}
.bar{max-width:1240px;margin:0 auto;padding:12px 16px 0;display:flex;flex-wrap:wrap;align-items:center;gap:10px 18px}
.brand{display:flex;align-items:center;gap:12px;min-width:0}
.logo{background:linear-gradient(135deg,#1641c7,#2f62f0);color:#fff;font-weight:900;letter-spacing:.06em;border-radius:8px;
 padding:4px 10px;font-size:18px}
.brand h1{font-size:17px;margin:0;line-height:1.2}.brand p{margin:0;color:var(--mute);font-size:12.5px}
.pick{margin-left:auto;display:flex;align-items:center;gap:8px}
.pick label{font-size:12.5px;color:var(--mute)}
select{font:inherit;color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:6px 10px;min-width:220px}
nav{max-width:1240px;margin:0 auto;padding:8px 16px 0;display:flex;gap:2px;overflow-x:auto;scrollbar-width:thin}
nav button{font:inherit;font-size:13.5px;background:none;border:0;border-bottom:3px solid transparent;color:var(--mute);
 padding:8px 12px;cursor:pointer;white-space:nowrap}
nav button[aria-selected=true]{color:var(--ink);border-bottom-color:var(--brand);font-weight:600}
main{max-width:1240px;margin:0 auto;padding:10px 16px 60px}
.tag{display:inline-block;padding:2px 9px;border-radius:999px;background:var(--chip);color:var(--brand);font-size:12px;font-weight:600}
h2{font-size:19px;margin:28px 0 6px}h3{font-size:14.5px;margin:16px 0 6px}
.cap{color:var(--mute);font-size:12.5px;margin:4px 0 10px;max-width:100ch}
.two{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px 28px}
.grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px 28px}
.grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px 22px}
@media (max-width:820px){.two,.grid2,.grid3{grid-template-columns:minmax(0,1fr)}.pick{margin-left:0;width:100%}select{flex:1;min-width:0}}
.tiles,.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(9rem,1fr));gap:10px}
.tile{border:1px solid var(--line);border-left:5px solid var(--c);border-radius:10px;padding:9px 12px;background:var(--surface)}
.tile b{display:block;font-size:26px;line-height:1.1}.tile span{font-size:12.5px;color:var(--mute)}.tile.zero b{color:var(--mute);font-weight:500}
.tile i,.st i,.lg i{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--c);margin-right:6px}
.kpi{border:1px solid var(--line);border-radius:10px;padding:9px 12px;background:var(--bg)}.kpi small{color:var(--mute);font-size:12.5px;display:block}
.kpi b{display:block;font-size:22px;line-height:1.25;font-variant-numeric:tabular-nums}
.d{font-size:12px;font-weight:600;color:var(--mute)}.d.bad,b.bad{color:var(--bad)}.d.good,b.good{color:var(--good)}
.wkcards{display:grid;grid-template-columns:repeat(auto-fit,minmax(13rem,1fr));gap:10px}
.wkcard{border:1px solid var(--line);border-top:4px solid var(--axis);border-radius:10px;padding:9px 12px;background:var(--surface)}
.wkcard.ramp{border-top-color:#eb6834}.wkcard.watch{border-top-color:#eda100}.wkcard.act{border-top-color:#e34948}
.wkcard small{color:var(--mute);font-size:12px}.wkcard b{display:block;font-size:21px;font-variant-numeric:tabular-nums}
.wkcard span{display:block;font-size:12.5px;color:var(--mute)}
.call{border:1px solid var(--line);border-radius:12px;padding:10px 16px;background:var(--surface)}
.call ol,.call ul{margin:6px 0;padding-left:20px}.call li{margin:7px 0}.call p{margin:2px 0;color:var(--mute)}
.ev{font-size:12.5px;color:var(--mute)}.acts li{margin:10px 0}.acts p{margin:2px 0}
.st{white-space:nowrap;font-weight:600}.nw{white-space:nowrap}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:10px;margin:4px 0 8px}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th,td{padding:6px 9px;border-bottom:1px solid var(--line);text-align:right;vertical-align:top;font-variant-numeric:tabular-nums}
th{background:var(--surface);font-weight:600;white-space:nowrap}td.t,th.t{text-align:left}tr:last-child td{border-bottom:0}
.sc td:first-child,.sc th:first-child{position:sticky;left:0;background:var(--bg)}.sc th:first-child{background:var(--surface)}
table.grid{width:auto}.gw{display:inline-block;max-width:100%;padding:6px 8px}
.grid td,.grid th{padding:2px;border:0;text-align:center}.grid th{font-weight:500;color:var(--mute);background:none}
.grid td.s{text-align:right;padding-right:8px;white-space:nowrap}
.cell{display:block;width:34px;height:20px;border-radius:3px;background:var(--c)}.cell.big{height:26px;width:40px;margin:0 auto}
.strip td small{display:block;color:var(--mute);font-size:11px}
.lg{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:12.5px;color:var(--mute);margin:8px 0}
.hmt td.hm{text-align:center;font-weight:600;min-width:84px;white-space:nowrap}.hmt th{white-space:normal;min-width:84px;vertical-align:bottom}.hmt td.t{min-width:150px}.hmt td.t,.hmt th.t{position:sticky;left:0;z-index:1;background:var(--bg)}.hmt thead th.t{background:var(--surface)}.hm .sig{font-size:9px;vertical-align:2px;margin-left:2px}.pcap{margin:6px 0 0;font-size:12.5px;color:var(--mute)}.sc td:nth-child(3){min-width:210px}.sc td:nth-child(2){white-space:nowrap}.hmt tr.grp td{background:var(--surface);font-weight:600;text-align:left;color:var(--mute)}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:999px;padding:2px;margin:2px 0 8px}
.seg button{font:inherit;font-size:12.5px;border:0;background:none;color:var(--mute);padding:4px 12px;border-radius:999px;cursor:pointer}
.seg button.on{background:var(--brand);color:#fff}
.card.status{display:grid;grid-template-columns:minmax(10rem,1fr) 3fr;gap:16px;border:1px solid var(--line);border-radius:12px;padding:12px 16px;background:var(--surface)}
@media (max-width:820px){.card.status{grid-template-columns:1fr}}
.sitegrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(14rem,1fr));gap:8px}
.sitebtn{font:inherit;text-align:left;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--ink);padding:9px 12px;cursor:pointer}
.sitebtn:hover{border-color:var(--brand)}.sitebtn small{display:block;color:var(--mute);font-size:12px;margin-top:2px}
svg.chart{width:100%;height:auto;display:block;overflow:visible}svg.chart text{font-family:inherit}
svg.chart .tick{font-size:11px;fill:var(--mute)}svg.chart .lg{font-size:11.5px;fill:var(--ink)}
svg.chart line.grid{stroke:var(--grid)}svg.chart line.zero{stroke:var(--axis)}
.rules td{white-space:normal}
.note{border:1px solid var(--line);border-radius:12px;padding:6px 18px 12px;background:var(--surface)}
.note h3{margin:16px 0 4px}.note ul{margin:4px 0;padding-left:20px}.note li{margin:4px 0}
.nsite{border-left:4px solid var(--c);padding:2px 0 2px 12px;margin:10px 0}.nsite .nh{margin:0 0 2px}.nsite ul{margin:2px 0}
.notetxt{white-space:pre-wrap;word-break:break-word;font:12.5px/1.5 ui-monospace,Consolas,Menlo,monospace;background:var(--surface);
 border:1px solid var(--line);border-radius:10px;padding:12px 14px;max-height:520px;overflow:auto}
.copybtn{font:inherit;font-size:12.5px;border:1px solid var(--line);background:var(--bg);color:var(--ink);border-radius:999px;padding:2px 12px;cursor:pointer}
footer{max-width:1240px;margin:0 auto;padding:16px;color:var(--mute);font-size:12px;border-top:1px solid var(--line)}
#loading{padding:40px 0;color:var(--mute)}
@media print{header{position:static}nav,.pick,.seg{display:none}.scroll{overflow:visible}h2{break-after:avoid}.kpis,.tiles,.call,.two{break-inside:avoid}}
</style></head>
<body>
<header>
 <div class="bar">
  <div class="brand"><span class="logo">OVSR</span><div><h1>Operational Variance &amp; Safety Response · site snapshot</h1>
   <p id="meta"></p></div></div>
  <div class="pick"><label for="scope">Showing</label><select id="scope" aria-label="Site or region"></select></div>
 </div>
 <nav role="tablist"><!--__TABS__--></nav>
</header>
<main>
 <p class="cap"><span class="tag">Snapshot, not live</span> <span id="meta2"></span></p>
 <div id="view"><p id="loading">Loading…</p></div>
 <div id="rules" hidden><!--__RULES__--></div>
</main>
<footer>EHS &amp; Asset Protection · built from the OVSR Dashboard in Snowflake. Copy of the numbers at build time; it does not update.
No names, usernames, employee IDs or narratives are included. Internal use only.</footer>
<script>
const SCOPES = /*__SCOPES__*/[];
const DATA = /*__DATA__*/{};
const META = /*__META__*/{};
const cache = {};
const sel = document.getElementById('scope'), view = document.getElementById('view'), rules = document.getElementById('rules');
const tabs = [...document.querySelectorAll('nav button')];
let state = {scope: SCOPES.length ? SCOPES[0].id : null, tab: 'overview'};
document.getElementById('meta').textContent = 'Data through ' + META.through + ' · built ' + META.generated;
document.getElementById('meta2').textContent = META.scopeText + ' · selected period ' + META.range + ' · rates ' + META.basis + '.';
const groups = {net: 'All', region: 'Regions', site: 'Sites'};
for (const k of ['net', 'region', 'site']) {
  const items = SCOPES.filter(s => s.kind === k); if (!items.length) continue;
  const og = document.createElement('optgroup'); og.label = groups[k];
  for (const s of items) { const o = document.createElement('option'); o.value = s.id; o.textContent = s.label; og.appendChild(o); }
  sel.appendChild(og);
}
async function load(id) {
  if (cache[id]) return cache[id];
  if (!('DecompressionStream' in window)) throw new Error('Open this file in a current Chrome or Edge.');
  const bin = Uint8Array.from(atob(DATA[id]), c => c.charCodeAt(0));
  const txt = await new Response(new Blob([bin]).stream().pipeThrough(new DecompressionStream('gzip'))).text();
  return (cache[id] = JSON.parse(txt));
}
async function show(scope, tab, push) {
  state = {scope: scope || state.scope, tab: tab || state.tab};
  sel.value = state.scope;
  tabs.forEach(b => b.setAttribute('aria-selected', String(b.dataset.tab === state.tab)));
  if (push !== false) { try { history.replaceState(null, '', '#' + encodeURIComponent(state.scope) + '/' + state.tab); } catch (e) { /* sandboxed embed (SharePoint/OneDrive viewer) blocks URL updates; ignore */ } }
  if (state.tab === 'rules') { view.hidden = true; rules.hidden = false; return; }
  rules.hidden = true; view.hidden = false;
  try {
    const d = await load(state.scope);
    view.innerHTML = d[state.tab] || '<p class="cap">Nothing to show for this view.</p>';
  } catch (e) { view.innerHTML = '<p class="cap">' + e.message + '</p>'; }
}
sel.addEventListener('change', () => show(sel.value, null));
tabs.forEach(b => b.addEventListener('click', () => { show(null, b.dataset.tab); window.scrollTo({top: 0}); }));
view.addEventListener('click', ev => {
  const go = ev.target.closest('[data-goto]');
  if (go) { show(go.dataset.goto, 'site'); window.scrollTo({top: 0}); return; }
  const gb = ev.target.closest('[data-grpbtn]');
  if (gb) {
    const [grp, val] = gb.dataset.grpbtn.split(':');
    gb.parentElement.querySelectorAll('button').forEach(b => b.classList.toggle('on', b === gb));
    view.querySelectorAll('[data-pgrp="' + grp + '"]').forEach(p => p.hidden = p.dataset.pval !== val);
    return;
  }
  const cb = ev.target.closest('[data-copy]');
  if (cb) {
    const pre = document.getElementById(cb.dataset.copy); if (!pre) return;
    const done = () => { cb.textContent = 'Copied'; setTimeout(() => cb.textContent = 'Copy', 1500); };
    const fallback = () => { const r = document.createRange(); r.selectNodeContents(pre); const s = getSelection();
      s.removeAllRanges(); s.addRange(r); try { document.execCommand('copy'); done(); } catch (e) {} };
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(pre.textContent).then(done, fallback);
    else fallback();
    return;
  }
  const lb = ev.target.closest('[data-lagbtn]');
  if (lb) {
    const lag = lb.dataset.lagbtn;
    lb.parentElement.querySelectorAll('button').forEach(b => b.classList.toggle('on', b === lb));
    view.querySelectorAll('.lagpane').forEach(p => p.hidden = p.dataset.lag !== lag);
  }
});
const h = decodeURIComponent(location.hash.slice(1)).split('/');
show(SCOPES.some(s => s.id === h[0]) ? h[0] : null, tabs.some(b => b.dataset.tab === h[1]) ? h[1] : 'overview', false);
</script>
</body></html>
"""


# ---------------------------------------------------------------------------------------------------------------
# Weekly note: one note per scope and week, rebuilt from the live data on every run (app tab and snapshot tab).
# Aggregates only: site codes, counts, rates and plan figures. No names, IDs or narratives.
# ---------------------------------------------------------------------------------------------------------------
XS_TABS = ["Drivers & evidence", "Injury profile", "Departments & paths", "Items & promotions", "Labor & rotation"]   # Ops x Safety sub-tabs (the app adds Event detail)
NOTE_FLAGGED = ("Elevated", "Moderate Elevation", "Watch")
NOTE_CALL = ("Elevated", "Moderate Elevation")
NOTE_OUTCOMES = ("TRIR", "MSDR", "MOER", "LIFTR", "REACHR")  # ledger outcomes the note draws on
NOTE_MAX_CALL, NOTE_MAX_WATCH = 10, 12
NOTE_MIN_RATE_HOURS = 100_000   # below this, the headline shows counts only (the site block has 4-week rates)


def render_xs(tabs):
    """Fill the Ops x Safety sub-tabs the app created ({name: container}); the panel is built once for all of them."""
    x = _xs_setup()
    parts = {"Drivers & evidence": _xs_drivers, "Injury profile": _xs_profile, "Departments & paths": _xs_paths,
             "Items & promotions": _xs_items,
             "Labor & rotation": _xs_labor}
    for name, fn in parts.items():
        if name not in tabs:
            continue
        with tabs[name]:
            try:
                fn(x)
            except Exception as exc:  # one sub-tab never breaks the others
                st.error(f"{name} could not render: {exc}")
                import traceback as _tb; print(f"[OVSR] {name} failed:\n" + _tb.format_exc(), flush=True)


def _wk_range(w):
    w = pd.Timestamp(w)
    e = w + pd.Timedelta(days=6)
    return (f"{w:%b} {w.day} – {e.day}, {e:%Y}" if w.month == e.month else f"{w:%b} {w.day} – {e:%b} {e.day}, {e:%Y}")


def _d(w):
    w = pd.Timestamp(w)
    return f"{w:%b} {w.day}"


def _n(v, f=",.0f"):
    return "—" if v is None or not np.isfinite(v) else format(v, f)


def _hours_txt(h):
    if not h or not np.isfinite(h):
        return "0 hours"
    return f"{h / 1e6:.2f}M hours" if h >= 1e6 else f"{h / 1e3:,.0f}K hours"


def _list_words(xs):
    xs = [x for x in xs if x]
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1] if xs else ""


def _first_sentence(t):
    t = str(t or "").strip()
    i = t.find(". ")
    return t if i < 0 else t[:i + 1]


def _cap1(t):
    t = str(t or "").strip()
    return t[:1].upper() + t[1:] if t else t


def _pct0(v):
    r = int(round(float(v)))
    return f"{r:+d}%" if r else "0%"


def _rate(n, h, basis):
    return n * basis / h if h and h > 0 else np.nan


@gov.viewer_cache(st, lambda: g("_viewer_context")(), ttl=3600, show_spinner=False)
def note_frames(basis, th_items, pool, wk, cur_wk, last_day, _rev=3):
    """Everything the app's note needs for one week (cached; the loaders and the status scorer are cached too)."""
    th = dict(th_items)
    pool = list(pool)
    wk = pd.Timestamp(wk)
    dfact = _daily_fact()
    hol, _, sop, _ = g("load_plan")()
    ow = g("ops_weeks")(hol)
    sw = g("site_weeks")(dfact)
    sw["INJ_ALL"] = sw["RECORDABLE_INCIDENTS"] + sw["FIRST_AID_INCIDENT"]
    fr = g("ovsr_frame")(sw, ow, sop, wk, th, pool)
    weeks = tuple(wk - pd.Timedelta(weeks=k) for k in range(8, -1, -1))
    hist = g("ovsr_history")(g("DAILY"), weeks, th_items, tuple(pool), cur_wk, last_day)
    try:
        ol = outlook_parts(dfact, sw, ow, hol, sop, pool, cur_wk)
    except Exception:
        ol = {"plan": pd.DataFrame(), "proj": pd.DataFrame(), "pbase": pd.DataFrame(), "watch": np.nan, "act": np.nan}
    met, _ = load_x("x_met")
    return {"sw": sw[sw["SITE"].isin(pool)].reset_index(drop=True), "ow": ow[ow["SITE"].isin(pool)].reset_index(drop=True),
            "fr": fr, "hist": hist, "plan": ol["plan"], "proj": ol["proj"], "pbase": ol["pbase"], "watch": ol["watch"],
            "act": ol["act"], "met": met, "eps": ol.get("eps", 0.6),
            "publish": sop["PUBLISH_WEEK"].max() if sop is not None and len(sop) and "PUBLISH_WEEK" in sop else pd.NaT}


# ---- the note itself: a plain structure that three renderers share (app markdown, copy text, snapshot HTML) ----
def _streak(hist, site, wk):
    h = hist[hist["SITE"] == site].drop_duplicates("WK").set_index("WK")["STATUS"]
    n, w = 0, pd.Timestamp(wk)
    while w in h.index and h[w] in NOTE_FLAGGED:
        n += 1
        w -= pd.Timedelta(weeks=1)
    return n


def _prev_status(hist, site, wk):
    h = hist[(hist["SITE"] == site) & (hist["WK"] == pd.Timestamp(wk) - pd.Timedelta(weeks=1))]
    return h["STATUS"].iloc[0] if len(h) else None


def _tail_txt(streak, prev, status):
    bits = []
    if status in NOTE_FLAGGED:
        if streak >= 2:
            bits.append(f"{_ord(streak)} week flagged in a row")
        elif prev is not None and prev not in NOTE_FLAGGED:
            bits.append("new this week")
    if prev is not None and prev != status:
        bits.append(f"week before: {prev}")
    elif prev is not None and status in NOTE_FLAGGED and streak < 2:
        bits.append(f"week before: {prev}")
    return " · ".join(bits)


def _site_injuries(sw, site, wk, basis):
    """Counts that week, and the last 4 full weeks' recordable and MSD rates vs the site's prior 13."""
    s = sw[(sw["SITE"] == site)]
    r = s[s["WK"] == pd.Timestamp(wk)]
    if r.empty:
        return None
    r = r.iloc[0]
    parts = [f"{int(r['RECORDABLE_INCIDENTS'])} recordable", f"{int(r['MSD_INJURIES'])} MSD",
             f"{int(r['MOE_INCIDENTS'])} MOE", f"{int(r['FIRST_AID_INCIDENT'])} first aid"]
    txt = " · ".join(parts)
    full = s[s["DAYS"] == 7]
    # The 4 latest full weeks on or before the note's week (a week in progress isn't full), then the 13 before.
    upto = full.loc[full["WK"] <= pd.Timestamp(wk), "WK"]
    e = upto.max() if len(upto) else pd.Timestamp(wk)
    l4 = full[(full["WK"] <= e) & (full["WK"] > e - pd.Timedelta(weeks=4))]
    b13 = full[(full["WK"] <= e - pd.Timedelta(weeks=4)) & (full["WK"] > e - pd.Timedelta(weeks=17))]
    if len(l4) >= 3 and len(b13) >= 8:
        bits = []
        for c, lab in (("RECORDABLE_INCIDENTS", "recordable"), ("MSD_INJURIES", "MSD")):
            a = _rate(l4[c].sum(), l4["TOTAL_HOURS"].sum(), basis)
            b = _rate(b13[c].sum(), b13["TOTAL_HOURS"].sum(), basis)
            if np.isfinite(a) and np.isfinite(b):
                bits.append(f"{lab} {a:.2f} vs {b:.2f}")
        if bits:
            txt += " · last 4 weeks' rates: " + ", ".join(bits) + " the 13 weeks before"
    return txt


def _watch_here(nc, site, wk):
    """Drivers that hold up (or are suggestive) network-wide and are now running high at this site."""
    p_all, led = nc.get("panel"), nc.get("ledger")
    if p_all is None or led is None or led.empty:
        return ""
    ps = p_all[p_all["SITE"] == site]
    if ps.empty:
        return ""
    try:
        tbl = site_driver_table(ps, led, wk, k=6, outcomes=NOTE_OUTCOMES)
    except Exception:
        return ""
    if tbl.empty:
        return ""
    bad = tbl[tbl["TONE"] == "bad"].drop_duplicates("DRIVER").head(2)
    if bad.empty:
        return ""
    bits = []
    for r in bad.to_dict("records"):
        way = "up" if np.isfinite(r["Z"]) and r["Z"] > 0 else "down"
        bits.append(f"{r['LABEL']} {way} at {r['NOW_TXT']} vs {r['NORMAL_TXT']} normal, the direction that goes with a higher "
                    f"{_lc(r['OUT_LABEL'])} rate network-wide")
    return "; ".join(bits)


def _asks(fired):
    """Up to two first moves from different responses, safety first (fired is already in that order)."""
    R = g("RESPONSES")
    out, seen = [], set()
    for k in fired:
        owner, move = R[k][1], R[k][2]
        if move in seen:
            continue
        seen.add(move)
        out.append((owner, move))
        if len(out) == 2:
            break
    return out


def _site_block(nc, r, i, wk, prev, streak):
    th, R = nc["th"], g("RESPONSES")
    fired = list(r.get("FIRED") or [])
    rows = []
    if r.get("STATUS_REASON"):
        rows.append(("Why", _cap1(r["STATUS_REASON"]) + "."))
    sig = []
    for k in fired:
        try:
            ev = g("_ev_text")(k, r, th)
        except Exception:
            ev = ""
        ev = ev.replace("; escalation also checks persistence and safety", "")
        sig.append(f"{R[k][0]}: {ev}" if ev else R[k][0])
    if sig:
        rows.append(("Signals", sig))
    inj = _site_injuries(nc["sw"], r["SITE"], wk, nc["basis"])
    if inj:
        how = mech_site_text(nc.get("inj"), r["SITE"], wk)
        rows.append(("Injuries that week", inj + (f" · how: {how}" if how else "")))
    wh = _watch_here(nc, r["SITE"], wk)
    if wh:
        rows.append(("Watch here", wh + "."))
    asks = _asks(fired)
    if asks:
        rows.append(("Ask", asks))
    return {"n": i, "site": r["SITE"], "region": nc["regions"].get(r["SITE"], "Other"), "status": r["STATUS"],
            "tail": _tail_txt(streak, prev, r["STATUS"]), "rows": rows}


def _ops_line(ow, sites, wk):
    o, op = _ops_sum(ow, sites, [pd.Timestamp(wk)]), _ops_sum(ow, sites, [pd.Timestamp(wk) - pd.Timedelta(weeks=1)])
    if not o or not np.isfinite(o.get("PRESS", np.nan)):
        return ""
    t = (f"Workload: outbound volume {o['VOL']:+.1f}% vs plan with hours {o['HRS']:+.1f}%, so pressure ran "
         f"{o['PRESS']:+.1f} pts")
    if op and np.isfinite(op.get("PRESS", np.nan)):
        t += f" (week before {op['PRESS']:+.1f})"
    extra = []
    if np.isfinite(o.get("TPH", np.nan)):
        extra.append(f"pace {o['TPH']:+.1f}% vs planned TPH")
    if np.isfinite(o.get("IB", np.nan)):
        extra.append(f"inbound {o['IB']:+.1f}% vs plan")
    return t + (". " + _cap1(", ".join(extra)) if extra else "") + "."


def _injury_line(sw, sites, wk, basis):
    wk = pd.Timestamp(wk)
    cur = sw[sw["SITE"].isin(sites) & (sw["WK"] == wk)]
    base = sw[sw["SITE"].isin(sites) & sw["WK"].isin([wk - pd.Timedelta(weeks=k) for k in range(1, 5)]) & (sw["DAYS"] == 7)]
    if cur.empty:
        return "", ""
    c = cur.sum(numeric_only=True)
    b = base.sum(numeric_only=True)
    n_b = base["WK"].nunique()
    counts = (f"{int(c['RECORDABLE_INCIDENTS'])} recordable, {int(c['MSD_INJURIES'])} MSD and {int(c['MOE_INCIDENTS'])} MOE "
              f"incidents (plus {int(c['FIRST_AID_INCIDENT'])} first aid) over {_hours_txt(c['TOTAL_HOURS'])}.")
    bits = []
    for col, lab in (("RECORDABLE_INCIDENTS", "Recordable"), ("MSD_INJURIES", "MSD"), ("MOE_INCIDENTS", "MOE")):
        a = _rate(c[col], c["TOTAL_HOURS"], basis)
        p = _rate(b[col], b["TOTAL_HOURS"], basis) if n_b else np.nan
        if not np.isfinite(a):
            continue
        if np.isfinite(p) and p > 0:
            bits.append(f"{lab} {a:.2f} vs {p:.2f} ({(a / p - 1) * 100:+.0f}%)")
        else:
            bits.append(f"{lab} {a:.2f}")
    rates = ((f"Rates vs the {n_b} weeks before: " if n_b else "Rates: ") + " · ".join(bits) + ".") if bits else ""
    return counts, rates


def _moving_lines(nc, sites, wk):
    led, p_all = nc.get("ledger"), nc.get("panel")
    if led is None or led.empty or p_all is None:
        return [], ""
    top = top_evidence(led, k=3, outcomes=NOTE_OUTCOMES)
    lead = ""
    if top is None or top.empty:
        top = top_evidence(led, k=2, outcomes=NOTE_OUTCOMES, verdicts=("Suggestive",))
        lead = "Nothing holds up after correction yet; suggestive leads only:" if top is not None and len(top) else ""
    if top is None or top.empty:
        return [], "No ops metric holds up or is even suggestive for these injury measures yet."
    p = p_all[p_all["SITE"].isin(sites)]
    now = driver_now(p, sites, list(top["DRIVER"]), end_wk=wk) if len(p) else pd.DataFrame()
    now = now.set_index("DRIVER") if len(now) else now
    out = []
    for r in top.to_dict("records"):
        z = now.loc[r["DRIVER"], "Z"] if len(now) and r["DRIVER"] in now.index else np.nan
        pct = now.loc[r["DRIVER"], "PCT"] if len(now) and r["DRIVER"] in now.index else np.nan
        _, word = _arrow_tone(z, r["IRR"])
        cur = (f" Here now: {word}" + (f" ({_ord(pct)} percentile of these sites' weeks)" if np.isfinite(pct) else "") + "."
               if np.isfinite(z) else "")
        out.append(f"{r['LABEL']} → {OUT_SHORT.get(r['OUTCOME'], r['OUT_LABEL'])} rate: {effect_text(r)} per typical rise, "
                   f"{'same week' if r['LAG'] == 0 else 'next week'} ({r['VERDICT'].lower()}).{cur}")
    return out, lead


def _ahead_lines(nc, sites):
    ol = outlook_for(nc, sites)
    out = []
    plan, proj, mw = ol.get("plan"), ol.get("proj"), ol.get("met")
    if plan is not None and len(plan):
        p4 = plan.head(4)
        out.append("Outbound plan vs the last 4 weeks shipped: " + " · ".join(
            f"{_d(r.WK)} {_pct0(r.VS)}" for r in p4.itertuples() if np.isfinite(r.VS)) + ".")
        ramp = p4[p4["VS"] >= nc["th"]["ramp"]]
        if len(ramp):
            out.append(f"Ramp line (+{nc['th']['ramp']:.0f}%) crossed in the week{'s' if len(ramp) > 1 else ''} of "
                       f"{_list_words([_d(w) for w in ramp['WK']])}: staff for that week now (hiring, training, PIT certifications).")
    else:
        out.append("No S&OP outbound plan for these sites (the plan covers the core FCs).")
    if proj is not None and len(proj):
        p4 = proj[proj["WK"].isin(plan.head(4)["WK"])] if plan is not None and len(plan) else proj.head(4)
        if len(p4):
            top = p4.loc[p4["EXP"].idxmax()]
            line = (f"Projected injuries at plan (recordable + first aid): highest about {top['EXP']:.0f} in the week of "
                    f"{_d(top['WK'])}" + (f" vs about {ol['normal']:.0f} in a normal week" if np.isfinite(ol.get("normal", np.nan)) else "")
                    + (f"; hours {top['LOAD']:.2f}× normal" if np.isfinite(top.get("LOAD", np.nan)) else ""))
            act, watch = nc.get("act", np.nan), nc.get("watch", np.nan)
            if np.isfinite(act) and np.isfinite(top.get("LOAD", np.nan)) and top["LOAD"] >= act:
                line += ", over the act line"
            elif np.isfinite(watch) and np.isfinite(top.get("LOAD", np.nan)) and top["LOAD"] >= watch:
                line += ", over the watch line"
            out.append(line + ".")
    if mw is not None and len(mw):
        m4 = mw.head(4)
        on = m4[m4["MET_H"] > 0]
        if on.empty:
            out.append("MET: no positive scheduled hours observed in the loaded staffing-plan rows for the next 4 weeks.")
        else:
            out.append("MET planned: " + " · ".join(f"{_d(r.WK)} {r.MET_H:,.0f} h ({r.PCT:.1f}% of regular)" for r in on.itertuples())
                       + ". Rotate MET days so the same people don't carry back-to-back long weeks.")
    try:
        for ln in path_note_lines(nc.get("paths"), nc.get("dept_plan"), sites, nc.get("wk_sel", nc["cur_wk"]), nc["basis"],
                                  sop_plan=nc.get("plan"), eps=nc.get("eps", 0.6)):
            out.append(ln)
    except Exception:
        pass
    pr, it = nc.get("promos"), nc.get("items")
    if pr is not None:
        try:
            for ln in promo_note_lines(pr, it, sites):
                out.append("Promotion: " + ln)
        except Exception:
            pass
    return out


def _icare_line(nc, sites, wk):
    ic = nc.get("icare") or {}
    act, cases = ic.get("activity"), ic.get("cases")
    if act is None and cases is None:
        return ""
    bits = []
    if act is not None and len(act) and "DT" in act:
        a = act[act["SITE"].isin(sites) & (act["DT"] >= pd.Timestamp(wk)) & (act["DT"] <= pd.Timestamp(wk) + pd.Timedelta(days=6))]
        n = int(pd.to_numeric(a.get("CONTACTS"), errors="coerce").fillna(0).sum()) if "CONTACTS" in a else 0
        ni = int(pd.to_numeric(a.get("NOT_IMPROVING"), errors="coerce").fillna(0).sum()) if "NOT_IMPROVING" in a else 0
        bits.append(f"{n:,} follow-up records that week" + (f" ({ni:,} reported pain not improving)" if ni else ""))
    if cases is not None and len(cases):
        c = cases[cases["SITE"].isin(sites)]
        oc = int(pd.to_numeric(c.get("OPEN_CASES"), errors="coerce").fillna(0).sum()) if "OPEN_CASES" in c else 0
        nf = int(pd.to_numeric(c.get("OPEN_NO_FOLLOWUP"), errors="coerce").fillna(0).sum()) if "OPEN_NO_FOLLOWUP" in c else 0
        bits.append(f"{oc:,} open cases in the current snapshot" + (f" ({nf:,} with no first iCare date)" if nf else ""))
    if not bits:
        return ""
    return _cap1(" · ".join(bits)) + ". Follow-ups don't change OVSR status; use them to check that injured associates are being reached."


def build_note(nc, sites, label, kind, wk):
    """The weekly note for `sites` and the week starting `wk`, as sections the renderers share."""
    wk = pd.Timestamp(wk)
    so = g("STATUS_ORDER")
    fr = nc["fr"][nc["fr"]["SITE"].isin(sites)].copy()
    fr["_O"] = fr["STATUS"].map(so)
    fr = fr.sort_values(["_O", "N_TRIG", "SITE"], ascending=[True, False, True])
    hist = nc["hist"]
    hist = hist[hist["SITE"].isin(sites)]
    prev = {s: _prev_status(hist, s, wk) for s in fr["SITE"]}
    streak = {s: _streak(hist, s, wk) for s in fr["SITE"]}
    cnt = fr["STATUS"].value_counts()
    single = len(sites) == 1
    secs = []

    # Headline
    head = []
    calls = fr[fr["STATUS"].isin(NOTE_CALL)]
    watch = fr[fr["STATUS"] == "Watch"]
    if single and len(fr):
        r = fr.iloc[0]
        tail = _tail_txt(streak[r["SITE"]], prev[r["SITE"]], r["STATUS"])
        head.append(f"{r['SITE']} is {r['STATUS']}" + (f" ({tail})" if tail else "") + "."
                    + (f" Signals: {r['TRIGGERS']}." if r["TRIGGERS"] else " No OVSR trigger fired."))
    else:
        if len(calls):
            names = [f"{r['SITE']} ({r['STATUS']}" + (f", {_ord(streak[r['SITE']])} week flagged" if streak[r["SITE"]] >= 2 else
                                                       ", new" if prev[r["SITE"]] not in NOTE_FLAGGED else "") + ")"
                     for r in calls.head(6).to_dict("records")]
            more = f" and {len(calls) - 6} more" if len(calls) > 6 else ""
            head.append(f"{len(calls)} site{'s' if len(calls) != 1 else ''} need{'s' if len(calls) == 1 else ''} a call: "
                        f"{_list_words(names)}{more}.")
        else:
            head.append("No site is Elevated or Moderate Elevation.")
        mix = [f"{int(cnt.get(s_, 0))} {s_}" for s_ in so if cnt.get(s_, 0)]
        if mix:
            head.append(f"{len(fr)} sites rated: " + " · ".join(mix) + ".")
    counts, rates = _injury_line(nc["sw"], sites, wk, nc["basis"])
    if counts:   # one week's rate on a small denominator swings wildly: show counts only under 100K hours
        hrs = nc["sw"][nc["sw"]["SITE"].isin(sites) & (nc["sw"]["WK"] == wk)]["TOTAL_HOURS"].sum()
        head.append(counts + (" " + rates if rates and hrs >= NOTE_MIN_RATE_HOURS else ""))
        how = mech_week_text(nc.get("inj"), sites, wk)
        if how:
            head.append(how)
    ops = _ops_line(nc["ow"], sites, wk)
    if ops:
        head.append(ops)
    secs.append({"title": "Headline", "items": head})

    # What changed since the week before
    if not single:
        moves = {"New": [], "Escalated": [], "Eased": [], "Cleared": []}
        for r in fr.to_dict("records"):
            s, now, was = r["SITE"], r["STATUS"], prev[r["SITE"]]
            if was is None or was == now:
                continue
            f_now, f_was = now in NOTE_FLAGGED, was in NOTE_FLAGGED
            arrow = f"{s} ({was} → {now})"
            if f_now and not f_was:
                moves["New"].append(arrow)
            elif f_now and f_was:
                moves["Escalated" if so.get(now, 9) < so.get(was, 9) else "Eased"].append(arrow)
            elif f_was and not f_now:
                moves["Cleared"].append(arrow)
        items = [f"{k}: {', '.join(v)}." for k, v in moves.items() if v]
        secs.append({"title": "What changed since the week before",
                     "items": items or ["No status changes vs the week before."]})

    # Patterns: the same trigger at several flagged sites points to a network-level fix
    if not single:
        flagged = fr[fr["STATUS"].isin(NOTE_FLAGGED)]
        if len(flagged) >= 2:
            R = g("RESPONSES")
            by = {}
            for r in flagged.to_dict("records"):
                for k in r["FIRED"]:
                    by.setdefault(k, []).append(r["SITE"])
            pats = sorted(((k, v) for k, v in by.items() if len(v) >= 2), key=lambda kv: (-len(kv[1]), kv[0]))
            if pats:
                secs.append({"title": "Patterns across flagged sites",
                             "items": [f"{R[k][0]} ({R[k][1]}): {len(v)} sites · {', '.join(v)}" for k, v in pats[:6]]})

    # Sites to call (full blocks) and Watch (one line each)
    blocks = []
    show = fr if single else calls
    for i, r in enumerate(show.head(NOTE_MAX_CALL).to_dict("records"), 1):
        blocks.append(_site_block(nc, r, i, wk, prev[r["SITE"]], streak[r["SITE"]]))
    if single:
        secs.append({"title": "Site detail", "items": blocks or ["No rating for this site that week."]})
    else:
        if len(calls) > NOTE_MAX_CALL:
            blocks.append(f"{len(calls) - NOTE_MAX_CALL} more on the overview scorecard.")
        secs.append({"title": "Sites to call", "items": blocks or ["None this week."]})
        if len(watch):
            R = g("RESPONSES")
            wl = []
            for r in watch.head(NOTE_MAX_WATCH).to_dict("records"):
                f0 = r["FIRED"][0] if len(r["FIRED"]) else None
                tail = _tail_txt(streak[r["SITE"]], prev[r["SITE"]], r["STATUS"])
                wl.append(f"{r['SITE']} ({nc['regions'].get(r['SITE'], 'Other')}): {r['TRIGGERS']}" + (f" · {tail}" if tail else "")
                          + (f". Ask ({R[f0][1]}): {_first_sentence(R[f0][2])}" if f0 else "."))
            if len(watch) > NOTE_MAX_WATCH:
                wl.append(f"{len(watch) - NOTE_MAX_WATCH} more on Watch on the overview scorecard.")
            secs.append({"title": "On Watch", "items": wl})

    # Evidence, outlook, iCare
    mv, lead = _moving_lines(nc, sites, wk)
    n_sites = nc.get("ledger_sites", 0)
    mt = "What's moving injuries" + (f" (evidence across {n_sites} sites)" if n_sites else "")
    if mv:
        secs.append({"title": mt, "lead": lead, "items": mv})
    else:
        secs.append({"title": mt, "items": [lead or "The evidence ledger isn't available for this scope."]})
    ah = _ahead_lines(nc, sites)
    if nc.get("live_extras"):
        try:
            ye_l = ye_note_line(list(sites))
        except Exception:
            ye_l = ""
        if ye_l:
            ah = list(ah or []) + [ye_l]
    if ah:
        secs.append({"title": f"Ahead · from the week of {_d(nc['cur_wk'])}", "items": ah})
    ic = _icare_line(nc, sites, wk)
    ic_items = [ic] if ic else []
    if nc.get("fa"):
        try:
            fl = fa_note_line(nc, sites)
        except Exception:
            fl = ""
        if fl:
            ic_items.append(fl)
    if ic_items:
        secs.append({"title": "iCare", "items": ic_items})

    th_txt = "default thresholds" if nc.get("th_default", True) else "thresholds edited this session"
    in_prog = pd.Timestamp(wk) >= pd.Timestamp(nc["cur_wk"])
    wk_txt = f"Week of {_wk_range(wk)}" + (f" · so far, through {pd.Timestamp(nc['last_day']):%b %d} (provisional)"
                                          if in_prog else "")
    return {"title": f"OVSR weekly note · {label}", "week": wk_txt,
            "sub": f"Data through {pd.Timestamp(nc['last_day']):%b %d, %Y} · rates {nc['basis_label']} · {th_txt}",
            "sections": secs,
            "footer": "Status order: Elevated, Moderate Elevation, Watch, Normal. Status comes from ops variance vs plan "
                      "(that week) and safety rates (last 4 full weeks vs each site's own prior 13). Critical events and "
                      "iCare don't change status."}


# ---- renderers ----
def note_text(note):
    """Plain text for email, Teams or Slack (the person pastes it themselves)."""
    L = [note["title"], note["week"] + " · " + note["sub"]]
    for sec in note["sections"]:
        L += ["", sec["title"].upper()]
        if sec.get("lead"):
            L.append(sec["lead"])
        for it in sec["items"]:
            if isinstance(it, dict):
                L.append(f"{it['n']}) {it['site']} ({it['region']}) · {it['status']}" + (f" · {it['tail']}" if it["tail"] else ""))
                for lab, val in it["rows"]:
                    if lab == "Ask":
                        for owner, move in val:
                            L.append(f"   Ask ({owner}): {move}")
                    elif isinstance(val, list):
                        if len(val) == 1:
                            L.append(f"   {lab}: {val[0]}")
                        else:
                            L.append(f"   {lab}:")
                            L += [f"     - {v}" for v in val]
                    else:
                        L.append(f"   {lab}: {val}")
            else:
                L.append(f"- {it}")
    L += ["", note["footer"]]
    return "\n".join(L)


def _md_safe(t):
    """Keep data text literal in Streamlit markdown (no strikethrough, LaTeX or emphasis from stray symbols)."""
    return str(t).replace("\\", "\\\\").replace("~", "\\~").replace("$", "\\$").replace("*", "\\*").replace("_", "\\_")


def note_markdown(note):
    """Render the note inside the app (the caller wraps it in a bordered container)."""
    SMD = g("STATUS_MD") or {}
    st.markdown(f"#### {_md_safe(note['title'])}")
    st.markdown(f"**{_md_safe(note['week'])}** · {_md_safe(note['sub'])}")
    for sec in note["sections"]:
        st.markdown(f"##### {_md_safe(sec['title'])}")
        if sec.get("lead"):
            st.markdown(_md_safe(sec["lead"]))
        lines = []
        for it in sec["items"]:
            if isinstance(it, dict):
                if lines:
                    st.markdown("\n".join(lines))
                    lines = []
                md = [f"**{it['n']}. {_md_safe(it['site'])}** ({_md_safe(it['region'])}) · "
                      f"{SMD.get(it['status'], _md_safe(it['status']))}" + (f" · {_md_safe(it['tail'])}" if it["tail"] else "")]
                for lab, val in it["rows"]:
                    if lab == "Ask":
                        md += [f"- **Ask ({_md_safe(o)}):** {_md_safe(m)}" for o, m in val]
                    elif isinstance(val, list):
                        md.append(f"- **{lab}:**" + (f" {_md_safe(val[0])}" if len(val) == 1 else ""))
                        if len(val) > 1:
                            md += [f"    - {_md_safe(v)}" for v in val]
                    else:
                        md.append(f"- **{lab}:** {_md_safe(val)}")
                st.markdown("\n".join(md))
            else:
                lines.append(f"- {_md_safe(it)}")
        if lines:
            st.markdown("\n".join(lines))
    st.caption(note["footer"])


def note_html(note, copy_id="note-copy"):
    """Snapshot tab: the formatted note plus a copy-ready text block."""
    sc = g("STATUS_COLOR") or {}
    s = [f"<h2>{E(note['title'])}</h2><p class='cap'><b>{E(note['week'])}</b> · {E(note['sub'])}</p><div class='note'>"]
    for sec in note["sections"]:
        s.append(f"<h3>{E(sec['title'])}</h3>")
        if sec.get("lead"):
            s.append(f"<p>{E(sec['lead'])}</p>")
        ul = []
        for it in sec["items"]:
            if isinstance(it, dict):
                if ul:
                    s.append("<ul>" + "".join(ul) + "</ul>")
                    ul = []
                rows = []
                for lab, val in it["rows"]:
                    if lab == "Ask":
                        rows += [f"<li><b>Ask ({E(o)}):</b> {E(m)}</li>" for o, m in val]
                    elif isinstance(val, list):
                        rows.append(f"<li><b>{E(lab)}:</b> " + (E(val[0]) if len(val) == 1 else
                                    "<ul>" + "".join(f"<li>{E(v)}</li>" for v in val) + "</ul>") + "</li>")
                    else:
                        rows.append(f"<li><b>{E(lab)}:</b> {E(val)}</li>")
                s.append(f"<div class='nsite' style='--c:{sc.get(it['status'], '#9a9893')}'><p class='nh'><b>{it['n']}. {E(it['site'])}</b> "
                         f"({E(it['region'])}) <span class='st' style='--c:{sc.get(it['status'], '#9a9893')}'><i></i>{E(it['status'])}</span>"
                         + (f" <span class='cap'>· {E(it['tail'])}</span>" if it["tail"] else "") + f"</p><ul>{''.join(rows)}</ul></div>")
            else:
                ul.append(f"<li>{E(it)}</li>")
        if ul:
            s.append("<ul>" + "".join(ul) + "</ul>")
    s.append(f"<p class='cap'>{E(note['footer'])}</p></div>")
    s.append(f"<h3>Copy-ready text</h3><p class='cap'>Paste into email, Teams or Slack. "
             f"<button class='copybtn' data-copy='{copy_id}'>Copy</button></p><pre class='notetxt' id='{copy_id}'>{E(note_text(note))}</pre>")
    return "".join(s)


def _code_wrapped(txt):
    try:
        st.code(txt, language=None, wrap_lines=True)
    except TypeError:  # older Streamlit: no wrap_lines
        st.code(txt, language=None)


def render_note():
    """Weekly note tab: rebuilt from the latest data on every run; pick the week and the scope."""
    BORDER = g("BORDER") or {}
    basis, basis_label = g("basis"), g("basis_label")
    th = g("ovsr_th_current")()
    th_items = tuple(sorted(th.items()))
    dfact = _daily_fact()
    pool = sorted(g("scope")(dfact, g("sel_regions"), g("sel_sites"))["SITE"].unique())
    st.markdown("### Weekly note for site EHS")
    if not pool or dfact is None or dfact.empty:
        st.info("No sites for the sidebar filters.")
        return
    today_ts = pd.Timestamp(g("_today_et")())
    cur_wk = _wk_of(today_ts)
    last_day = pd.Timestamp(dfact["DT"].max())
    wk_all = sorted({_wk_of(d) for d in pd.to_datetime(dfact["DT"].drop_duplicates())})
    wks = [w for w in wk_all if w < cur_wk and w + pd.Timedelta(days=6) <= last_day][-8:]
    if not wks:
        st.info("No complete week of data yet.")
        return
    # The week in progress, listed first; it rebuilds as new days land (provisional, never Elevated).
    live_wk = cur_wk if last_day >= cur_wk else None
    opts = ([live_wk] if live_wk is not None else []) + wks[::-1]
    rl = g("region_lookup") or {}
    regions = {s: rl.get(s, "Other") for s in pool}
    scopes = scopes_for({"pool": pool, "regions": regions})
    labels = {sid: lab for sid, lab, _, _ in scopes}
    c1, c2 = st.columns([1.3, 2])
    def _wk_opt(w):
        if live_wk is not None and w == live_wk:
            return f"Week of {_wk_range(w)} · this week so far (through {last_day:%b %d})"
        return f"Week of {_wk_range(w)}" + (" · latest full week" if w == wks[-1] else "")
    wk = c1.selectbox("Week", opts, index=0, key="note_wk", format_func=_wk_opt,
                      help="This week so far is provisional: it updates as each day loads and can't reach Elevated "
                           "until the week is complete.")
    sid = c2.selectbox("Note for", [s_[0] for s_ in scopes], key="note_scope", format_func=lambda k: labels.get(k, k),
                       help="All sites in the sidebar scope, a region, or one site (a site's own note for its EHS lead).")
    _, label, sites, kind = next(s_ for s_ in scopes if s_[0] == sid)
    with st.spinner("Writing the note from the latest data…"):
        nf = note_frames(basis, th_items, tuple(pool), pd.Timestamp(wk), cur_wk, last_day)
        p_all, _, wf_on = _panel_for_app()
        p_pool = p_all[p_all["SITE"].isin(pool)]
        try:
            led, _ = ledger_cached(basis, wf_on, tuple(sorted(p_pool["SITE"].unique()))) if len(p_pool) >= LEDGER_MIN_N else (pd.DataFrame(), 0)
        except Exception:
            led = pd.DataFrame()
        nc = {**nf, "basis": basis, "basis_label": basis_label, "th": th,
              "th_default": th == {k: float(v[1]) for k, v in (g("OVSR_TH") or {}).items()}, "regions": regions,
              "cur_wk": cur_wk, "last_day": last_day, "panel": p_all, "ledger": led,
              "ledger_sites": int(p_pool["SITE"].nunique()), "icare": g("icare_frames") or {}, "inj": load_x("x_inj")[0]}
        idata = _items_if_warm()
        if idata is not None:
            nc["items"], nc["promos"] = idata[0], idata[2]
        pres = paths_if_warm(basis)
        if pres is not None:
            nc["paths"] = pres[0]
            nc["dept_plan"] = load_x("x_dept_plan")[0]
        nc["wk_sel"] = pd.Timestamp(wk)
        nc["live_extras"] = True
        nc["fa"] = fa_data()[0]
        note = build_note(nc, sites, label, kind, wk)
        txt = note_text(note)
    st.caption("Rebuilt from the latest data every time the app loads. Opens on this week so far (provisional; it "
               "refreshes as each day lands); pick a complete week for the final read. Uses the sidebar region and "
               "site filters and the thresholds in force.")
    with st.expander("Copy-ready text · paste into email, Teams or Slack (copy icon at the top right of the box)"):
        _code_wrapped(txt)
    with st.container(**BORDER):
        note_markdown(note)
    st.download_button("Download the note (.txt)", txt.encode("utf-8"), key="note_dl", mime="text/plain",
                       file_name=f"OVSR_weekly_note_{pd.Timestamp(wk):%Y%m%d}_{re.sub(r'[^A-Za-z0-9]+', '_', label).strip('_')}.txt")
    st.caption("Nothing is sent from the app. Site codes, counts and rates only: no names, IDs or narratives.")


def tab_note(ctx, sites, label, kind, ol, pw):
    """Snapshot: the weekly note for the last full week and this scope."""
    p_all = ctx["panel"]
    nc = {"sw": ctx["sw"], "ow": ctx["ow"], "fr": ctx["fr_last"], "hist": ctx["hist"], "plan": ctx["plan"], "proj": ctx["proj"],
          "pbase": ctx["pbase"], "watch": ctx["watch"], "act": ctx["act"], "met": ctx.get("met"), "publish": ctx.get("publish"),
          "basis": ctx["basis"], "basis_label": ctx["basis_label"], "th": ctx["th"], "th_default": ctx.get("th_default", True),
          "regions": ctx["regions"], "cur_wk": ctx["cur_wk"], "last_day": ctx["last_day"], "panel": p_all,
          "ledger": ctx.get("ledger"), "ledger_sites": ctx.get("panel_sites", 0), "icare": ctx.get("icare") or {},
          "inj": ctx.get("inj"), "items": ctx.get("items"), "promos": ctx.get("promos"), "paths": ctx.get("paths"),
          "dept_plan": ctx.get("dept_plan"), "wk_sel": ctx["lw"], "eps": ctx.get("eps", 0.6)}
    note = build_note(nc, sites, label, kind, ctx["lw"])
    return note_html(note, copy_id="note-" + re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-").lower())


# ---------------------------------------------------------------------------------------------------------------
# Mechanism of injury: how people get hurt (lifting, carrying, overreaching, repetitive motion, ...), how serious,
# when in the day, and in which department. Counts come from the Intelex incident detail (aggregated in Snowflake);
# rates use the OBR safety table's hours for the same sites and weeks.
# ---------------------------------------------------------------------------------------------------------------
MECH_TREND = {"Lift & carry": ["Lifting / lowering", "Carrying"],
              "Reach, repetition & posture": ["Overreaching", "Repetitive motion", "Bending / twisting / posture"],
              "Struck by / against": ["Struck by / against"],
              "Slip, trip or fall": ["Slip, trip or fall"]}
MECH_BANDS = [(0, 4, "12–4 am"), (4, 8, "4–8 am"), (8, 12, "8 am–12 pm"), (12, 16, "12–4 pm"), (16, 20, "4–8 pm"),
              (20, 24, "8 pm–12 am")]
MECH_ERGO = MECH_GROUPS["INJ_LIFT"] + MECH_GROUPS["INJ_REACH"]


def _mech_hours(sites):
    """Hours worked per week for `sites` (OBR safety table, all days loaded)."""
    sw = g("site_weeks")(_daily_fact())
    sw = sw[sw["SITE"].isin(sites)]
    return sw.groupby("WK")["TOTAL_HOURS"].sum()


def _full_weeks_to(end_wk, n):
    end_wk = pd.Timestamp(end_wk)
    return [end_wk - pd.Timedelta(weeks=k) for k in range(n - 1, -1, -1)]


def mech_table(inj, sites, lo_wk, hi_wk, end_full, basis, hrs_wk=None):
    """One row per mechanism for the range: counts, share, MSD, serious, lost time, rate, and the last 4 full weeks'
    rate vs the 13 before (both ending at `end_full`)."""
    if inj is None or inj.empty or "MECH" not in inj.columns:
        return pd.DataFrame()
    hrs_wk = _mech_hours(sites) if hrs_wk is None else hrs_wk
    d_all = inj[inj["SITE"].isin(sites)]
    d = d_all[(d_all["WK"] >= pd.Timestamp(lo_wk)) & (d_all["WK"] <= pd.Timestamp(hi_wk))]
    if d.empty:
        return pd.DataFrame()
    t = d.assign(LOST=d["INJURIES"].where(d["SEVERITY"] == "Lost time", 0)).groupby("MECH")[
        ["INJURIES", "MSD", "RECORDABLES", "LOST"]].sum()
    t = t.reindex([m for m in MECH_ORDER if m in t.index] + [m for m in t.index if m not in MECH_ORDER]).fillna(0)
    t["SHARE"] = t["INJURIES"] / max(t["INJURIES"].sum(), 1) * 100
    t["SERIOUS_PCT"] = t["RECORDABLES"] / t["INJURIES"].where(t["INJURIES"] > 0) * 100
    h_rng = hrs_wk[(hrs_wk.index >= pd.Timestamp(lo_wk)) & (hrs_wk.index <= pd.Timestamp(hi_wk))].sum()
    t["RATE"] = t["INJURIES"] * basis / h_rng if h_rng > 0 else np.nan
    l4, p13 = _full_weeks_to(end_full, 4), _full_weeks_to(pd.Timestamp(end_full) - pd.Timedelta(weeks=4), 13)
    h4, h13 = hrs_wk.reindex(l4).sum(), hrs_wk.reindex(p13).sum()
    n4 = d_all[d_all["WK"].isin(l4)].groupby("MECH")["INJURIES"].sum().reindex(t.index).fillna(0)
    n13 = d_all[d_all["WK"].isin(p13)].groupby("MECH")["INJURIES"].sum().reindex(t.index).fillna(0)
    t["R4"] = n4 * basis / h4 if h4 > 0 else np.nan
    t["R13"] = n13 * basis / h13 if h13 > 0 else np.nan
    t["CHG"] = (t["R4"] / t["R13"].where(t["R13"] > 0) - 1) * 100
    t["N4"], t["N13"] = n4, n13
    return t.reset_index().rename(columns={"index": "MECH"})


def mech_weekly(inj, sites, weeks, basis, roll=4, hrs_wk=None):
    """4-week rolling rate per group in MECH_TREND for each week in `weeks`."""
    if inj is None or inj.empty or "MECH" not in inj.columns:
        return pd.DataFrame()
    hrs_wk = _mech_hours(sites) if hrs_wk is None else hrs_wk
    d = inj[inj["SITE"].isin(sites)]
    allw = sorted(set(weeks) | {w - pd.Timedelta(weeks=k) for w in weeks for k in range(roll)})
    out = pd.DataFrame({"WK": allw})
    h = hrs_wk.reindex(allw).to_numpy(float)
    for lab, ms in MECH_TREND.items():
        n = d[d["MECH"].isin(ms)].groupby("WK")["INJURIES"].sum().reindex(allw).fillna(0).to_numpy(float)
        rn = pd.Series(n).rolling(roll, min_periods=roll).sum()
        rh = pd.Series(h).rolling(roll, min_periods=roll).sum()
        out[lab] = (rn * basis / rh.where(rh > 0)).to_numpy()
    return out[out["WK"].isin(weeks)].reset_index(drop=True)


def _band(hr):
    for a, b, lab in MECH_BANDS:
        if a <= hr < b:
            return lab
    return "Not recorded"


def mech_pivot(d, col, top_cols=8):
    """Mechanism × another dimension (counts), mechanisms in MECH_ORDER, the busiest `top_cols` columns."""
    if d is None or d.empty or "MECH" not in d.columns:
        return pd.DataFrame()
    if col == "BAND":
        d = d.assign(BAND=[_band(h) if h is not None and h >= 0 else "Not recorded" for h in d["EVENT_HOUR"].fillna(-1)])
        cols = [b for _, _, b in MECH_BANDS]
    else:
        cols = list(d.groupby(col)["INJURIES"].sum().sort_values(ascending=False).head(top_cols).index)
    pv = d.pivot_table(index="MECH", columns=col, values="INJURIES", aggfunc="sum", fill_value=0)
    pv = pv.reindex(index=[m for m in MECH_ORDER if m in pv.index], columns=[c for c in cols if c in pv.columns]).fillna(0)
    return pv[pv.sum(axis=1) > 0]


def _ergo_line(t):
    if t.empty:
        return ""
    tot, msd = t["INJURIES"].sum(), t["MSD"].sum()
    e = t[t["MECH"].isin(MECH_ERGO)]
    if not tot:
        return ""
    return (f"Lifting, carrying, overreaching, repetitive motion and bending/twisting caused {e['INJURIES'].sum() / tot * 100:.0f}% "
            f"of injuries" + (f" and {e['MSD'].sum() / msd * 100:.0f}% of MSD injuries" if msd else "") + " in this range.")


def _heat_chart(pv, title):
    """Counts heatmap (one sequential hue), labels on cells."""
    if pv.empty:
        return None
    m = pv.reset_index().melt("MECH", var_name="COL", value_name="N")
    rows, cols = list(pv.index), list(pv.columns)
    base = alt.Chart(m).encode(
        x=alt.X("COL:N", sort=cols, title=None, axis=alt.Axis(labelAngle=-35, labelLimit=120, ticks=False, domain=False,
                                                              labelOverlap=False), scale=alt.Scale(paddingInner=0.06)),
        y=alt.Y("MECH:N", sort=rows, title=None, axis=alt.Axis(ticks=False, domain=False, labelLimit=220, labelOverlap=False),
                scale=alt.Scale(paddingInner=0.06)))
    heat = base.mark_rect(cornerRadius=2).encode(
        color=alt.Color("N:Q", title=title, scale=alt.Scale(range=g("SEQ_BLUE") or ["#cde2fb", "#0d366b"]),
                        legend=alt.Legend(orient="bottom")),
        tooltip=[alt.Tooltip("MECH:N", title="Mechanism"), alt.Tooltip("COL:N", title=""), alt.Tooltip("N:Q", title="Injuries")])
    mx = float(m["N"].max() or 1)
    txt = base.mark_text(fontSize=11).encode(
        text=alt.condition("datum.N > 0", alt.Text("N:Q", format=",.0f"), alt.value("")),
        color=alt.condition(f"datum.N > {mx * 0.55}", alt.value("#ffffff"), alt.value("#1f2328")))
    return (heat + txt).properties(height=alt.Step(26))


def render_mech(inj, pool, lo_wk, hi_wk):
    """App: the 'How people get hurt' section at the top of Injury profile."""
    BORDER, STRETCH, NC = g("BORDER") or {}, g("STRETCH") or {}, st.column_config.NumberColumn
    basis = g("basis")
    st.markdown("##### How people get hurt · mechanism of injury")
    if inj is None or "MECH" not in inj.columns:
        st.caption("Mechanism needs the updated incident query; it shows after the next data refresh.")
        return
    today_ts = pd.Timestamp(g("_today_et")())
    # Latest full Sunday week that ends on or before the range end (a mid-week end date would match no week key).
    end_full = min(_wk_of(today_ts), _wk_of(pd.Timestamp(hi_wk) + pd.Timedelta(days=1))) - pd.Timedelta(weeks=1)
    hrs_wk = _mech_hours(pool)
    t = mech_table(inj, pool, lo_wk, hi_wk, end_full, basis, hrs_wk)
    if t.empty:
        st.caption("No injuries with a mechanism in the sidebar range.")
        return
    tot = t["INJURIES"].sum()
    lift = t[t["MECH"].isin(MECH_GROUPS["INJ_LIFT"])]
    reach = t[t["MECH"].isin(MECH_GROUPS["INJ_REACH"])]
    k = g("kpi_cols")(4)
    k[0].metric("Lift & carry", f"{int(lift['INJURIES'].sum()):,}", f"{lift['INJURIES'].sum() / tot * 100:.0f}% of injuries",
                delta_color="off", help="Lifting, lowering and carrying.", **BORDER)
    k[1].metric("Reach, repetition & posture", f"{int(reach['INJURIES'].sum()):,}",
                f"{reach['INJURIES'].sum() / tot * 100:.0f}% of injuries", delta_color="off",
                help="Overreaching, repetitive motion, bending and twisting.", **BORDER)
    k[2].metric("Recordable or lost time", f"{int(t['RECORDABLES'].sum()):,}", f"{t['RECORDABLES'].sum() / tot * 100:.0f}% of injuries",
                delta_color="off", **BORDER)
    big = t[t["INJURIES"] >= 20].sort_values("SERIOUS_PCT", ascending=False)
    k[3].metric("Most serious mechanism", big["MECH"].iloc[0] if len(big) else "—",
                f"{big['SERIOUS_PCT'].iloc[0]:.0f}% recordable or lost time" if len(big) else None, delta_color="off",
                help="Highest share of recordable or lost-time cases among mechanisms with 20+ injuries.", **BORDER)
    st.dataframe(t[["MECH", "INJURIES", "SHARE", "MSD", "RECORDABLES", "SERIOUS_PCT", "LOST", "RATE", "R4", "R13", "CHG"]],
                 hide_index=True, **STRETCH, column_config={
                     "MECH": "Mechanism", "INJURIES": NC("Injuries", format="%d"), "SHARE": NC("Share", format="%.0f%%"),
                     "MSD": NC("MSD", format="%d"), "RECORDABLES": NC("Recordable or lost time", format="%d"),
                     "SERIOUS_PCT": NC("% serious", format="%.0f%%"), "LOST": NC("Lost time", format="%d"),
                     "RATE": NC("Rate", format="%.2f", help=f"Injuries per {basis:,} hours in the range."),
                     "R4": NC("Last 4 wks", format="%.2f", help=f"Rate in the 4 full weeks to {end_full:%b %d}."),
                     "R13": NC("13 wks before", format="%.2f"), "CHG": NC("Change", format="%+.0f%%")})
    st.caption(_ergo_line(t) + " Mechanism comes from Intelex's cause of injury; lifting vs carrying, overreaching vs "
               "bending and the 'Other' cases are sorted by keywords in the event description, read inside Snowflake "
               "(the text never leaves it). With no usable cause, a case counts as PIT or equipment only when the "
               "text names equipment and says it struck, pinned, ran over or backed into someone, tipped, or that "
               "the TM fell off or was operating it. Rates use hours worked at the same sites.")
    wk_all = sorted(w for w in hrs_wk.index if w <= end_full)[-52:]
    mw = mech_weekly(inj, pool, wk_all, basis, hrs_wk=hrs_wk)
    if len(mw):
        st.markdown(f"**How people get hurt by week** · 4-week rolling rate per {basis:,} hours")
        g("show_chart")(g("_wk_chart")(mw, {k_: k_ for k_ in MECH_TREND}, "Injuries per " + f"{basis:,} h", ".2f",
                                       height=260, colors=(g("PALETTE") or [])[:len(MECH_TREND)]))
    d = injury_profile(inj, pool, lo_wk, hi_wk)
    a, b = g("pair_cols")(2)
    with a:
        st.markdown("**Mechanism by department**")
        ch = _heat_chart(mech_pivot(d, "DEPT", 6), "Injuries")
        if ch is not None:
            g("show_chart")(ch)
    with b:
        st.markdown("**Mechanism by time of day**")
        ch = _heat_chart(mech_pivot(d, "BAND"), "Injuries")
        if ch is not None:
            g("show_chart")(ch)
    nr = int(d.loc[d["EVENT_HOUR"].fillna(-1) < 0, "INJURIES"].sum()) if "EVENT_HOUR" in d else 0
    st.caption("Time of day is the event time in Intelex (site local time)." + (f" {nr:,} injuries have no usable time." if nr else ""))


def mech_html(ctx, sites):
    """Snapshot: the same section for the Ops x Safety tab."""
    inj = ctx.get("inj")
    if inj is None or "MECH" not in inj.columns:
        return ""
    rng, basis = ctx["rng"], ctx["basis"]
    hrs_wk = _mech_hours(sites)
    t = mech_table(inj, sites, rng[0], rng[-1], ctx["lw"], basis, hrs_wk)
    if t.empty:
        return ""
    fi = lambda v: fnum(v, ",.0f")   # noqa: E731
    s = [f"<h2>How people get hurt · {_wk(rng[0])} – {_wk(rng[-1] + pd.Timedelta(days=6), '%b %d, %Y')}</h2>"
         f"<p class='cap'>{E(_ergo_line(t))} Last 4 weeks and the 13 before end the week of {_wk(ctx['lw'])}. Rates per "
         f"{basis:,} hours.</p>"]
    s.append(html_table(t, [("MECH", "Mechanism", None), ("INJURIES", "Injuries", fi), ("SHARE", "Share", lambda v: fnum(v, ".0f", "%")),
                            ("MSD", "MSD", fi), ("RECORDABLES", "Recordable or lost time", fi),
                            ("SERIOUS_PCT", "% serious", lambda v: fnum(v, ".0f", "%")), ("LOST", "Lost time", fi),
                            ("RATE", "Rate", lambda v: fnum(v, ".2f")), ("R4", "Last 4 wks", lambda v: fnum(v, ".2f")),
                            ("R13", "13 wks before", lambda v: fnum(v, ".2f")), ("CHG", "Change", lambda v: fnum(v, "+.0f", "%"))]))
    wk_all = sorted(w for w in hrs_wk.index if w <= ctx["lw"])[-52:]
    mw = mech_weekly(inj, sites, wk_all, basis, hrs_wk=hrs_wk)
    if len(mw):
        s.append(f"<h3>By week · 4-week rolling rate per {basis:,} hours</h3>" + svg_chart(
            [w.strftime("%m/%d") for w in mw["WK"]], [(k_, list(mw[k_]), P[i]) for i, k_ in enumerate(MECH_TREND)],
            yfmt=".2f", height=230, x_every=4))
    d = injury_profile(inj, sites, rng[0], rng[-1])
    parts = []
    for col, title in (("DEPT", "By department"), ("BAND", "By time of day")):
        pv = mech_pivot(d, col, 6)
        if pv.empty:
            continue
        df = pv.reset_index()
        parts.append(f"<div><h3>{title}</h3>" + html_table(df, [("MECH", "Mechanism", None)] + [(c, str(c), fi) for c in pv.columns],
                                                             cls="hmt") + "</div>")
    if parts:
        s.append("<div class='two'>" + "".join(parts) + "</div>")
    return "".join(s)


def mech_week_text(inj, sites, wk, top=3):
    """Weekly note: the top mechanisms that week, with the prior 4 weeks' average, plus the lift/reach share."""
    if inj is None or "MECH" not in inj.columns:
        return ""
    wk = pd.Timestamp(wk)
    d = inj[inj["SITE"].isin(sites)]
    cur = d[d["WK"] == wk].groupby("MECH")["INJURIES"].sum()
    if cur.sum() <= 0:
        return ""
    prev = d[d["WK"].isin([wk - pd.Timedelta(weeks=k) for k in range(1, 5)])].groupby("MECH")["INJURIES"].sum() / 4
    cur = cur.sort_values(ascending=False)
    bits = [f"{m.lower() if m not in ('PIT or equipment',) else m} {int(n)} (avg {prev.get(m, 0):.1f})" for m, n in cur.head(top).items()]
    ergo = cur[cur.index.isin(MECH_ERGO)].sum()
    return (f"How people got hurt: {', '.join(bits)}; lifting, carrying, overreaching, repetitive motion and bending/twisting "
            f"were {int(ergo)} of {int(cur.sum())} injury reports that week (all severities).")


def mech_site_text(inj, site, wk):
    if inj is None or "MECH" not in inj.columns:
        return ""
    d = inj[(inj["SITE"] == site) & (inj["WK"] == pd.Timestamp(wk))].groupby("MECH")["INJURIES"].sum().sort_values(ascending=False)
    if d.sum() <= 0:
        return ""
    return ", ".join(f"{int(n)} {m.lower() if m != 'PIT or equipment' else m}" for m, n in d.head(4).items())


# ---------------------------------------------------------------------------------------------------------------
# Items and promotions: item numbers written in injury reports (read inside Snowflake), joined to product-catalog weight and
# dimensions; upcoming promotions that put those items, or heavy items, in front of the floor.
# ---------------------------------------------------------------------------------------------------------------
X_SRC.update({"promo": "EDLDB.BT_RETAIL.DIM_PROMOTION", "promo_prod": "EDLDB.BT_RETAIL.DIM_PROMOTION_PRODUCT_ASSOCIATION",
              "products": "EDLDB.CHEWYBI.PRODUCTS"})
HEAVY_LB = 40                 # heavy item line (warehouse packaged weight)
PROMO_MAX_PRODUCTS = 60000    # promotions covering more products than this are sitewide offers, not item risk
PROMO_MIN_PRODUCTS = 25       # smaller ones are personal codes (influencer, win-back), not a floor-volume event
WBANDS = ["Under 10 lb", "10–24 lb", "25–39 lb", "40–59 lb", "60 lb +", "Not stated"]
_W = "COALESCE(NULLIF(PRODUCT_WAREHOUSE_PACKAGED_WEIGHT, 0), NULLIF(PRODUCT_PACKAGED_WEIGHT, 0), NULLIF(PRODUCT_WEIGHT, 0))"

# One row per injury with the item number written in the report (if any) and a weight stated in the text (if any).
# Item weight and size: Chewy product catalog, warehouse-packaged (the unit as handled in the FC).
_INJ_ITEMS_CTE = f"""
inj AS (
  SELECT COALESCE(REGEXP_SUBSTR(UPPER(LOCATION), '[A-Z]{{3}}[0-9]'), TRIM(LOCATION)) AS SITE,
         DATEADD(day, -(DAYOFWEEKISO(EVENT_DATE::DATE) % 7), EVENT_DATE::DATE)::DATE AS WK,
         REGEXP_SUBSTR(T2, '(^|[^a-z])(item|sku|part|product|itm)[^0-9]{{0,12}}([0-9]{{4,8}})([^0-9]|$)', 1, 1, 'e', 3) AS ITEM,
         TRY_TO_DOUBLE(REGEXP_SUBSTR(T2, '([0-9]{{1,3}}([.][0-9])?) ?(lb|lbs|pound|pounds)([^a-z]|$)', 1, 1, 'e', 1)) AS LBS,
         {_MECH_SQL} AS MECH,
         {_B('MSD_INJURY')} AS IS_MSD,
         ({_B('OSHA_RECORDABLE')} OR INJURY_CLASSIFICATION ILIKE ANY ('recordable%', 'lost time%')) AS IS_REC
  FROM (SELECT *, {_TXT_SQL} AS TXT,
               LOWER(COALESCE(EVENT_NARRATIVE, '') || ' ' || COALESCE(ACTIVITY_BEFORE_INCIDENT, '') || ' '
                     || COALESCE(SUSPECTED_CAUSE, '') || ' ' || COALESCE(IMMEDIATE_ACTION, '')) AS T2
        FROM {X_SRC['inc']}
        WHERE INCIDENT_TYPE ILIKE 'injury%' AND EVENT_DATE >= '2025-06-01'
          AND COALESCE(INJURY_CLASSIFICATION, '') NOT ILIKE 'non-occupational%')),
iu AS (
  SELECT PRODUCT_PART_NUMBER AS ITEM_NUMBER, ANY_VALUE(PRODUCT_NAME) AS DESCR, MAX({_W}) AS W,
         MAX(COALESCE(NULLIF(PRODUCT_WAREHOUSE_PACKAGED_LENGTH, 0), PRODUCT_PACKAGED_LENGTH)) AS L,
         MAX(COALESCE(NULLIF(PRODUCT_WAREHOUSE_PACKAGED_WIDTH, 0), PRODUCT_PACKAGED_WIDTH)) AS WD,
         MAX(COALESCE(NULLIF(PRODUCT_WAREHOUSE_PACKAGED_HEIGHT, 0), PRODUCT_PACKAGED_HEIGHT)) AS H,
         ANY_VALUE(PRODUCT_CATEGORY_LEVEL1) AS CAT
  FROM {X_SRC['products']}
  WHERE PRODUCT_PART_NUMBER IN (SELECT ITEM FROM inj WHERE ITEM IS NOT NULL)
  GROUP BY 1),
ti AS (   -- injuries tied to a real item (the number exists in the product catalog)
  SELECT inj.*, iu.W, iu.L, iu.WD, iu.H, iu.DESCR, iu.CAT
  FROM inj JOIN iu ON iu.ITEM_NUMBER = inj.ITEM)"""

SQL_ITEMS = f"""
WITH {_INJ_ITEMS_CTE.strip()}
SELECT 'ITEM' AS LVL, ITEM, ANY_VALUE(DESCR) AS DESCR, ANY_VALUE(CAT) AS CAT, ANY_VALUE(W) AS W, ANY_VALUE(L) AS L,
       ANY_VALUE(WD) AS WD, ANY_VALUE(H) AS H, SITE, NULL::STRING AS WBAND, NULL::STRING AS MECH,
       MIN(WK) AS FIRST_WK, MAX(WK) AS LAST_WK, COUNT(*) AS INJURIES, COUNT_IF(IS_MSD) AS MSD, COUNT_IF(IS_REC) AS RECORDABLES,
       COUNT_IF(MECH IN ('Lifting / lowering', 'Carrying')) AS LIFT, COUNT_IF(MECH = 'Struck by / against') AS STRUCK
FROM ti GROUP BY ITEM, SITE
UNION ALL
SELECT 'WEEK', NULL, NULL, NULL, NULL, NULL, NULL, NULL, SITE,
       CASE WHEN COALESCE(IW, LBS) IS NULL THEN 'Not stated' WHEN COALESCE(IW, LBS) < 10 THEN 'Under 10 lb'
            WHEN COALESCE(IW, LBS) < 25 THEN '10–24 lb' WHEN COALESCE(IW, LBS) < 40 THEN '25–39 lb'
            WHEN COALESCE(IW, LBS) < 60 THEN '40–59 lb' ELSE '60 lb +' END AS WB,
       MECH, WK, WK, COUNT(*), COUNT_IF(IS_MSD), COUNT_IF(IS_REC),
       COUNT_IF(MECH IN ('Lifting / lowering', 'Carrying')), COUNT_IF(MECH = 'Struck by / against')
FROM (SELECT inj.*, iu.W AS IW FROM inj LEFT JOIN iu ON iu.ITEM_NUMBER = inj.ITEM)
GROUP BY SITE, WB, MECH, WK
"""

SQL_PROMO = f"""
WITH {_INJ_ITEMS_CTE.strip()},
pr AS (
  SELECT PROMOTION_KEY, PROMOTION_NAME, EVENT_NAME, PROMOTION_TYPE, DISCOUNT_TYPE, DISCOUNT_DEPTH,
         PROMOTION_START_TS::DATE AS START_D, PROMOTION_END_TS::DATE AS END_D
  FROM {X_SRC['promo']}
  WHERE PROMOTION_END_TS >= CURRENT_DATE() AND PROMOTION_START_TS <= DATEADD(week, 13, CURRENT_DATE())
    AND PROMOTION_START_TS >= DATEADD(week, -8, CURRENT_DATE()) AND NOT COALESCE(IS_DELETED, FALSE)),
pp0 AS (
  SELECT DISTINCT pa.PROMOTION_KEY, p.PRODUCT_PART_NUMBER AS ITEM, {_W.replace('PRODUCT_', 'p.PRODUCT_')} AS W
  FROM {X_SRC['promo_prod']} pa JOIN pr ON pr.PROMOTION_KEY = pa.PROMOTION_KEY
  JOIN {X_SRC['products']} p ON p.PRODUCT_KEY = pa.PRODUCT_KEY
  WHERE pa.IS_CURRENT AND p.PRODUCT_PART_NUMBER IS NOT NULL),
np AS (SELECT PROMOTION_KEY, COUNT(DISTINCT ITEM) AS NP, COUNT(DISTINCT IFF(W >= {HEAVY_LB}, ITEM, NULL)) AS N_HEAVY
       FROM pp0 GROUP BY 1),
pp AS (SELECT pp0.* FROM pp0 JOIN np USING (PROMOTION_KEY) WHERE np.NP <= {PROMO_MAX_PRODUCTS}),
ii AS (SELECT ITEM, SITE, COUNT(*) AS N FROM ti GROUP BY 1, 2),
ia AS (SELECT ITEM, SUM(N) AS N FROM ii GROUP BY 1),
pk AS (SELECT pr.*, np.NP, np.N_HEAVY FROM pr JOIN np USING (PROMOTION_KEY) WHERE np.NP <= {PROMO_MAX_PRODUCTS})
SELECT 'PROMO' AS LVL, PROMOTION_KEY, PROMOTION_NAME, EVENT_NAME, PROMOTION_TYPE, DISCOUNT_TYPE, DISCOUNT_DEPTH, START_D, END_D,
       NP, NULL::STRING AS SITE, NULL::STRING AS ITEM, NULL::FLOAT AS W, N_HEAVY, 0 AS N_INJ_ITEMS, 0 AS INJ
FROM pk WHERE N_HEAVY > 0 OR PROMOTION_KEY IN (SELECT pp.PROMOTION_KEY FROM pp JOIN ia ON ia.ITEM = pp.ITEM)
UNION ALL
SELECT 'SITE', pk.PROMOTION_KEY, pk.PROMOTION_NAME, pk.EVENT_NAME, pk.PROMOTION_TYPE, pk.DISCOUNT_TYPE, pk.DISCOUNT_DEPTH,
       pk.START_D, pk.END_D, pk.NP, ii.SITE, NULL, NULL, 0, COUNT(DISTINCT ii.ITEM), SUM(ii.N)
FROM pp JOIN ii ON ii.ITEM = pp.ITEM JOIN pk ON pk.PROMOTION_KEY = pp.PROMOTION_KEY
GROUP BY 2, 3, 4, 5, 6, 7, 8, 9, 10, 11
UNION ALL
SELECT 'ITEM', pk.PROMOTION_KEY, pk.PROMOTION_NAME, pk.EVENT_NAME, pk.PROMOTION_TYPE, pk.DISCOUNT_TYPE, pk.DISCOUNT_DEPTH,
       pk.START_D, pk.END_D, pk.NP, NULL, pp.ITEM, pp.W, 0, 1, ia.N
FROM pp JOIN ia ON ia.ITEM = pp.ITEM JOIN pk ON pk.PROMOTION_KEY = pp.PROMOTION_KEY
UNION ALL
SELECT 'HEAVY', PROMOTION_KEY, PROMOTION_NAME, EVENT_NAME, PROMOTION_TYPE, DISCOUNT_TYPE, DISCOUNT_DEPTH, START_D, END_D, NP,
       NULL, ITEM, W, 1, 0, 0
FROM (SELECT pk.*, pp.ITEM, pp.W FROM pp JOIN pk ON pk.PROMOTION_KEY = pp.PROMOTION_KEY WHERE pp.W >= {HEAVY_LB}
      QUALIFY ROW_NUMBER() OVER (PARTITION BY pk.PROMOTION_KEY ORDER BY pp.W DESC, pp.ITEM) <= 10)
"""

X_QUERIES.update({"x_items": SQL_ITEMS, "x_promo": SQL_PROMO})
X_DATES = {"x_items": ("FIRST_WK", "LAST_WK"), "x_promo": ("START_D", "END_D")}
X_TEXT = ("ITEM", "DESCR", "CAT", "WBAND", "MECH", "LVL", "PROMOTION_NAME", "EVENT_NAME", "PROMOTION_TYPE", "DISCOUNT_TYPE")

ITEMS_TTL = 6 * 3600


@st.cache_resource(show_spinner=False)
def _items_state():
    """Shared across sessions: when the item and promotion queries last ran."""
    return {"at": None}


def items_warm():
    at = _items_state().get("at")
    return at is not None and (pd.Timestamp.now() - at).total_seconds() < ITEMS_TTL - 120


@gov.viewer_cache(st, lambda: g("_viewer_context")(), ttl=ITEMS_TTL, show_spinner=False)
def _items_cached(_rev=3):
    """Item-tagged injuries (with catalog weight and size) and promotions ahead. Cached 6 hours for everyone; a failed
    query raises, so an error is never cached."""
    it, e1 = load_x("x_items")
    if it is None:
        raise RuntimeError(f"item query: {e1}")
    pr, e2 = load_x("x_promo")
    if pr is None:
        raise RuntimeError(f"promotion query: {e2}")
    _items_state()["at"] = pd.Timestamp.now()
    return it, pr


def items_data():
    try:
        it, pr = _items_cached()
        return it, None, pr, None
    except Exception as exc:
        return None, str(exc)[:400], None, str(exc)[:400]


def _items_if_warm(force=False):
    if force or items_warm() or st.session_state.get("items_load"):
        return items_data()
    return None


def _dims(r):
    vals = [r.get("L"), r.get("WD"), r.get("H")]
    if any(v is None or not np.isfinite(v) or v <= 0 for v in vals):
        return "—"
    return " × ".join(f"{v:.0f}" for v in vals) + " in"


def item_table(it, sites, pr=None):
    """One row per item with injuries at `sites`: attributes, injury counts, sites, and promotions ahead."""
    if it is None or it.empty:
        return pd.DataFrame()
    d = it[(it["LVL"] == "ITEM") & it["SITE"].isin(sites)]
    if d.empty:
        return pd.DataFrame()
    t = d.groupby("ITEM").agg(DESCR=("DESCR", "first"), W=("W", "first"), L=("L", "first"), WD=("WD", "first"),
                              H=("H", "first"), CAT=("CAT", "first"), INJURIES=("INJURIES", "sum"), MSD=("MSD", "sum"),
                              RECORDABLES=("RECORDABLES", "sum"), LIFT=("LIFT", "sum"), STRUCK=("STRUCK", "sum"),
                              SITES=("SITE", lambda s: ", ".join(sorted(set(s)))), LAST_WK=("LAST_WK", "max")).reset_index()
    t["DIMS"] = [_dims(r) for r in t.to_dict("records")]
    t["HEAVY"] = t["W"].fillna(0) >= HEAVY_LB
    if pr is not None and len(pr):
        pi = pr[pr["LVL"] == "ITEM"]
        today = pd.Timestamp(g("_today_et")())
        up = pi[pi["END_D"] >= today].groupby("ITEM").agg(PROMOS=("PROMOTION_KEY", "nunique"), NEXT=("START_D", "min"),
                                                          PROMO_NAMES=("PROMOTION_NAME", lambda s: "; ".join(sorted(set(s))[:3])))
        t = t.merge(up, left_on="ITEM", right_index=True, how="left")
    for c in ("PROMOS", "NEXT", "PROMO_NAMES"):
        if c not in t.columns:
            t[c] = np.nan
    t["PROMOS"] = t["PROMOS"].fillna(0).astype(int)
    return t.sort_values(["INJURIES", "W"], ascending=[False, False]).reset_index(drop=True)


def weight_bands(it, sites, lo_wk, hi_wk):
    if it is None or it.empty:
        return pd.DataFrame()
    w = it[(it["LVL"] == "WEEK") & it["SITE"].isin(sites) & (it["FIRST_WK"] >= pd.Timestamp(lo_wk)) & (it["FIRST_WK"] <= pd.Timestamp(hi_wk))]
    if w.empty:
        return pd.DataFrame()
    t = w.groupby("WBAND")[["INJURIES", "MSD", "RECORDABLES", "LIFT"]].sum().reindex(WBANDS).fillna(0).reset_index()
    stated = t[t["WBAND"] != "Not stated"]["INJURIES"].sum()
    t["SHARE_STATED"] = np.where(t["WBAND"] != "Not stated", t["INJURIES"] / max(stated, 1) * 100, np.nan)
    t["LIFT_PCT"] = t["LIFT"] / t["INJURIES"].where(t["INJURIES"] > 0) * 100
    t["MSD_PCT"] = t["MSD"] / t["INJURIES"].where(t["INJURIES"] > 0) * 100
    return t


def promo_table(pr, it, sites):
    """Promotions ahead with their risk for `sites`: heavy items stocked here, items with injury history here."""
    if pr is None or pr.empty:
        return pd.DataFrame()
    today = pd.Timestamp(g("_today_et")())
    s = pr[(pr["LVL"] == "PROMO") & (pr["END_D"] >= today)]
    if s.empty:
        return pd.DataFrame()
    base = s.groupby("PROMOTION_KEY").agg(NAME=("PROMOTION_NAME", "first"), EVENT=("EVENT_NAME", "first"),
                                          TYPE=("PROMOTION_TYPE", "first"), DISC_TYPE=("DISCOUNT_TYPE", "first"),
                                          DEPTH=("DISCOUNT_DEPTH", "first"), START=("START_D", "first"), END=("END_D", "first"),
                                          NP=("NP", "first"), HEAVY_MAX=("N_HEAVY", "max"))
    ss = pr[(pr["LVL"] == "SITE") & pr["SITE"].isin(sites)]
    base["INJ"] = ss.groupby("PROMOTION_KEY")["INJ"].sum().reindex(base.index).fillna(0).astype(int)
    here = set()
    if it is not None and len(it):
        here = set(it[(it["LVL"] == "ITEM") & it["SITE"].isin(sites)]["ITEM"])
    pi = pr[(pr["LVL"] == "ITEM") & pr["ITEM"].isin(here)]
    base["INJ_ITEMS"] = pi.groupby("PROMOTION_KEY")["ITEM"].nunique().reindex(base.index).fillna(0).astype(int)
    base = base[((base["INJ_ITEMS"] > 0) | (base["HEAVY_MAX"] > 0)) & (base["NP"] >= PROMO_MIN_PRODUCTS)].reset_index()
    base["RISK"] = base["INJ"] + base["HEAVY_MAX"] / 10
    base["OFFER"] = [_offer(r) for r in base.to_dict("records")]
    base["EVENT"] = base["EVENT"].fillna("—").replace({"": "—", "None": "—"})
    base["WHEN"] = [f"{pd.Timestamp(a):%b %d} – {pd.Timestamp(b):%b %d}" for a, b in zip(base["START"], base["END"])]
    base["LIVE"] = base["START"] <= today
    base["ITEMS_HERE"] = [sorted(set(pi[pi["PROMOTION_KEY"] == k]["ITEM"])) for k in base["PROMOTION_KEY"]]
    return base.sort_values(["RISK", "START"], ascending=[False, True]).reset_index(drop=True)


def _offer(r):
    t = str(r.get("DISC_TYPE") or r.get("TYPE") or "")
    dep = r.get("DEPTH")
    if dep is not None and np.isfinite(dep) and dep > 0 and "percent" in t.lower():
        return f"{dep:.0f}% off"
    if dep is not None and np.isfinite(dep) and dep > 0 and "dollar" in t.lower():
        return f"${dep:.0f} off"
    nm = str(r.get("NAME") or "")
    m = re.search(r"\bB(\d)G(\d)\b", nm.upper())
    if m:
        return f"Buy {m.group(1)} get {m.group(2)}"
    return t or "—"


def promo_items(pr, it, sites, key):
    """Items in one promotion: the ones with injury history at `sites`, then its heaviest items."""
    if pr is None or pr.empty:
        return pd.DataFrame()
    tbl = item_table(it, sites)
    hist = tbl.set_index("ITEM") if len(tbl) else pd.DataFrame()
    rows = []
    for r in pr[(pr["PROMOTION_KEY"] == key) & (pr["LVL"] == "ITEM")].to_dict("records"):
        if len(hist) and r["ITEM"] in hist.index:
            h = hist.loc[r["ITEM"]]
            rows.append({"ITEM": r["ITEM"], "WHY": "Injury history here", "DESCR": h["DESCR"], "W": h["W"], "DIMS": h["DIMS"],
                         "INJURIES": h["INJURIES"], "SITES": h["SITES"]})
    for r in pr[(pr["PROMOTION_KEY"] == key) & (pr["LVL"] == "HEAVY")].to_dict("records"):
        if any(x["ITEM"] == r["ITEM"] for x in rows):
            continue
        rows.append({"ITEM": r["ITEM"], "WHY": f"Heavy ({HEAVY_LB}+ lb)", "DESCR": None, "W": r["W"], "DIMS": "—",
                     "INJURIES": 0, "SITES": ""})
    return pd.DataFrame(rows)


def promo_week_risk(pt):
    """Promotions running in each of the next 13 weeks that carry injury-prone or heavy items."""
    if pt is None or pt.empty:
        return pd.DataFrame()
    cur = _wk_of(g("_today_et")())
    rows = []
    for k in range(13):
        w0 = cur + pd.Timedelta(weeks=k)
        w1 = w0 + pd.Timedelta(days=6)
        on = pt[(pt["START"] <= w1) & (pt["END"] >= w0)]
        items = set(i for lst in on["ITEMS_HERE"] for i in lst)
        rows.append({"WK": w0, "PROMOS": len(on), "INJ_PROMOS": int((on["INJ_ITEMS"] > 0).sum()), "INJ_ITEMS": len(items),
                     "HEAVY_PROMOS": int((on["HEAVY_MAX"] > 0).sum())})
    return pd.DataFrame(rows)


def promo_note_lines(pr, it, sites, k=3):
    """Weekly note: promotions starting in the next 4 weeks with injury-prone or heavy items here."""
    pt = promo_table(pr, it, sites)
    if pt.empty:
        return []
    today = pd.Timestamp(g("_today_et")())
    soon = pt[(pt["START"] >= today - pd.Timedelta(days=6)) & (pt["START"] <= today + pd.Timedelta(weeks=4))]
    soon = soon.sort_values(["INJ_ITEMS", "HEAVY_MAX"], ascending=False).head(k)
    out = []
    for r in soon.to_dict("records"):
        bits = []
        if r["INJ_ITEMS"]:
            bits.append(f"{r['INJ_ITEMS']} item{'s' if r['INJ_ITEMS'] != 1 else ''} with injury history here")
        if r["HEAVY_MAX"]:
            bits.append(f"{int(r['HEAVY_MAX'])} items of {HEAVY_LB}+ lb")
        out.append(f"{r['NAME']} ({r['OFFER']}, {r['WHEN']}): {' and '.join(bits)}.")
    return out


def _xs_items(x):
    """Ops x Safety › Items & promotions."""
    gov.render_scope(st, _namespace(), "Whole weeks")
    BORDER, STRETCH, NC, TC = g("BORDER") or {}, g("STRETCH") or {}, st.column_config.NumberColumn, st.column_config.TextColumn
    pool = x["pool"]
    st.markdown("#### Items & promotions · products named in injury reports")
    st.caption("Report counts are not product risk rankings. Matching product handling/touch exposure is unavailable. Missing item and weight data can change the apparent pattern.")
    data = _items_if_warm()
    if data is None:
        st.caption("Reads item numbers written in injury reports (inside Snowflake), item weight and size from the product catalog, and the "
                   "promotion calendar. The first load of the day takes about a minute; after that it's cached for everyone "
                   "for 6 hours.")
        st.button("Load items & promotions", key="items_btn", type="primary",
                  on_click=lambda: st.session_state.update(items_load=True))
        return
    it, e1, pr, e2 = data
    if it is None:
        st.info(f"The item query couldn't run: {e1}")
        return
    lo = pd.Timestamp(g("start"))
    lo_wk, hi = _wk_of(lo), pd.Timestamp(g("end"))
    tbl = item_table(it, pool, pr)
    wb = weight_bands(it, pool, lo_wk, hi)
    all_inj = it[(it["LVL"] == "WEEK") & it["SITE"].isin(pool)]["INJURIES"].sum()
    k = g("kpi_cols")(4)
    k[0].metric("Injuries naming an item", f"{int(tbl['INJURIES'].sum()) if len(tbl) else 0:,}",
                f"{(tbl['INJURIES'].sum() if len(tbl) else 0) / max(all_inj, 1) * 100:.0f}% of injuries since Jun 2025",
                delta_color="off", help="The report text names an item number that exists in the product catalog.", **BORDER)
    k[1].metric("Items involved", f"{len(tbl):,}", **BORDER)
    hv = tbl[tbl["HEAVY"]] if len(tbl) else tbl
    k[2].metric(f"On items of {HEAVY_LB}+ lb", f"{int(hv['INJURIES'].sum()) if len(hv) else 0:,}",
                f"{(hv['INJURIES'].sum() if len(hv) else 0) / max(tbl['INJURIES'].sum() if len(tbl) else 1, 1) * 100:.0f}% of item injuries",
                delta_color="off", **BORDER)
    k[3].metric("Items named in injury reports on promotion ahead", f"{int((tbl['PROMOS'] > 0).sum()) if len(tbl) else 0:,}",
                help="Items with injury history at these sites that are in a promotion running now or starting in the next "
                     "13 weeks.", **BORDER)

    if len(wb):
        missing_weight = float(wb.loc[wb["WBAND"] == "Not stated", "INJURIES"].sum())
        total_weight = float(wb["INJURIES"].sum())
        st.caption(f"Weight not recorded: {missing_weight:,.0f} of {total_weight:,.0f} injuries ({missing_weight/total_weight:.1%})." if total_weight else "No injury weight observations.")
        a, b = g("pair_cols")(2)
        with a:
            st.markdown("**Injuries by the weight of the item involved** · sidebar range")
            ws = wb[wb["WBAND"] != "Not stated"]
            ch = alt.Chart(ws).mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4, color=(g("PALETTE") or ["#2a78d6"])[0]).encode(
                x=alt.X("WBAND:N", sort=WBANDS, title=None, axis=alt.Axis(labelAngle=0)),
                y=alt.Y("INJURIES:Q", title="Injuries"),
                tooltip=[alt.Tooltip("WBAND:N", title="Item weight"), alt.Tooltip("INJURIES:Q", title="Injuries"),
                         alt.Tooltip("MSD:Q", title="MSD"), alt.Tooltip("LIFT_PCT:Q", title="% lift or carry", format=".0f")])
            g("show_chart")(ch.properties(height=240))
            ns = int(wb.loc[wb["WBAND"] == "Not stated", "INJURIES"].sum())
            st.caption(f"Weight = the item's warehouse-packaged weight when the report names an item, else a weight written in the report. "
                       f"{ns:,} injuries in range state neither.")
        with b:
            st.markdown("**How those injuries happened** · share lifting or carrying, and MSD")
            m = ws.melt("WBAND", ["LIFT_PCT", "MSD_PCT"], var_name="K", value_name="V")
            m["K"] = m["K"].map({"LIFT_PCT": "Lifting or carrying", "MSD_PCT": "MSD"})
            pal = g("PALETTE") or ["#2a78d6", "#eb6834"]
            ch2 = alt.Chart(m).mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
                x=alt.X("WBAND:N", sort=WBANDS, title=None, axis=alt.Axis(labelAngle=0)), xOffset="K:N",
                y=alt.Y("V:Q", title="% of injuries in the band"),
                color=alt.Color("K:N", title=None, scale=alt.Scale(range=[pal[1], pal[2]]), legend=alt.Legend(orient="top")),
                tooltip=[alt.Tooltip("WBAND:N", title=""), alt.Tooltip("K:N", title=""), alt.Tooltip("V:Q", title="%", format=".0f")])
            g("show_chart")(ch2.properties(height=240))

    st.markdown("**Items named in injury reports** · injuries since Jun 2025 at the sites in scope, with item weight and size")
    if tbl.empty:
        st.caption("No injury report at these sites names an item number found in the product catalog.")
    else:
        show = tbl.head(60)[["ITEM", "DESCR", "CAT", "W", "DIMS", "INJURIES", "MSD", "RECORDABLES", "LIFT", "SITES",
                             "LAST_WK", "PROMOS", "NEXT", "PROMO_NAMES"]]
        st.dataframe(show, hide_index=True, **STRETCH, column_config={
            "ITEM": "Item", "DESCR": TC("Description", width="large"), "W": NC("Each weight (lb)", format="%.1f"),
            "DIMS": "L × W × H", "CAT": "Category", "INJURIES": NC("Injuries", format="%d"), "MSD": NC("MSD", format="%d"),
            "RECORDABLES": NC("Recordable", format="%d"), "LIFT": NC("Lift or carry", format="%d"), "SITES": "Sites",
            "LAST_WK": st.column_config.DateColumn("Last injury (week)", format="MMM DD, YYYY"),
            "PROMOS": NC("Promotions ahead", format="%d"),
            "NEXT": st.column_config.DateColumn("Next promo start", format="MMM DD"), "PROMO_NAMES": TC("Promotions", width="large")})
        st.download_button("Download items named in injury reports (CSV)", tbl.to_csv(index=False),
                           file_name="ovsr_injury_prone_items.csv", mime="text/csv", key="items_dl")
        st.caption("Item numbers come from the injury report text (Intelex has no item field filled in), matched to the product catalog so "
                   "only real items count. Weight and size are the warehouse-packaged unit in the Chewy product catalog. The text itself "
                   "never leaves Snowflake.")

    st.markdown("#### Promotions ahead on injury-prone or heavy items")
    if pr is None:
        st.info(f"The promotion query couldn't run: {e2}")
        return
    pt = promo_table(pr, it, pool)
    if pt.empty:
        st.caption("No promotion running now or starting in the next 13 weeks carries injury-prone or heavy items at these sites.")
        return
    wr = promo_week_risk(pt)
    if len(wr):
        st.markdown("**By week** · distinct items named in injury reports on promotion (items with injury history at these sites)")
        g("show_chart")(g("_wk_chart")(wr, {"INJ_ITEMS": "Items named in injury reports on promotion"}, "Items", ",.0f", kind="bar", height=220,
                                       colors=[(g("PALETTE") or ["", "#eb6834"])[1]]))
        pk = wr.loc[wr["INJ_ITEMS"].idxmax()]
        st.caption(f"Peak: week of {pk['WK']:%b %d}, {int(pk['INJ_ITEMS'])} items named in injury reports on promotion across "
                   f"{int(pk['INJ_PROMOS'])} promotions; {int(pk['HEAVY_PROMOS'])} promotions that week include {HEAVY_LB}+ lb items.")
    st.dataframe(pt[["NAME", "EVENT", "OFFER", "WHEN", "NP", "INJ_ITEMS", "INJ", "HEAVY_MAX"]], hide_index=True,
                 **STRETCH, column_config={
                     "NAME": TC("Promotion", width="large"), "EVENT": "Event", "OFFER": "Offer", "WHEN": "Runs",
                     "NP": NC("Products", format="%d"), "INJ_ITEMS": NC("Items named in injury reports", format="%d",
                                                                       help="Items in the promotion with injury history at these sites."),
                     "INJ": NC("Past injuries on them", format="%d"),
                     "HEAVY_MAX": NC(f"{HEAVY_LB}+ lb items", format="%d", help="Items in the promotion with a warehouse-packaged "
                                                                               f"weight of {HEAVY_LB} lb or more (product catalog).")})
    pick = st.selectbox("Items in a promotion", list(pt["PROMOTION_KEY"]), key="promo_pick",
                        format_func=lambda k_: f"{pt.loc[pt['PROMOTION_KEY'] == k_, 'NAME'].iloc[0]} · "
                                               f"{pt.loc[pt['PROMOTION_KEY'] == k_, 'WHEN'].iloc[0]}")
    pi = promo_items(pr, it, pool, pick)
    if len(pi):
        st.dataframe(pi, hide_index=True, **STRETCH, column_config={
            "ITEM": "Item", "WHY": "Why it's listed", "DESCR": TC("Description", width="large"),
            "W": NC("Each weight (lb)", format="%.1f"), "DIMS": "L × W × H", "INJURIES": NC("Injuries here", format="%d"),
            "SITES": "Sites with injuries"})
    st.caption(f"Promotions running now or starting within 13 weeks (Chewy promotion calendar), highest risk first (past injuries on "
               f"their items, then heavy items). Sitewide offers on more than {PROMO_MAX_PRODUCTS:,} products and personal codes on "
               f"fewer than {PROMO_MIN_PRODUCTS} are left out. Before a promotion starts: slot heavy and items named in injury reports at "
               "waist height, confirm team-lift and lift-assist coverage, and brief pick and pack on the items by name.")


def items_html(ctx, sites):
    """Snapshot: items named in injury reports and promotions ahead for this scope."""
    it, pr = ctx.get("items"), ctx.get("promos")
    if it is None:
        return ""
    fi = lambda v: fnum(v, ",.0f")   # noqa: E731
    tbl = item_table(it, sites, pr)
    s = ["<h2>Items named in injury reports and promotions ahead</h2>"]
    if tbl.empty:
        s.append("<p class='cap'>No injury report at these sites names an item number found in the product catalog.</p>")
    else:
        hv = tbl[tbl["HEAVY"]]
        s.append(f"<p class='cap'>{int(tbl['INJURIES'].sum()):,} injuries since Jun 2025 name one of {len(tbl):,} items; "
                 f"{int(hv['INJURIES'].sum()):,} involved items of {HEAVY_LB}+ lb. Weight and size from the product catalog.</p>")
        s.append(html_table(tbl.head(25), [("ITEM", "Item", None), ("DESCR", "Description", None),
                                           ("W", "Weight (lb)", lambda v: fnum(v, ".1f")), ("DIMS", "L × W × H", None),
                                           ("INJURIES", "Injuries", fi), ("MSD", "MSD", fi), ("LIFT", "Lift or carry", fi),
                                           ("SITES", "Sites", None), ("PROMOS", "Promotions ahead", fi)]))
    pt = promo_table(pr, it, sites) if pr is not None else pd.DataFrame()
    if len(pt):
        s.append("<h3>Promotions ahead carrying injury-prone or heavy items</h3>" + html_table(pt.head(20), [
            ("NAME", "Promotion", None), ("OFFER", "Offer", None), ("WHEN", "Runs", None), ("INJ_ITEMS", "Items named in injury reports", fi),
            ("INJ", "Past injuries on them", fi), ("HEAVY_MAX", f"{HEAVY_LB}+ lb items", fi)]))
    return "".join(s)


# ---------------------------------------------------------------------------------------------------------------
# Departments and process paths: injuries by path (Intelex event area) over the labor hours in that path (CLMS
# labor functions), the path-level ops metrics that move them, and the department staffing plan ahead.
# ---------------------------------------------------------------------------------------------------------------
PATHS = ["Multis & ground pick", "Pallet & case pick", "Autostore", "Singles & pack", "Induction & sort",
         "Outbound dock & loading", "Inbound dock & receiving", "Putaway", "Replenishment", "PIT & VNA",
         "Inventory control", "Other & support"]
PATH_DEPT = {"Multis & ground pick": "OB", "Pallet & case pick": "OB", "Autostore": "OB", "Singles & pack": "OB",
             "Induction & sort": "OB", "Outbound dock & loading": "OB", "Inbound dock & receiving": "IB", "Putaway": "IB",
             "PIT & VNA": "IB", "Replenishment": "REPLN", "Inventory control": "IC", "Other & support": None}
DEPT_NAME = {"OB": "Outbound", "IB": "Inbound", "REPLN": "Replenishment", "IC": "Inventory control"}
# Intelex event area -> path (first match on the lower-cased area wins)
AREA_RULES = [("autostore", "Autostore"), ("pick mod", "Multis & ground pick"), ("multis", "Multis & ground pick"),
              ("modsdrop", "Multis & ground pick"), ("mod (", "Multis & ground pick"), ("uom", "Multis & ground pick"),
              ("reserve", "Pallet & case pick"), ("fetch", "Pallet & case pick"), ("pallet pick", "Pallet & case pick"),
              ("singles", "Singles & pack"), ("tape", "Singles & pack"), ("dunnage", "Singles & pack"), ("pack", "Singles & pack"),
              ("induction", "Induction & sort"), ("hospital", "Induction & sort"), ("flap", "Induction & sort"),
              ("sort", "Induction & sort"), ("outbound dock", "Outbound dock & loading"), ("staging", "Outbound dock & loading"),
              ("inbound dock", "Inbound dock & receiving"), ("receiv", "Inbound dock & receiving"), ("vas", "Inbound dock & receiving"),
              ("putaway", "Putaway"), ("replen", "Replenishment"), ("vna", "PIT & VNA"), ("pit", "PIT & VNA"),
              ("battery", "PIT & VNA"), ("inventory", "Inventory control")]
_PATH_SQL = """CASE
    WHEN LABOR_FUNCTION ILIKE ANY ('lead', '%lead', 'unallocated', '%escalation%', '%training%', '%orientation%')
      THEN 'Other & support'
    WHEN LABOR_DEPARTMENT ILIKE 'replen%' THEN 'Replenishment'
    WHEN LABOR_DEPARTMENT ILIKE 'inventory%' THEN 'Inventory control'
    WHEN LABOR_FUNCTION ILIKE ANY ('%yard jockey%', '%triple transport%', '%tugger%') THEN 'PIT & VNA'
    WHEN LABOR_DEPARTMENT ILIKE 'inbound%' THEN IFF(LABOR_FUNCTION ILIKE '%putaway%', 'Putaway', 'Inbound dock & receiving')
    WHEN LABOR_DEPARTMENT ILIKE 'outbound%' THEN CASE
      WHEN LABOR_FUNCTION ILIKE '%autostore%' THEN 'Autostore'
      WHEN LABOR_FUNCTION ILIKE ANY ('%induct%', '%hospital%', '%flap%', 'bot %') THEN 'Induction & sort'
      WHEN LABOR_FUNCTION ILIKE ANY ('%truck load%', '%line load%', '%staging%', '%water spider%', '%down stack%',
                                     '%pallet wrangl%', '%trailer%') THEN 'Outbound dock & loading'
      WHEN LABOR_FUNCTION ILIKE ANY ('%pallet pick%', '%case pick%', '%fetch%', '%bulk pick%') THEN 'Pallet & case pick'
      WHEN LABOR_FUNCTION ILIKE ANY ('%pack%', '%tap%', '%box%', '%void%', '%label%', '%problem pk%', '%groom%')
        THEN 'Singles & pack'
      WHEN LABOR_FUNCTION ILIKE ANY ('%pick%', '%mods%', '%restock%') THEN 'Multis & ground pick'
      ELSE 'Other & support' END
    ELSE 'Other & support' END"""

SQL_PATHS = f"""
WITH d AS (
  SELECT WH_ID AS SITE, DATEADD(day, -(DAYOFWEEKISO(TIME_SEGMENT::DATE) % 7), TIME_SEGMENT::DATE)::DATE AS WK,
         {_PATH_SQL} AS PATH,
         COALESCE(IS_DIRECT, FALSE) AS DIR, EMPLOYEE_ID AS E, TIME_IN_SECONDS / 3600.0 AS H,
         COALESCE(UNITS, 0) AS U, COALESCE(TRANSACTIONS, 0) AS T
  FROM {X_SRC['clms']}
  WHERE TIME_SEGMENT >= '2025-05-01' AND TIME_IN_SECONDS > 0 AND WH_ID IS NOT NULL),
e AS (SELECT SITE, WK, PATH, E, SUM(H) AS H FROM d GROUP BY 1, 2, 3, 4),
f AS (SELECT SITE, PATH, E, MIN(WK) AS FIRST_WK FROM e GROUP BY 1, 2, 3),
agg AS (SELECT SITE, WK, PATH, SUM(H) AS HRS, SUM(IFF(DIR, H, 0)) AS DIRECT_HRS, SUM(U) AS UNITS, SUM(IFF(DIR, T, 0)) AS TRANS
        FROM d GROUP BY 1, 2, 3),
tm AS (SELECT e.SITE, e.WK, e.PATH, COUNT(*) AS TMS,
              COUNT_IF(f.FIRST_WK >= DATEADD(week, 4, (SELECT MIN(WK) FROM e)) AND e.WK < DATEADD(week, 4, f.FIRST_WK)) AS NEW_TMS,
              COUNT_IF(e.H >= 40) AS LONG_TMS
       FROM e JOIN f ON f.SITE = e.SITE AND f.PATH = e.PATH AND f.E = e.E
       GROUP BY 1, 2, 3)
SELECT agg.*, tm.TMS, tm.NEW_TMS, tm.LONG_TMS
FROM agg JOIN tm ON tm.SITE = agg.SITE AND tm.WK = agg.WK AND tm.PATH = agg.PATH
WHERE agg.WK >= '2025-06-01'
"""

# Department staffing plan by week: the plan in force for past weeks, the latest run for weeks ahead (same rule as MET)
SQL_DEPT_PLAN = f"""
WITH r AS (
  SELECT UPPER(TRIM(FC_NAME)) AS SITE, TRY_TO_DATE(MODEL_RUN_WEEK) AS RUN, RUN_ID, MAX(UPLOAD_DATETIME) AS UP
  FROM {X_SRC['met_plan']}
  WHERE TRY_TO_DATE(MODEL_RUN_WEEK) IS NOT NULL
  GROUP BY 1, 2, 3
  QUALIFY ROW_NUMBER() OVER (PARTITION BY SITE, RUN ORDER BY UP DESC, RUN_ID DESC) = 1),
d AS (
  SELECT r.SITE, r.RUN, TRY_TO_DATE(s.DATE) AS D, s.DEPARTMENT AS DEPT, s.REQUIRED_HOURS, s.SCHEDULED_HOURS, s.REGULAR_HOURS,
         s.OT, s.MET, s.VET, s.VTO, s.CAPACITY_GAP, s.LABOR_SHARE_IN, s.LABOR_SHARE_OUT, s.SHIP_PLAN, s.RECEIVE_UNITS,
         s.NEW_HIRES, s.SCHEDULED_HEADCOUNT
  FROM {X_SRC['met_plan']} s
  JOIN r ON r.SITE = UPPER(TRIM(s.FC_NAME)) AND r.RUN_ID = s.RUN_ID AND r.RUN = TRY_TO_DATE(s.MODEL_RUN_WEEK)
  WHERE TRY_TO_DATE(s.DATE) IS NOT NULL),
w AS (SELECT d.*, DATEADD(day, -(DAYOFWEEKISO(D) % 7), D)::DATE AS WK FROM d),
k AS (SELECT * FROM w WHERE RUN <= WK OR WK >= (SELECT MAX(RUN) FROM r)
      QUALIFY DENSE_RANK() OVER (PARTITION BY SITE, D ORDER BY RUN DESC) = 1)
SELECT SITE, WK, DEPT, MAX(RUN) AS RUN, COUNT(DISTINCT D) AS DAYS, SUM(REQUIRED_HOURS) AS REQ_H, SUM(SCHEDULED_HOURS) AS SCH_H,
       SUM(REGULAR_HOURS) AS REG_H, SUM(OT) AS OT_H, SUM(MET) AS MET_H, SUM(VET) AS VET_H, SUM(VTO) AS VTO_H,
       SUM(CAPACITY_GAP) AS GAP_H, SUM(LABOR_SHARE_IN) AS LS_IN, SUM(LABOR_SHARE_OUT) AS LS_OUT, SUM(SHIP_PLAN) AS SHIP_PLAN,
       SUM(RECEIVE_UNITS) AS RECV_PLAN, SUM(NEW_HIRES) AS HIRES, AVG(SCHEDULED_HEADCOUNT) AS HC
FROM k GROUP BY 1, 2, 3
"""
# Injuries placed in the path the injured TM was clocked into (CLMS, same site, that day or the day before for night
# shifts; most hours, direct paths ahead of lead/training time). Unmatched injuries keep their Intelex event area and
# fall back to the area rules. Matched inside Snowflake; only counts leave.
SQL_PATH_INJ = f"""
WITH i AS (
  SELECT ROW_NUMBER() OVER (ORDER BY EVENT_NO, INCIDENT_NO, EVENT_DATE) AS RID,
         COALESCE(REGEXP_SUBSTR(UPPER(LOCATION), '[A-Z]{{3}}[0-9]'), TRIM(LOCATION)) AS SITE,
         EVENT_DATE::DATE AS D,
         DATEADD(day, -(DAYOFWEEKISO(EVENT_DATE::DATE) % 7), EVENT_DATE::DATE)::DATE AS WK,
         LOWER(TRIM(INJURED_TM_USERNAME)) AS U,
         COALESCE(NULLIF(TRIM(EVENT_AREA), ''), 'Not recorded') AS AREA,
         {_MECH_SQL} AS MECH,
         {_B('MSD_INJURY')} AS IS_MSD,
         ({_B('OSHA_RECORDABLE')} OR INJURY_CLASSIFICATION ILIKE ANY ('recordable%', 'lost time%')) AS IS_REC
  FROM (SELECT *, {_TXT_SQL} AS TXT
        FROM {X_SRC['inc']}
        WHERE INCIDENT_TYPE ILIKE 'injury%' AND EVENT_DATE >= '2025-07-20'
          AND COALESCE(INJURY_CLASSIFICATION, '') NOT ILIKE 'non-occupational%')),
c AS (
  SELECT WH_ID AS SITE, TIME_SEGMENT::DATE AS D, LOWER(TRIM(EMPLOYEE_ID)) AS U, {_PATH_SQL} AS PATH,
         SUM(TIME_IN_SECONDS) AS S
  FROM {X_SRC['clms']}
  WHERE TIME_SEGMENT >= '2025-07-01' AND TIME_IN_SECONDS > 0
    AND LOWER(TRIM(EMPLOYEE_ID)) IN (SELECT U FROM i WHERE U IS NOT NULL)
  GROUP BY 1, 2, 3, 4),
f AS (SELECT SITE, U, PATH, MIN(D) AS FIRST_D FROM c GROUP BY 1, 2, 3),
m AS (
  SELECT i.*, c.PATH AS CPATH, f.FIRST_D
  FROM i
  LEFT JOIN c ON c.SITE = i.SITE AND c.U = i.U AND c.D BETWEEN i.D - 1 AND i.D
  LEFT JOIN f ON f.SITE = c.SITE AND f.U = c.U AND f.PATH = c.PATH
  QUALIFY ROW_NUMBER() OVER (PARTITION BY i.RID ORDER BY IFF(c.D = i.D, 0, 1), IFF(c.PATH = 'Other & support', 1, 0),
                                                        c.S DESC NULLS LAST) = 1)
SELECT SITE, WK, CPATH AS PATH, IFF(CPATH IS NULL, AREA, NULL) AS AREA,
       COUNT(*) AS INJURIES, COUNT_IF(IS_MSD) AS MSD, COUNT_IF(IS_REC) AS RECORDABLES,
       COUNT_IF(MECH IN ('Lifting / lowering', 'Carrying')) AS LIFT,
       COUNT_IF(FIRST_D >= (SELECT MIN(D) FROM c) + 28 AND DATEDIFF(day, FIRST_D, D) < 28) AS NEW_IN_PATH,
       COUNT_IF(CPATH IS NOT NULL) AS MATCHED
FROM m
GROUP BY 1, 2, 3, 4
"""
X_QUERIES.update({"x_paths": SQL_PATHS, "x_dept_plan": SQL_DEPT_PLAN, "x_path_inj": SQL_PATH_INJ})
X_TEXT = X_TEXT + ("PATH",)

PATH_DRIVERS = {   # key: (label, unit, format, note)
    "PATH_LOAD": ("Path hours vs its normal", "×", ".2f", "Hours in the path ÷ the path's average over its prior 13 weeks at the site."),
    "UPH_PUSH": ("Path rate push", "%", "+.1f", "Units per direct hour in the path vs its prior 13-week average."),
    "H_PER_TM": ("Hours per TM in the path", "h", ".1f", "Path hours ÷ TMs who worked the path that week."),
    "NEW_SHARE": ("New to the path", "%", ".1f", "TMs in their first 4 weeks in the path ÷ TMs in the path (CLMS history starts "
                                                 "late Jul 2025, so the first 4 weeks of data count no one as new)."),
    "LONG_SHARE": ("40+ h in the path", "%", ".1f", "TMs with 40 or more hours in the path that week ÷ TMs in the path."),
    "PRESSURE": ("Workload pressure (site)", "pts", "+.1f", "Site volume % over plan minus hours % over plan."),
    "OT_PCT": ("Overtime share (site)", "%", ".1f", "Site UKG overtime ÷ regular hours."),
}
PATH_OUTCOMES = {"INJ": "Injuries in the path", "MSD": "MSD in the path", "LIFT": "Lift & carry in the path"}


def area_path(a):
    a = str(a or "").lower()
    for k, v in AREA_RULES:
        if k in a:
            return v
    return "Other & support"


@st.cache_resource(show_spinner=False)
def _paths_state():
    """Shared across sessions: when the path panel was last built."""
    return {"at": None}


def paths_warm():
    at = _paths_state().get("at")
    return at is not None and (pd.Timestamp.now() - at).total_seconds() < ITEMS_TTL - 120


def paths_if_warm(basis, force=False):
    if force or paths_warm() or st.session_state.get("paths_load"):
        return path_panel(basis)
    return None


PATH_INJ_COLS = ["INJ", "MSD", "LIFT", "INJ_NEW", "MATCHED"]


def _path_injuries():
    """(site x week x path injury counts, source). Injuries go to the path the injured TM was clocked into (CLMS); the
    few that don't match fall back to their Intelex event area. Falls back to event areas for all if the match fails."""
    pi, _ = load_x("x_path_inj")
    if pi is not None and len(pi):
        pi = pi.copy()
        pi["PATH"] = [p_ if isinstance(p_, str) and p_ else area_path(a_) for p_, a_ in zip(pi["PATH"], pi["AREA"])]
        pi = pi.rename(columns={"INJURIES": "INJ", "NEW_IN_PATH": "INJ_NEW"})
        return pi.groupby(["SITE", "WK", "PATH"], as_index=False)[PATH_INJ_COLS].sum(), "clms"
    inj, _ = load_x("x_inj")
    if inj is not None and len(inj) and "AREA" in inj.columns:
        ii = inj.assign(PATH=inj["AREA"].map(area_path), INJ_NEW=0, MATCHED=0)
        ii["LIFT"] = ii["INJURIES"].where(ii["MECH"].isin(MECH_GROUPS["INJ_LIFT"]), 0) if "MECH" in ii else 0
        ii = ii.rename(columns={"INJURIES": "INJ"})
        return ii.groupby(["SITE", "WK", "PATH"], as_index=False)[PATH_INJ_COLS].sum(), "area"
    return pd.DataFrame(columns=["SITE", "WK", "PATH"] + PATH_INJ_COLS), "none"


@gov.viewer_cache(st, lambda: g("_viewer_context")(), ttl=ITEMS_TTL, show_spinner=False)
def path_panel(basis, _rev=3):
    """One row per site x full week x path: CLMS hours, units and crew, injuries tied to the path, path drivers."""
    pth, e = load_x("x_paths")
    if pth is None or pth.empty:
        return pd.DataFrame(), e
    cur = _wk_of(g("_today_et")())
    pth = pth[pth["WK"] < cur].copy()
    ii, src = _path_injuries()
    if len(ii):
        pth = pth.merge(ii, on=["SITE", "WK", "PATH"], how="left")
    for c in PATH_INJ_COLS:
        pth[c] = pth[c].fillna(0) if c in pth else 0
    pth["INJ_SRC"] = src
    pth = pth.sort_values(["SITE", "PATH", "WK"]).reset_index(drop=True)
    grp = pth.groupby(["SITE", "PATH"])
    div = lambda a, b: a / b.where(b > 0)   # noqa: E731
    pth["UPH"] = div(pth["UNITS"], pth["DIRECT_HRS"])
    pth["PATH_LOAD"] = pth["HRS"] / grp["HRS"].transform(_prior_mean)
    pth["UPH_PUSH"] = (pth["UPH"] / grp["UPH"].transform(_prior_mean) - 1) * 100
    # A path that triples or drops to a third of its normal in a week is a process or labor-function change (e.g. pallet
    # and case pick functions split out of ground pick in Mar 2026), not a workload swing: leave those weeks out
    brk = (pth["PATH_LOAD"] > 3) | (pth["PATH_LOAD"] < 1 / 3)
    pth.loc[brk, ["PATH_LOAD", "UPH_PUSH"]] = np.nan
    pth["H_PER_TM"] = div(pth["HRS"], pth["TMS"])
    pth["NEW_SHARE"] = div(pth["NEW_TMS"], pth["TMS"]) * 100
    pth["LONG_SHARE"] = div(pth["LONG_TMS"], pth["TMS"]) * 100
    try:
        p_all, _, _ = panel_cached(basis, False)
        pth = pth.merge(p_all[["SITE", "WK", "PRESSURE", "OT_PCT"]], on=["SITE", "WK"], how="left")
    except Exception:
        pth["PRESSURE"], pth["OT_PCT"] = np.nan, np.nan
    _paths_state()["at"] = pd.Timestamp.now()
    return pth[pth["HRS"] >= 100].reset_index(drop=True), None


def poisson_fe2(y, log_h, x, g1, g2, iters=50, tol=1e-9):
    r = gov.poisson_fit(y, log_h, x, g1, g2, week_fixed=True, min_n=LEDGER_MIN_N)
    return r["b"], r["se"], r["p"], r["phi"]


def path_ledger(pp, min_n=LEDGER_MIN_N):
    """Per path: Poisson regression of path injuries on each driver (offset log path hours, site and week fixed
    effects), per 1 SD of the driver's within-site swing; Benjamini-Hochberg q across every test."""
    rows = []
    for path, d in pp.groupby("PATH"):
        for oc in PATH_OUTCOMES:
            y = d[oc].to_numpy(float)
            hrs = d["HRS"].to_numpy(float)
            for k in PATH_DRIVERS:
                if k not in d:
                    continue
                x = d[k].to_numpy(float)
                m = np.isfinite(x) & np.isfinite(y) & (hrs > 0)
                row = {"PATH": path, "OUTCOME": oc, "DRIVER": k, "N": int(m.sum()), "EVENTS": float(y[m].sum()) if m.any() else 0,
                       "IRR": np.nan, "IRR_LO": np.nan, "IRR_HI": np.nan, "P": np.nan}
                if m.sum() >= min_n and y[m].sum() >= 10:
                    s = d.loc[m, "SITE"].astype(str).to_numpy()
                    w = d.loc[m, "WK"].astype(str).to_numpy()
                    xs = x[m]
                    dm = xs - pd.Series(xs).groupby(s).transform("mean").to_numpy() if len(set(s)) > 1 else xs - xs.mean()
                    sd = float(np.std(dm))
                    if sd > 0:
                        fit = gov.poisson_fit(y[m], np.log(hrs[m]), (xs-xs.mean())/sd, s, w, week_fixed=True, min_n=min_n)
                        b, se, pv, crit = fit["b"], fit["se"], fit["p"], fit["crit"]
                        row.update(QUASI_SE=fit["quasi_se"], CLUSTER_SE=se, CLUSTERS=fit["clusters"], CONVERGED=fit["converged"], METHOD=fit["method"])
                        if np.isfinite(b) and np.isfinite(se) and se < 3 and abs(b) < 5:
                            row.update({"IRR": math.exp(b), "IRR_LO": math.exp(np.clip(b-crit*se, -30, 30)), "IRR_HI": math.exp(np.clip(b+crit*se, -30, 30)), "P": pv})
                rows.append(row)
    led = pd.DataFrame(rows)
    if led.empty:
        return led
    led["Q"] = bh_qvalues(led["P"])
    led["VERDICT"] = np.select([~np.isfinite(led["P"]), led["Q"] < ALPHA, led["P"] < ALPHA],
                               ["Too little data", "Exploratory after correction", "Suggestive"], "Not supported")
    return led


@gov.viewer_cache(st, lambda: g("_viewer_context")(), ttl=3600, show_spinner=False)
def path_ledger_cached(basis, sites, _rev=3):
    pp, _ = path_panel(basis)
    if pp.empty:
        return pd.DataFrame()
    return path_ledger(pp[pp["SITE"].isin(sites)])


def path_table(pp, sites, lo_wk, hi_wk, end_full, basis):
    """One row per path for the range, plus the last 4 full weeks vs the 13 before."""
    d = pp[pp["SITE"].isin(sites)]
    r = d[(d["WK"] >= pd.Timestamp(lo_wk)) & (d["WK"] <= pd.Timestamp(hi_wk))]
    if r.empty:
        return pd.DataFrame()
    t = r.groupby("PATH").agg(INJ=("INJ", "sum"), MSD=("MSD", "sum"), LIFT=("LIFT", "sum"), HRS=("HRS", "sum"),
                              DIRECT_HRS=("DIRECT_HRS", "sum"), UNITS=("UNITS", "sum"), TMS=("TMS", "sum"), NEW=("NEW_TMS", "sum"),
                              LONG=("LONG_TMS", "sum"), INJ_NEW=("INJ_NEW", "sum"), MATCHED=("MATCHED", "sum"))
    t["NEW_INJ_SHARE"] = t["INJ_NEW"] / t["MATCHED"].where(t["MATCHED"] >= 5) * 100
    t["RATE"] = t["INJ"] * basis / t["HRS"].where(t["HRS"] > 0)
    t["UPH"] = t["UNITS"] / t["DIRECT_HRS"].where(t["DIRECT_HRS"] > 0)
    t["H_PER_TM"] = t["HRS"] / t["TMS"].where(t["TMS"] > 0)
    t["NEW_SHARE"] = t["NEW"] / t["TMS"].where(t["TMS"] > 0) * 100
    t["LONG_SHARE"] = t["LONG"] / t["TMS"].where(t["TMS"] > 0) * 100
    t["HRS_SHARE"] = t["HRS"] / t["HRS"].sum() * 100
    t["INJ_SHARE"] = t["INJ"] / max(t["INJ"].sum(), 1) * 100
    l4 = [pd.Timestamp(end_full) - pd.Timedelta(weeks=k) for k in range(4)]
    p13 = [pd.Timestamp(end_full) - pd.Timedelta(weeks=k) for k in range(4, 17)]
    a = d[d["WK"].isin(l4)].groupby("PATH")[["INJ", "HRS"]].sum()
    b = d[d["WK"].isin(p13)].groupby("PATH")[["INJ", "HRS"]].sum()
    t["R4"] = (a["INJ"] * basis / a["HRS"].where(a["HRS"] > 0)).reindex(t.index)
    t["R13"] = (b["INJ"] * basis / b["HRS"].where(b["HRS"] > 0)).reindex(t.index)
    t["CHG"] = (t["R4"] / t["R13"].where(t["R13"] > 0) - 1) * 100
    t["DEPT"] = [DEPT_NAME.get(PATH_DEPT.get(p_), "—") for p_ in t.index]
    t = t.reindex([p_ for p_ in PATHS if p_ in t.index]).reset_index()
    return t


def dept_outlook(plan, pp, sites, basis, weeks=8):
    """Department plan ahead with projected injuries: each department's injury rate over its last 13 full weeks of
    CLMS hours, applied to the planned scheduled hours."""
    if plan is None or plan.empty:
        return pd.DataFrame()
    cur = _wk_of(g("_today_et")())
    allp = plan[plan["SITE"].isin(sites)]
    pl = allp[allp["WK"] >= cur]
    if pl.empty:
        return pd.DataFrame()
    past = allp[(allp["WK"] < cur) & (allp["WK"] >= cur - pd.Timedelta(weeks=4))].groupby(["WK", "DEPT"])["SCH_H"].sum()
    plan_recent = past.groupby(level="DEPT").mean() if len(past) else pd.Series(dtype=float)
    pl = pl.groupby(["WK", "DEPT"], as_index=False)[["REQ_H", "SCH_H", "REG_H", "OT_H", "MET_H", "VET_H", "VTO_H", "GAP_H",
                                                      "SHIP_PLAN", "RECV_PLAN", "HIRES"]].sum()
    pl = pl[pl["WK"] < cur + pd.Timedelta(weeks=weeks)]
    rates, recent = {}, pd.Series(dtype=float)
    if pp is not None and len(pp):
        d = pp[pp["SITE"].isin(sites)].assign(DEPT=lambda f: f["PATH"].map(PATH_DEPT))
        full = sorted(d["WK"].unique())
        p13, l4 = full[-13:], full[-4:]
        for dep, g_ in d[d["WK"].isin(p13)].groupby("DEPT"):
            h = g_["HRS"].sum()
            rates[dep] = g_["INJ"].sum() / h if h > 0 else np.nan
        recent = d[d["WK"].isin(l4)].groupby("DEPT")["HRS"].sum() / max(len(l4), 1)
    pl["RATE_H"] = pl["DEPT"].map(rates)
    # Plan hours and CLMS hours can count different things: compare the plan with its own recent weeks, and scale the
    # projection by CLMS hours ÷ plan hours over those weeks (1.0 when the plan has no history yet)
    pl["RECENT_H"] = pl["DEPT"].map(plan_recent) if len(plan_recent) else np.nan
    calib = {d_: float(np.clip(recent.get(d_, np.nan) / plan_recent.get(d_, np.nan), 0.2, 5))
             if np.isfinite(recent.get(d_, np.nan) / plan_recent.get(d_, np.nan)) else 1.0 for d_ in pl["DEPT"].unique()} \
        if len(plan_recent) and len(recent) else {}
    pl["CALIB"] = pl["DEPT"].map(calib).fillna(1.0) if calib else 1.0
    pl["EXP_INJ"] = pl["SCH_H"] * pl["CALIB"] * pl["RATE_H"]
    pl["VS_RECENT"] = (pl["SCH_H"] / pl["RECENT_H"].where(pl["RECENT_H"] > 0) - 1) * 100
    pl["OT_SHARE"] = (pl["OT_H"] + pl["MET_H"]) / pl["REG_H"].where(pl["REG_H"] > 0) * 100
    pl["DEPT_NAME"] = pl["DEPT"].map(DEPT_NAME).fillna(pl["DEPT"])
    return pl.sort_values(["WK", "DEPT"]).reset_index(drop=True)


@gov.viewer_cache(st, lambda: g("_viewer_context")(), ttl=3600, show_spinner=False)
def sop_ahead(pool, cur_wk, _rev=1):
    """(S&OP plan ahead per site and week scaled to shipped units, hours elasticity) with the same rules as the peak
    outlook, for the path and department projections."""
    pool, cur_wk = list(pool), pd.Timestamp(cur_wk)
    hol, _, sop, _ = g("load_plan")()
    ow = g("ops_weeks")(hol)
    eps = 0.6
    try:
        hol_w = g("holiday_weeks")(hol) if hol is not None else pd.DataFrame(columns=["SITE", "WK", "UNITS", "DAYS_U"])
        eps_est, _ = g("hours_elasticity")(g("site_weeks")(_daily_fact()), hol_w)
        eps = round(float(np.clip(eps_est, 0.2, 1.2)), 2) if pd.notna(eps_est) else 0.6
    except Exception:
        pass
    return sop_plan_rows(sop, ow, pool, cur_wk), eps


def path_outlook(pp, plan, eps, sites, basis, weeks=8):
    """Paths ahead at the S&OP plan: each site's path hours over its last 4 full weeks, scaled by the site's planned
    shipped units vs its last 4 weeks (to the power of the hours elasticity), times each path's injury rate per CLMS hour
    over the last 13 full weeks (pooled across the sites in scope). One row per week x path."""
    if pp is None or pp.empty or plan is None or plan.empty:
        return pd.DataFrame()
    cur = _wk_of(g("_today_et")())
    d = pp[pp["SITE"].isin(sites)]
    full = sorted(d["WK"].unique())
    if len(full) < 13:
        return pd.DataFrame()
    l4, p13 = full[-4:], full[-13:]
    h4 = (d[d["WK"].isin(l4)].groupby(["SITE", "PATH"])["HRS"].sum() / len(l4)).rename("H4").reset_index()
    i4 = (d[d["WK"].isin(l4)].groupby(["SITE", "PATH"])["INJ"].sum() / len(l4)).rename("I4").reset_index()
    r13 = d[d["WK"].isin(p13)].groupby("PATH")[["INJ", "HRS"]].sum()
    rate = (r13["INJ"] / r13["HRS"].where(r13["HRS"] > 0)).rename("RATE_H")
    pl = plan[plan["SITE"].isin(sites) & (plan["WK"] >= cur) & (plan["WK"] < cur + pd.Timedelta(weeks=weeks))].copy()
    if pl.empty:
        return pd.DataFrame()
    pl["RATIO"] = (pl["UNITS"] / pl["RECENT"].where(pl["RECENT"] > 0)).clip(0.3, 4.0) ** float(eps)
    x = h4.merge(i4, on=["SITE", "PATH"], how="left").merge(pl[["SITE", "WK", "RATIO"]], on="SITE")
    x = x[np.isfinite(x["RATIO"])]
    if x.empty:
        return pd.DataFrame()
    x["PH"] = x["H4"] * x["RATIO"]
    x["EXP_INJ"] = x["PH"] * x["PATH"].map(rate)
    o = x.groupby(["WK", "PATH"], as_index=False).agg(PH=("PH", "sum"), H4=("H4", "sum"), I4=("I4", "sum"), EXP_INJ=("EXP_INJ", "sum"),
                                                      SITES=("SITE", "nunique"))
    o["VS_RECENT"] = (o["PH"] / o["H4"].where(o["H4"] > 0) - 1) * 100
    o["RATE"] = o["PATH"].map(rate) * basis
    o["DEPT_NAME"] = [DEPT_NAME.get(PATH_DEPT.get(p_), "Support") for p_ in o["PATH"]]
    return o.sort_values(["WK", "PATH"]).reset_index(drop=True)


def path_sensitivity(po):
    if po is None or po.empty:
        return
    with st.expander("Path mix and rate sensitivity"):
        low=po.groupby("PATH")["EXP_INJ"].sum().rename("Base expected").reset_index()
        shift=st.slider("Path exposure variation (%)",0,40,10,5,key="path_exposure_sensitivity")/100
        rshift=st.slider("Path baseline-rate variation (%)",0,50,20,5,key="path_rate_sensitivity")/100
        low["Low scenario"]=low["Base expected"]*(1-shift)*(1-rshift)
        low["High scenario"]=low["Base expected"]*(1+shift)*(1+rshift)
        st.dataframe(low,hide_index=True,**g("STRETCH"))
        st.caption("Illustrative per-path exposure and rate assumptions. These are not probability bounds or a validated mix forecast.")

def path_outlook_table(po):
    """Per path over the outlook window: hours now, the busiest week, projected injuries vs the last 4 weeks' pace."""
    if po is None or po.empty:
        return pd.DataFrame()
    n = po["WK"].nunique()
    t = po.groupby("PATH").agg(H4=("H4", "first"), I4=("I4", "first"), EXP_INJ=("EXP_INJ", "sum"), PEAK_VS=("VS_RECENT", "max"),
                               RATE=("RATE", "first"), DEPT_NAME=("DEPT_NAME", "first"))
    t["PEAK_WK"] = po.loc[po.groupby("PATH")["PH"].idxmax()].set_index("PATH")["WK"]
    t["AT_PACE"] = t["I4"] * n
    t["VS_PACE"] = t["EXP_INJ"] - t["AT_PACE"]
    t = t.reindex([p_ for p_ in PATHS if p_ in t.index]).reset_index()
    return t


def _path_heat(led, outcome):
    t = led[(led["OUTCOME"] == outcome)].copy()
    if t.empty:
        return None
    t["EFF"] = (t["IRR"] - 1) * 100
    t["MARK"] = np.select([t["VERDICT"] == "Exploratory after correction", t["VERDICT"] == "Suggestive"], ["●", "○"], "")
    t["TXT"] = [f"{e:+.0f}%{m}" if np.isfinite(e) else "" for e, m in zip(t["EFF"], t["MARK"])]
    t["DLAB"] = t["DRIVER"].map(lambda k: PATH_DRIVERS[k][0])
    rows = [p_ for p_ in PATHS if p_ in set(t["PATH"])]
    cols = [PATH_DRIVERS[k][0] for k in PATH_DRIVERS]
    base = alt.Chart(t).encode(
        x=alt.X("DLAB:N", sort=cols, title=None, axis=alt.Axis(labelAngle=-30, labelLimit=150, ticks=False, domain=False,
                                                              labelOverlap=False)),
        y=alt.Y("PATH:N", sort=rows, title=None, axis=alt.Axis(ticks=False, domain=False, labelLimit=200, labelOverlap=False)))
    heat = base.mark_rect(cornerRadius=2).encode(
        color=alt.Color("EFF:Q", title="% change per typical rise",
                        scale=alt.Scale(domain=[-30, 0, 30], range=["#2a78d6", "#f2f1ee", "#e34948"], clamp=True),
                        legend=alt.Legend(orient="bottom")),
        tooltip=[alt.Tooltip("PATH:N", title="Path"), alt.Tooltip("DLAB:N", title="Ops metric"),
                 alt.Tooltip("EFF:Q", title="% change in the rate", format="+.0f"), alt.Tooltip("VERDICT:N", title="Evidence"),
                 alt.Tooltip("Q:Q", title="q", format=".3f"), alt.Tooltip("N:Q", title="Site-weeks"), alt.Tooltip("EVENTS:Q", title="Injuries")])
    txt = base.mark_text(fontSize=11).encode(text="TXT:N")
    return (heat + txt).properties(height=alt.Step(26))


def _xs_paths(x):
    """Ops x Safety › Departments & paths."""
    gov.render_scope(st, _namespace(), "Whole weeks")
    BORDER, STRETCH, NC, TC = g("BORDER") or {}, g("STRETCH") or {}, st.column_config.NumberColumn, st.column_config.TextColumn
    pool, basis = x["pool"], g("basis")
    st.markdown("#### Departments & paths · candidate labor-path attribution")
    st.caption("A labor-path match is a proxy: same-day or prior-day CLMS, direct path preferred, then most hours. It does not confirm the path at the injury time. Event-time accuracy has not been measured.")
    res = paths_if_warm(basis)
    if res is None:
        st.caption("Groups CLMS labor hours and Intelex injuries into 12 process paths, tests which path-level ops metrics move "
                   "injuries, and adds the department staffing plan ahead. The first load of the day takes a minute or two; "
                   "after that it's cached for everyone for 6 hours.")
        st.button("Load departments & paths", key="paths_btn", type="primary",
                  on_click=lambda: st.session_state.update(paths_load=True))
        return
    pp, err = res
    if pp.empty:
        st.info(f"Path hours couldn't be built: {err}")
        return
    lo, hi = pd.Timestamp(g("start")), pd.Timestamp(g("end"))
    # Latest full Sunday week that ends on or before the range end (a mid-week end date would match no week key).
    end_full = min(_wk_of(g("_today_et")()), _wk_of(hi + pd.Timedelta(days=1))) - pd.Timedelta(weeks=1)
    t = path_table(pp, pool, _wk_of(lo), hi, end_full, basis)
    if t.empty:
        st.caption("No path hours for the sidebar filters.")
        return
    st.markdown("**Injury rate by process path** · sidebar range")
    st.dataframe(t[["PATH", "DEPT", "INJ", "INJ_SHARE", "HRS_SHARE", "MSD", "LIFT", "RATE", "R4", "R13", "CHG", "UPH", "H_PER_TM",
                    "NEW_SHARE", "NEW_INJ_SHARE", "LONG_SHARE"]], hide_index=True, **STRETCH, column_config={
        "PATH": "Path", "DEPT": "Dept", "INJ": NC("Injuries", format="%d"), "INJ_SHARE": NC("% of injuries", format="%.0f%%"),
        "HRS_SHARE": NC("% of hours", format="%.0f%%"), "MSD": NC("MSD", format="%d"), "LIFT": NC("Lift or carry", format="%d"),
        "RATE": NC("Rate", format="%.2f", help=f"Injuries per {basis:,} CLMS hours in the path."),
        "R4": NC("Last 4 wks", format="%.2f"), "R13": NC("13 wks before", format="%.2f"), "CHG": NC("Change", format="%+.0f%%"),
        "UPH": NC("UPH", format="%.0f", help="Units per direct hour in the path."), "H_PER_TM": NC("Hours per TM", format="%.1f"),
        "NEW_SHARE": NC("TMs new to path", format="%.0f%%", help=PATH_DRIVERS["NEW_SHARE"][3]),
        "NEW_INJ_SHARE": NC("Injuries new to path", format="%.0f%%",
                            help="Injured TMs who were in their first 4 weeks in that path (CLMS) ÷ injuries matched to CLMS. "
                                 "Well above 'TMs new to path' means new people in the path get hurt out of proportion."),
        "LONG_SHARE": NC("40+ h in path", format="%.0f%%", help=PATH_DRIVERS["LONG_SHARE"][3])})
    hot = t[(t["INJ_SHARE"] > t["HRS_SHARE"] * 1.25) & (t["INJ"] >= 5)].sort_values("RATE", ascending=False)
    if len(hot):
        st.markdown("**Over-represented:** " + "; ".join(f"{r['PATH']} has {r['INJ_SHARE']:.0f}% of injuries on {r['HRS_SHARE']:.0f}% "
                                                         f"of the hours" for r in hot.head(3).to_dict("records")) + ".")
    newh = t[(t["NEW_INJ_SHARE"] >= t["NEW_SHARE"] + 10) & (t["MATCHED"] >= 5)].sort_values("NEW_INJ_SHARE", ascending=False)
    if len(newh):
        st.markdown("**New to the path:** " + "; ".join(
            f"{r['PATH']}: {r['NEW_INJ_SHARE']:.0f}% of injuries were TMs in their first 4 weeks in the path, while new TMs "
            f"are {r['NEW_SHARE']:.0f}% of the crew" for r in newh.head(3).to_dict("records")) + ".")
    d_sc = pp[pp["SITE"].isin(pool) & (pp["WK"] >= _wk_of(lo)) & (pp["WK"] <= hi)]
    src = str(pp["INJ_SRC"].iloc[0]) if "INJ_SRC" in pp and len(pp) else "area"
    if src == "clms":
        mr = d_sc["MATCHED"].sum() / max(d_sc["INJ"].sum(), 1) * 100
        st.caption(f"Each injury is placed in the path the injured TM was clocked into in CLMS that day ({mr:.0f}% matched; the "
                   "rest by Intelex event area), and rated over the CLMS hours in the same path. Matching happens inside "
                   "Snowflake: only counts leave. Leads, training and admin time sit in Other & support.")
    else:
        st.caption("Injuries come from the Intelex event area and hours from CLMS labor functions, grouped into the same 12 "
                   "paths (the CLMS match for injured TMs couldn't be read). Leads, training and admin time sit in Other & support.")
    with st.expander("How paths are grouped"):
        st.markdown("- **CLMS labor functions → paths:** Replenishment and Inventory Control by department; yard jockey, triple "
                    "transport and tugger as PIT & VNA; inbound putaway functions as Putaway, the rest of inbound as dock & "
                    "receiving; outbound split by function name (autostore; induction, hospital, flap; truck and line load, "
                    "staging, water spider; pallet, case, fetch and bulk pick; pack, tape, box, label; then other picking and "
                    "mods). Leads, escalation, training, orientation and unallocated time go to Other & support.\n"
                    "- **Injury → path:** the injured TM's CLMS record at that site on the day of the injury (or the day before, "
                    "for night shifts): the path with the most hours, with direct paths ahead of lead or training time.\n"
                    "- **No CLMS match:** the Intelex event area decides (" + "; ".join(
                        f"{p_}: " + ", ".join(f"'{a_}'" for a_, v_ in AREA_RULES if v_ == p_) for p_ in PATHS
                        if any(v_ == p_ for _, v_ in AREA_RULES)) + "; anything else: Other & support).")
        st.caption("Pallet & case pick hours jump in March 2026 (new or renamed CLMS functions) while ground pick hours drop. "
                   "Weeks where a path's hours triple or fall to a third of its normal are left out of the path tests.")

    led = path_ledger_cached(basis, tuple(sorted(pp[pp["SITE"].isin(pool)]["SITE"].unique())))
    if led is not None and len(led):
        st.markdown("**Which ops metrics move injuries in each path** · % change in the path's injury rate per typical "
                    "week-to-week rise in the metric")
        lab = g("seg")("Outcome", list(PATH_OUTCOMES.values()), PATH_OUTCOMES["INJ"], key="path_oc")
        oc = {v: k for k, v in PATH_OUTCOMES.items()}[lab]
        ch = _path_heat(led, oc)
        if ch is not None:
            g("show_chart")(ch)
        n_h = int((led["VERDICT"] == "Exploratory after correction").sum())
        n_t = int(np.isfinite(led["P"]).sum())
        top = led[(led["OUTCOME"] == oc) & led["VERDICT"].isin(["Exploratory after correction", "Suggestive"])].copy()
        top["_A"] = np.abs(np.log(top["IRR"]))
        lines = []
        for r in top.sort_values(["Q", "_A"], ascending=[True, False]).head(5).to_dict("records"):
            lines.append(f"- **{r['PATH']} · {PATH_DRIVERS[r['DRIVER']][0]}**: {(r['IRR'] - 1) * 100:+.0f}% "
                         f"(95% {(r['IRR_LO'] - 1) * 100:+.0f}% to {(r['IRR_HI'] - 1) * 100:+.0f}%) per typical rise · "
                         f"{r['VERDICT'].lower()}")
        if lines:
            st.markdown("\n".join(lines))
        st.caption(f"{n_t} tests ({len(PATH_DRIVERS)} metrics × {len(PATH_OUTCOMES)} outcomes × paths with 30+ site-weeks and 10+ "
                   f"injuries); {n_h} hold after the Benjamini–Hochberg correction. ● holds, ○ suggestive. Poisson regression "
                   "with each path's hours as exposure, site fixed effects and week fixed effects: season, peak hiring and "
                   "network-wide changes are taken out, so each metric is judged on a site running above or below its own normal "
                   "while the other sites didn't. Evidence, not cause.")

        # One path and one metric by week
        c1, c2 = st.columns(2)
        pth = c1.selectbox("Path", [p_ for p_ in PATHS if p_ in set(t["PATH"])], key="path_pick")
        drv = c2.selectbox("Ops metric", list(PATH_DRIVERS), key="path_drv", format_func=lambda k: PATH_DRIVERS[k][0])
        d = pp[pp["SITE"].isin(pool) & (pp["PATH"] == pth)]
        wk = d.groupby("WK").agg(INJ=("INJ", "sum"), HRS=("HRS", "sum")).sort_index()
        num = d.assign(_W=d[drv] * d["HRS"]).groupby("WK")["_W"].sum()
        wk["DRV"] = (num / wk["HRS"]).reindex(wk.index)
        wk["RATE4"] = wk["INJ"].rolling(4, min_periods=4).sum() * basis / wk["HRS"].rolling(4, min_periods=4).sum()
        wk = wk.tail(52).reset_index()
        if len(wk) >= 8:
            for c in ("RATE4", "DRV"):
                s_ = wk[c]
                wk[c + "_Z"] = (s_ - s_.mean()) / s_.std() if s_.std() and s_.std() > 0 else np.nan
            ch2 = g("_wk_chart")(wk, {"DRV_Z": PATH_DRIVERS[drv][0], "RATE4_Z": "Injury rate (4-week rolling)"},
                                 "Distance from normal (SD)", "+.1f", height=240, colors=list(PAIR_COLORS))
            g("show_chart")(ch2)
            r_ = wk[["DRV", "RATE4"]].corr().iloc[0, 1] if wk[["DRV", "RATE4"]].dropna().shape[0] >= 8 else np.nan
            st.caption(f"{pth} at the sites in scope, last {len(wk)} weeks. Both lines are shown as distance from their own "
                       f"average (one axis). r = {r_:+.2f} between the weekly metric and the 4-week rolling rate." if np.isfinite(r_) else
                       f"{pth} at the sites in scope, last {len(wk)} weeks, shown as distance from each line's own average.")

    # Paths and departments ahead
    st.markdown("#### Paths ahead · projected hours and injuries at the S&OP plan")
    try:
        sp, eps = sop_ahead(tuple(pool), _wk_of(g("_today_et")()))
    except Exception:
        sp, eps = pd.DataFrame(), 0.6
    po = path_outlook(pp, sp, eps, pool, basis)
    if po.empty:
        st.caption("No S&OP plan ahead for these sites.")
    else:
        lvl = g("seg")("Show", ["Department", "Path"], "Department", key="path_ol_lvl")
        col = "DEPT_NAME" if lvl == "Department" else "PATH"
        po2 = po
        if col == "PATH":   # seven named paths at most, the rest folded into one gray series
            top = set(po.groupby("PATH")["EXP_INJ"].sum().nlargest(7).index)
            po2 = po.assign(PATH=po["PATH"].where(po["PATH"].isin(top), "Other paths"))
        wk_ = po2.groupby(["WK", col], as_index=False).agg(EXP_INJ=("EXP_INJ", "sum"), PH=("PH", "sum"), H4=("H4", "sum"))
        wk_["VS_RECENT"] = (wk_["PH"] / wk_["H4"].where(wk_["H4"] > 0) - 1) * 100
        dom = [d_ for d_ in list(DEPT_NAME.values()) + ["Support"] if d_ in set(wk_[col])] if col == "DEPT_NAME" else \
            [p_ for p_ in PATHS if p_ in set(wk_["PATH"])] + (["Other paths"] if "Other paths" in set(wk_["PATH"]) else [])
        ch3 = alt.Chart(wk_).mark_bar(cornerRadiusTopLeft=2, cornerRadiusTopRight=2).encode(
            x=alt.X("yearmonthdate(WK):O", title=None, axis=alt.Axis(format="%m/%d", labelAngle=0)),
            y=alt.Y("EXP_INJ:Q", title="Projected injuries", stack="zero"),
            color=alt.Color(f"{col}:N", title=None, sort=dom, scale=alt.Scale(domain=dom, range=_path_colors(dom)),
                            legend=alt.Legend(orient="top", columns=4 if col == "PATH" else 5)),
            order=alt.Order("EXP_INJ:Q", sort="descending"),
            tooltip=[alt.Tooltip("WK:T", title="Week of", format="%b %d"), alt.Tooltip(f"{col}:N", title=lvl),
                     alt.Tooltip("PH:Q", title="Projected CLMS hours", format=",.0f"),
                     alt.Tooltip("VS_RECENT:Q", title="Hours vs last 4 wks %", format="+.0f"),
                     alt.Tooltip("EXP_INJ:Q", title="Projected injuries", format=".1f")])
        g("show_chart")(ch3.properties(height=260))
        path_sensitivity(po)
        tt = path_outlook_table(po)
        st.dataframe(tt[["PATH", "DEPT_NAME", "H4", "PEAK_VS", "PEAK_WK", "RATE", "AT_PACE", "EXP_INJ", "VS_PACE"]], hide_index=True,
                     **STRETCH, column_config={
                         "PATH": "Path", "DEPT_NAME": "Dept", "H4": NC("Hours / week now", format="%d", help="Average of the last 4 full weeks."),
                         "PEAK_VS": NC("Busiest week vs now", format="%+.0f%%"),
                         "PEAK_WK": st.column_config.DateColumn("Busiest week", format="MMM DD"),
                         "RATE": NC("Rate used", format="%.2f", help=f"Injuries per {basis:,} CLMS hours in the path, last 13 full weeks."),
                         "AT_PACE": NC("At the last 4 weeks' pace", format="%.0f", help="Injuries per week over the last 4 full weeks × weeks ahead."),
                         "EXP_INJ": NC("Projected injuries", format="%.0f"),
                         "VS_PACE": NC("Extra vs pace", format="%+.0f")})
        st.caption(f"Next {po['WK'].nunique()} weeks, {int(po['SITES'].max())} of {len(pool)} sites with an S&OP plan. Each site's "
                   f"path hours over its last 4 full weeks are scaled by its planned shipped units vs its last 4 weeks (hours "
                   f"elasticity {eps:.2f}), then multiplied by the path's injury rate over the last 13 weeks. The path mix is held at "
                   "today's; peak shifts hours toward pick, pack and loading, and new hires and extra time usually push rates up, so "
                   "read the projection as a baseline scenario that can overpredict or underpredict.")

    plan, e2 = load_x("x_dept_plan")
    if plan is None:
        st.caption("The department staffing plan (scheduled, required, OT and MET hours by department) isn't readable by the app's "
                   "role yet; a read grant on EDLDB.SC_ORDER_ROUTING_TEAM_SANDBOX.STAFF_DAILY_DEPARTMENT for SNOP_USER adds it here "
                   "and MET to the drivers.")
        return
    do = dept_outlook(plan, pp, pool, basis)
    if do.empty:
        return
    st.markdown("#### Departments ahead · staffing plan")
    show = do.groupby("DEPT_NAME", as_index=False).agg(SCH_H=("SCH_H", "sum"), REQ_H=("REQ_H", "sum"), OT_H=("OT_H", "sum"),
                                                        MET_H=("MET_H", "sum"), GAP_H=("GAP_H", "sum"), EXP_INJ=("EXP_INJ", "sum"),
                                                        PEAK_VS=("VS_RECENT", "max"), HIRES=("HIRES", "sum"))
    pk = do.loc[do.groupby("DEPT_NAME")["EXP_INJ"].idxmax()][["DEPT_NAME", "WK"]].rename(columns={"WK": "PEAK_WK"})
    show = show.merge(pk, on="DEPT_NAME", how="left")
    st.dataframe(show[["DEPT_NAME", "SCH_H", "REQ_H", "OT_H", "MET_H", "GAP_H", "HIRES", "PEAK_VS", "PEAK_WK", "EXP_INJ"]], hide_index=True,
                 **STRETCH, column_config={
                     "DEPT_NAME": "Department", "SCH_H": NC("Scheduled hours", format="%d"), "REQ_H": NC("Required hours", format="%d"),
                     "OT_H": NC("OT hours", format="%d"), "MET_H": NC("MET hours", format="%d"),
                     "GAP_H": NC("Capacity gap h", format="%d", help="Staffing plan capacity gap (negative = short)."),
                     "HIRES": NC("Planned hires", format="%d"), "PEAK_VS": NC("Peak week vs last 4", format="%+.0f%%"),
                     "PEAK_WK": st.column_config.DateColumn("Highest week", format="MMM DD"),
                     "EXP_INJ": NC("Projected injuries", format="%.0f", help="Each department's injury rate per CLMS hour over its "
                                                                           "last 13 full weeks × planned scheduled hours × "
                                                                           "calibration (CLMS hours ÷ plan hours, last 4 weeks).")})
    st.caption(f"Next {do['WK'].nunique()} weeks from the latest network staffing-plan run; 'vs last 4' compares with the plan's "
               "own last 4 weeks. Projected injuries assume each department keeps its last 13 weeks' rate per CLMS hour (plan hours "
               "scaled to CLMS hours).")


def _path_colors(dom):
    base = list(g("PALETTE") or SNAP_PALETTE)
    named = [d_ for d_ in dom if d_ not in ("Other paths", "Support")]
    cols = dict(zip(named, base))
    return [cols.get(d_, "#9a9a9a") for d_ in dom]


def path_note_lines(pp, plan, sites, wk, basis, sop_plan=None, eps=0.6):
    """Weekly note: the path that ran hottest that week, new-to-path injuries, the path with the most projected injuries
    ahead at the S&OP plan, and (when readable) the department with the biggest planned step-up."""
    out = []
    if pp is not None and len(pp):
        t = path_table(pp, sites, wk, wk, wk, basis)
        if len(t):
            hot = t[(t["INJ"] >= 2)].sort_values("RATE", ascending=False)
            if len(hot):
                r = hot.iloc[0]
                out.append(f"Hottest path that week: {r['PATH']} with {int(r['INJ'])} injuries ({r['INJ_SHARE']:.0f}% of injuries on "
                           f"{r['HRS_SHARE']:.0f}% of hours)" + (f"; last 4 weeks' rate {r['CHG']:+.0f}% vs the 13 before" if np.isfinite(r["CHG"]) else "") + ".")
        lw = pd.Timestamp(wk)
        t4 = path_table(pp, sites, lw - pd.Timedelta(weeks=3), lw, lw, basis)
        if len(t4):
            nh = t4[(t4["NEW_INJ_SHARE"] >= t4["NEW_SHARE"] + 10) & (t4["MATCHED"] >= 5)].sort_values("NEW_INJ_SHARE", ascending=False)
            if len(nh):
                r = nh.iloc[0]
                out.append(f"New to the path: {r['NEW_INJ_SHARE']:.0f}% of {r['PATH']} injuries over the last 4 weeks were TMs in their "
                           f"first 4 weeks in the path (new TMs are {r['NEW_SHARE']:.0f}% of that crew). Pair them with a trained "
                           "partner and keep their first weeks at learning-curve rates.")
        if sop_plan is not None and len(sop_plan):
            po = path_outlook(pp, sop_plan, eps, sites, basis, weeks=4)
            tt = path_outlook_table(po)
            if len(tt):
                r = tt.sort_values("EXP_INJ", ascending=False).iloc[0]
                out.append(f"Path to staff carefully: {r['PATH']}, about {r['EXP_INJ']:.0f} projected injuries over the next "
                           f"{po['WK'].nunique()} weeks at the S&OP plan (busiest week {_d(r['PEAK_WK'])}, hours {r['PEAK_VS']:+.0f}% "
                           "vs the last 4 weeks).")
    if plan is not None and len(plan):
        do = dept_outlook(plan, pp, sites, basis, weeks=4)
        if len(do):
            r = do.sort_values("VS_RECENT", ascending=False).iloc[0]
            if np.isfinite(r["VS_RECENT"]) and r["VS_RECENT"] >= 10:
                out.append(f"Biggest step-up ahead: {r['DEPT_NAME']} scheduled {r['VS_RECENT']:+.0f}% vs its last 4 weeks in the week of "
                           f"{_d(r['WK'])}" + (f", with {r['MET_H']:,.0f} MET hours" if r["MET_H"] > 0 else "") + ".")
    return out


def paths_html(ctx, sites):
    """Snapshot: injury rate by process path and the paths ahead at the S&OP plan."""
    pp, plan = ctx.get("paths"), ctx.get("dept_plan")
    if pp is None or pp.empty:
        return ""
    basis, rng = ctx["basis"], ctx["rng"]
    t = path_table(pp, sites, rng[0], rng[-1], ctx["lw"], basis)
    if t.empty:
        return ""
    f0, f2, fp = (lambda v: fnum(v, ",.0f")), (lambda v: fnum(v, ".2f")), (lambda v: fnum(v, ".0f", "%"))
    src = "the path the injured TM was clocked into in CLMS that day" if str(pp["INJ_SRC"].iloc[0]) == "clms" else "Intelex event area"
    s = [f"<h2>Process paths · {_wk(rng[0])} – {_wk(rng[-1] + pd.Timedelta(days=6), '%b %d, %Y')}</h2>"
         f"<p class='cap'>Injuries placed by {src}, over CLMS hours in the same path. Last 4 weeks and the 13 before end the "
         f"week of {_wk(ctx['lw'])}.</p>"]
    s.append(html_table(t, [("PATH", "Path", None), ("DEPT", "Dept", None), ("INJ", "Injuries", f0), ("INJ_SHARE", "% of injuries", fp),
                            ("HRS_SHARE", "% of hours", fp), ("MSD", "MSD", f0), ("RATE", "Rate", f2), ("R4", "Last 4 wks", f2),
                            ("R13", "13 wks before", f2), ("CHG", "Change", lambda v: fnum(v, "+.0f", "%")),
                            ("NEW_SHARE", "TMs new to path", fp), ("NEW_INJ_SHARE", "Injuries new to path", fp),
                            ("LONG_SHARE", "40+ h in path", fp)]))
    sp = ctx.get("plan")
    po = path_outlook(pp, sp, ctx.get("eps", 0.6), sites, basis) if sp is not None else pd.DataFrame()
    tt = path_outlook_table(po)
    if len(tt):
        s.append(f"<h3>Paths ahead · next {po['WK'].nunique()} weeks at the S&OP plan</h3>" + html_table(tt, [
            ("PATH", "Path", None), ("DEPT_NAME", "Dept", None), ("H4", "Hours / week now", f0),
            ("PEAK_VS", "Busiest week vs now", lambda v: fnum(v, "+.0f", "%")), ("RATE", "Rate used", f2),
            ("AT_PACE", "At last 4 weeks' pace", f0), ("EXP_INJ", "Projected injuries", f0),
            ("VS_PACE", "Extra vs pace", lambda v: fnum(v, "+.0f"))])
            + "<p class='cap'>Each site's path hours over its last 4 full weeks scaled by its planned shipped units vs its last 4 weeks, "
            "times the path's injury rate over the last 13 weeks. Recent path mix is held fixed. This baseline scenario can be above or below actual outcomes.</p>")
    do = dept_outlook(plan, pp, sites, basis) if plan is not None else pd.DataFrame()
    if len(do):
        sh = do.groupby("DEPT_NAME", as_index=False).agg(SCH_H=("SCH_H", "sum"), OT_H=("OT_H", "sum"), MET_H=("MET_H", "sum"),
                                                          GAP_H=("GAP_H", "sum"), EXP_INJ=("EXP_INJ", "sum"), PEAK_VS=("VS_RECENT", "max"))
        s.append(f"<h3>Departments ahead · next {do['WK'].nunique()} weeks of the staffing plan</h3>" + html_table(sh, [
            ("DEPT_NAME", "Department", None), ("SCH_H", "Scheduled hours", f0), ("OT_H", "OT hours", f0), ("MET_H", "MET hours", f0),
            ("GAP_H", "Capacity gap h", f0), ("PEAK_VS", "Peak week vs last 4", lambda v: fnum(v, "+.0f", "%")),
            ("EXP_INJ", "Projected injuries", f0)]) + "<p class='cap'>Projected injuries = each department's injury rate per CLMS "
            "hour over its last 13 full weeks × planned scheduled hours × calibration (CLMS hours ÷ plan hours, last 4 weeks).</p>")
    return "".join(s)


# ---------------------------------------------------------------------------------------------------------------
# First aid -> recordable: when in-house (first aid) cases become recordable, and which open first aid cases are
# likely to. A case "becomes recordable" when Intelex's date of recordability is 2+ days after the injury (prior first-aid status is not established by these dates). Time runs in weeks from day 2 after the injury; iCare follow-ups up to
# the start of each week (how many, how long in house, pain trend, improving or not) predict whether the case becomes
# recordable that week (a discrete-time hazard model: ridge logistic regression on case-weeks).
# Case rows carry the Incident # and categorical fields only: no names, usernames, notes or narratives.
# ---------------------------------------------------------------------------------------------------------------
X_SRC.update({"icare": "EDLDB.SNOP_SANDBOX.T_EHS_ICARE_DETAILS"})
FA_START_DAY = 2          # day 0-1 is outside the delayed-recordability proxy cohort
FA_KMAX = 15              # weekly periods 0..15 = days 2-113 after the injury
FA_HORIZON = FA_START_DAY + 7 * (FA_KMAX + 1) - 1
FA_HIGH = 0.25            # high risk: 25%+ chance of becoming recordable by day 113
FA_HIGH_PAIN = 6          # pain 6+ of 10 counts as a high-pain follow-up
FA_LAMBDA = 1.0           # ridge penalty (standardized features)
FA_TTL = 3600

_FA_INC = "COALESCE(TO_VARCHAR(TRY_TO_NUMBER(TRIM({c}::STRING))), TRIM({c}::STRING))"
_FA_CASES_CTE = f"""
b AS (
  SELECT *, {_TXT_SQL} AS TXT, {_FA_INC.format(c='INCIDENT_NO')} AS INC,
         TRY_TO_DATE(DATE_OF_RECORDABILITY::STRING) AS DOR_D
  FROM {X_SRC['inc']}
  WHERE INCIDENT_TYPE ILIKE 'injury%' AND EVENT_DATE >= '2025-06-01'
    AND COALESCE(INJURY_CLASSIFICATION, '') NOT ILIKE 'non-occupational%'
    AND NULLIF(TRIM(INCIDENT_NO::STRING), '') IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY INC ORDER BY LAST_REFRESHED_CT DESC NULLS LAST, EVENT_DATE, INJURY_CLASSIFICATION, DATE_OF_RECORDABILITY, CURRENT_STAGE, TYPE_OF_INJURY, CAUSE_OF_INJURY, LOCATION) = 1),
fa AS (
  SELECT INC, COALESCE(REGEXP_SUBSTR(UPPER(LOCATION), '[A-Z]{{3}}[0-9]'), TRIM(LOCATION)) AS SITE,
         EVENT_DATE::DATE AS ED, DOR_D AS DOR, TO_DATE(TRY_TO_TIMESTAMP(DATE_TIME_CLOSED_TS::STRING)) AS CLOSED_D,
         COALESCE(NULLIF(TRIM(CURRENT_STAGE), ''), 'Not recorded') AS STAGE,
         IFF(INJURY_CLASSIFICATION ILIKE ANY ('recordable%', 'lost time%'), 1, 0) AS IS_REC,
         CASE WHEN TYPE_OF_INJURY ILIKE 'strain%' THEN 'Strain / sprain'
              WHEN TYPE_OF_INJURY ILIKE 'contusion%' THEN 'Contusion'
              WHEN TYPE_OF_INJURY ILIKE ANY ('laceration%', 'scrape%', 'puncture%') THEN 'Cut, scrape or puncture'
              WHEN TYPE_OF_INJURY ILIKE ANY ('inflammation%', 'irritation%') THEN 'Inflammation or irritation'
              WHEN TYPE_OF_INJURY ILIKE ANY ('crush%', 'fracture%', 'concussion%') THEN 'Crush, fracture or concussion'
              ELSE 'Other' END AS INJ_TYPE,
         {_MECH_SQL} AS MECH,
         CASE WHEN TRY_TO_BOOLEAN(MSD_INJURY::STRING) THEN 1 WHEN TRY_TO_BOOLEAN(MSD_INJURY::STRING) = FALSE THEN 0 END AS IS_MSD,
         CASE WHEN DATE_HIRED IS NULL OR DATE_HIRED > EVENT_DATE THEN NULL WHEN DATEDIFF(day, DATE_HIRED, EVENT_DATE) < 90 THEN 1 ELSE 0 END AS NEW_HIRE,
         CASE WHEN WORKING_OT_HOURS ILIKE ANY ('%MET%', '%VET%') THEN 1 WHEN WORKING_OT_HOURS ILIKE 'no%' THEN 0 END AS EXTRA_TIME
  FROM b
  WHERE INJURY_CLASSIFICATION ILIKE 'first aid%'
     OR (INJURY_CLASSIFICATION ILIKE ANY ('recordable%', 'lost time%') AND DATEDIFF(day, EVENT_DATE::DATE, DOR_D) >= 2))"""

# One row per delayed-recordability proxy case or current first-aid case (first aid now, or recordable 2+ days after the injury).
SQL_FA_CASES = f"""
WITH {_FA_CASES_CTE.strip()}
SELECT * FROM fa"""

# One row per iCare follow-up on those cases: date, pain 0-10 and improving yes/no. No notes, names or usernames.
SQL_FA_CONTACTS = f"""
WITH {_FA_CASES_CTE.strip()},
ic AS (
  SELECT {_FA_INC.format(c='INCIDENT__X0023_')} AS INC, TRIM(ICARE_RECORD__X0023_) AS CONTACT_KEY, ICARE_DATE::DATE AS ICARE_D,
         TRY_TO_NUMBER(PAIN_LEVEL_NUMERIC::STRING) AS PAIN,
         CASE WHEN UPPER(IS_YOUR_PAIN_LEVEL_IMPROVING::STRING) IN ('TRUE', 'YES', 'Y', '1') THEN 1
              WHEN UPPER(IS_YOUR_PAIN_LEVEL_IMPROVING::STRING) IN ('FALSE', 'NO', 'N', '0') THEN 0 END AS IMPROVING
  FROM {X_SRC['icare']}
  WHERE HAS_ICARE_RECORD AND ICARE_DATE IS NOT NULL AND NULLIF(TRIM(ICARE_RECORD__X0023_), '') IS NOT NULL
    AND ICARE_DATE >= '2025-06-01'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY INC, TRIM(ICARE_RECORD__X0023_)
                             ORDER BY INTELEX_UPDATED DESC NULLS LAST, LAST_REFRESHED_CT DESC NULLS LAST, ID DESC) = 1)
SELECT ic.* FROM ic WHERE ic.INC IN (SELECT INC FROM fa)"""

X_QUERIES.update({"x_fa_cases": SQL_FA_CASES, "x_fa_contacts": SQL_FA_CONTACTS})
X_DATES.update({"x_fa_cases": ("ED", "DOR", "CLOSED_D"), "x_fa_contacts": ("ICARE_D",)})
X_TEXT = X_TEXT + ("INC", "CONTACT_KEY", "STAGE", "INJ_TYPE")

FA_MECH_GROUP = {"Lifting / lowering": "Lift & carry", "Carrying": "Lift & carry",
                 "Overreaching": "Reach, repetition & posture", "Repetitive motion": "Reach, repetition & posture",
                 "Bending / twisting / posture": "Reach, repetition & posture", "Pushing / pulling": "Pushing / pulling",
                 "Struck by / against": "Struck by or caught", "Caught in / between": "Struck by or caught",
                 "Slip, trip or fall": "Slip, trip or fall"}
FA_TYPES = ["Contusion", "Cut, scrape or puncture", "Inflammation or irritation", "Crush, fracture or concussion", "Other"]
FA_MECHS = ["Lift & carry", "Reach, repetition & posture", "Pushing / pulling", "Struck by or caught", "Slip, trip or fall"]
FA_CONT = ["LOG_N", "INHOUSE_WK", "GAP_WK", "PAIN_LAST", "PAIN_UP", "PAIN_DOWN", "NOT_SHARE", "HIGH_N"]
FA_BIN = ["NO_ICARE", "PAIN_NA", "NOT_LAST", "CLOSED", "IS_MSD", "NEW_HIRE", "EXTRA_TIME"]
FA_LABELS = {"LOG_N": "iCare follow-ups so far", "INHOUSE_WK": "Weeks since first iCare follow-up",
             "GAP_WK": "Weeks since the last follow-up", "PAIN_LAST": "Latest pain level",
             "PAIN_UP": "Pain up since the first follow-up", "PAIN_DOWN": "Pain down since the first follow-up",
             "NOT_SHARE": "Share of follow-ups 'not improving'", "HIGH_N": "High-pain follow-ups (6+ of 10)",
             "NO_ICARE": "No iCare follow-up yet", "PAIN_NA": "No pain level recorded",
             "NOT_LAST": "Latest follow-up 'not improving'", "CLOSED": "Case closed in Intelex",
             "IS_MSD": "MSD injury", "NEW_HIRE": "Hired under 90 days before", "EXTRA_TIME": "Working MET or VET"}
FA_LABELS.update({f"T_{t}": f"Injury type: {t.lower()}" for t in FA_TYPES})
FA_LABELS.update({f"M_{m}": f"Mechanism: {m.lower()}" for m in FA_MECHS})


def _fa_week_label(k):
    a = FA_START_DAY + 7 * int(k)
    return f"Wk {int(k) + 1} (days {a}–{a + 6})"


def _fa_load():
    c, e1 = load_x("x_fa_cases")
    if c is None:
        raise RuntimeError(f"first aid case query: {e1}")
    k, e2 = load_x("x_fa_contacts")
    if k is None:
        raise RuntimeError(f"iCare follow-up query: {e2}")
    c = c.dropna(subset=["INC", "ED"]).drop_duplicates("INC").reset_index(drop=True)
    for col in ("IS_REC",):
        c[col] = pd.to_numeric(c[col], errors="coerce").fillna(0).astype(int)
    c.loc[c["IS_REC"].eq(1) & c["DOR"].isna(), "IS_REC"] = -1          # recordable with no date: can't be placed in time
    c = c[c["IS_REC"] >= 0].reset_index(drop=True)
    k = k.dropna(subset=["INC", "ICARE_D"]).copy()
    k["PAIN"] = pd.to_numeric(k["PAIN"], errors="coerce").clip(0, 10)
    k["IMPROVING"] = pd.to_numeric(k["IMPROVING"], errors="coerce")
    return c, k.sort_values(["INC", "ICARE_D"]).reset_index(drop=True)


def _fa_periods(c, today):
    """Case-weeks: one row per case per week at risk (first aid at the start of that week). Y = became recordable
    that week. Weeks still in progress for open cases are left out (their outcome isn't known yet)."""
    c = c.copy()
    c["AGE"] = (today - c["ED"]).dt.days
    c["FLIP"] = np.where(c["IS_REC"].eq(1), (c["DOR"] - c["ED"]).dt.days, np.nan)
    c["KF"] = np.floor((c["FLIP"] - FA_START_DAY) / 7)
    k_open = np.floor((c["AGE"] - FA_START_DAY - 7) / 7)
    c["K_LAST"] = np.where(c["IS_REC"].eq(1), np.minimum(c["KF"], FA_KMAX), np.minimum(k_open, FA_KMAX))
    c = c[c["K_LAST"] >= 0]
    rep = c.loc[c.index.repeat(c["K_LAST"].astype(int) + 1)].copy()
    rep["K"] = rep.groupby(level=0).cumcount()
    rep["Y"] = (rep["IS_REC"].eq(1) & rep["K"].eq(rep["KF"])).astype(int)
    rep["CUT"] = rep["ED"] + pd.to_timedelta(FA_START_DAY + 7 * rep["K"], unit="D")
    return rep.reset_index(drop=True)


def _fa_icare_feats(rows, k, pain_fill=2.0):
    """iCare history before each row's CUT date: count, in-house time, gap, pain trend, improving answers."""
    keys = rows[["INC", "CUT"]].reset_index().rename(columns={"index": "RID"})
    m = keys.merge(k, on="INC", how="inner")
    m = m[m["ICARE_D"] < m["CUT"]]
    out = pd.DataFrame(index=rows.index)
    if len(m):
        m = m.assign(NOT_=m["IMPROVING"].eq(0).astype(int), ANS_=m["IMPROVING"].notna().astype(int),
                     HI_=(m["PAIN"] >= FA_HIGH_PAIN).astype(int)).sort_values(["RID", "ICARE_D"])
        gp = m.groupby("RID")
        agg = pd.DataFrame({"N": gp.size(), "FIRST_D": gp["ICARE_D"].min(), "LAST_D": gp["ICARE_D"].max(),
                            "P_FIRST": gp["PAIN"].first(), "P_LAST": gp["PAIN"].last(), "I_LAST": gp["IMPROVING"].last(),
                            "N_NOT": gp["NOT_"].sum(), "N_ANS": gp["ANS_"].sum(), "N_HI": gp["HI_"].sum()})
        out = out.join(agg)
    for col in ("N", "N_NOT", "N_ANS", "N_HI"):
        out[col] = out[col].fillna(0) if col in out else 0
    for col in ("FIRST_D", "LAST_D"):
        if col not in out:
            out[col] = pd.NaT
    for col in ("P_FIRST", "P_LAST", "I_LAST"):
        if col not in out:
            out[col] = np.nan
    cut = rows["CUT"]
    f = pd.DataFrame(index=rows.index)
    f["LOG_N"] = np.log1p(out["N"])
    f["NO_ICARE"] = out["N"].eq(0).astype(int)
    f["INHOUSE_WK"] = ((cut - out["FIRST_D"]).dt.days / 7).clip(0, 16).fillna(0)
    f["GAP_WK"] = ((cut - out["LAST_D"]).dt.days / 7).clip(0, 4).fillna(0)
    f["PAIN_NA"] = out["P_LAST"].isna().astype(int)
    f["PAIN_LAST"] = out["P_LAST"].fillna(pain_fill)   # missing = a typical pain, so PAIN_NA is "missing vs typical"
    d = (out["P_LAST"] - out["P_FIRST"]).fillna(0)
    f["PAIN_UP"], f["PAIN_DOWN"] = d.clip(lower=0), (-d).clip(lower=0)
    f["NOT_LAST"] = out["I_LAST"].eq(0).astype(int)
    f["NOT_SHARE"] = (out["N_NOT"] / out["N_ANS"].where(out["N_ANS"] > 0)).fillna(0)
    f["HIGH_N"] = np.log1p(out["N_HI"])
    raw = out[["N", "FIRST_D", "LAST_D", "P_FIRST", "P_LAST", "I_LAST"]]
    return f, raw


def _fa_design(rows, feats, sites, scale=None):
    """Model matrix: intercept, week-since-injury steps, iCare history, case facts, site (sites with 40+ cases)."""
    X = pd.DataFrame(index=rows.index)
    k = rows["K"]
    for lab, lo, hi in (("W2", 1, 1), ("W3", 2, 2), ("W4", 3, 3), ("W5_6", 4, 5), ("W7_9", 6, 8), ("W10P", 9, 99)):
        X[lab] = k.between(lo, hi).astype(int)
    for col in FA_CONT + ["NO_ICARE", "PAIN_NA", "NOT_LAST"]:
        X[col] = feats[col]
    X["CLOSED"] = (rows["CLOSED_D"].notna() & (rows["CLOSED_D"] < rows["CUT"])).astype(int)
    for col in ("IS_MSD", "NEW_HIRE", "EXTRA_TIME"):
        X[col] = pd.to_numeric(rows[col], errors="coerce").fillna(0).astype(int)
        X[col+"_UNKNOWN"] = pd.to_numeric(rows[col], errors="coerce").isna().astype(int)
    for t in FA_TYPES:
        X[f"T_{t}"] = rows["INJ_TYPE"].eq(t).astype(int)
    mg = rows["MECH"].map(FA_MECH_GROUP)
    for m_ in FA_MECHS:
        X[f"M_{m_}"] = mg.eq(m_).astype(int)
    for s in sites:
        X[f"S_{s}"] = rows["SITE"].eq(s).astype(int)
    X = X.astype(float)
    if scale is None:
        scale = {c_: (X[c_].mean(), X[c_].std() or 1.0) for c_ in FA_CONT}
    for c_, (mu, sd) in scale.items():
        X[c_] = (X[c_] - mu) / (sd if sd and np.isfinite(sd) and sd > 0 else 1.0)
    X.insert(0, "CONST", 1.0)
    return X, scale


def _fa_fit(X, y, lam=FA_LAMBDA, iters=60):
    """Ridge logistic regression by Newton steps (no penalty on the intercept). Returns (beta, covariance)."""
    A = X.to_numpy(float)
    yv = np.asarray(y, float)
    m = A.shape[1]
    beta = np.zeros(m)
    beta[0] = np.log(max(yv.mean(), 1e-4) / max(1 - yv.mean(), 1e-4))
    P = lam * np.eye(m)
    P[0, 0] = 1e-6

    def obj(b):   # penalized negative log-likelihood
        eta = A @ b
        return float(np.sum(np.logaddexp(0, eta) - yv * eta) + 0.5 * b @ P @ b)

    cur = obj(beta)
    H = np.eye(m)
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(A @ beta, -30, 30)))
        grad = A.T @ (yv - p) - P @ beta
        H = A.T @ (A * (p * (1 - p))[:, None]) + P
        step = np.linalg.solve(H, grad)
        t = 1.0
        while t > 1e-8:            # step halving: never accept a step that makes the fit worse
            new = obj(beta + t * step)
            if new <= cur + 1e-10:
                break
            t /= 2
        beta = beta + t * step
        done = abs(cur - new) < 1e-9 or np.max(np.abs(t * step)) < 1e-7
        cur = new
        if done:
            break
    return beta, np.linalg.inv(H)


def _fa_prob(X, beta):
    return 1 / (1 + np.exp(-np.clip(X.to_numpy(float) @ beta, -30, 30)))


def _fa_auc(y, p):
    y = np.asarray(y)
    n1, n0 = int(y.sum()), int(len(y) - y.sum())
    if n1 == 0 or n0 == 0:
        return np.nan
    r = pd.Series(p).rank().to_numpy()
    return (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)



def _fa_horizon_check(c, contacts, cut, today, beta, scale, sites, pain_fill):
    """Case-disjoint later cohort, one observation per case at each stated age.
    Latest source snapshot is a limitation: past edits cannot be reconstructed.
    """
    test = c[(c["ED"] >= cut) & (c["ED"]+pd.Timedelta(days=FA_HORIZON) <= today)].copy()
    rows=[]
    for age in (2,9,30,60,100,112):
        d=test[(test["IS_REC"].eq(0)) | (test["DOR"] > test["ED"]+pd.Timedelta(days=age))].copy().reset_index(drop=True)
        if d.empty:
            continue
        d["CUT"]=d["ED"]+pd.Timedelta(days=age+1)
        f,_=_fa_icare_feats(d,contacts,pain_fill)
        hz=[]
        for j in range(FA_KMAX+1):
            Xj,_=_fa_design(d.assign(K=j),f,sites,scale=scale)
            hz.append(_fa_prob(Xj,beta))
        pred=gov.horizon_probability(hz,np.full(len(d),age),14,FA_START_DAY,FA_HORIZON)
        actual=(d["IS_REC"].eq(1) & (d["DOR"]<=d["ED"]+pd.Timedelta(days=min(age+14,FA_HORIZON)))).to_numpy(int)
        rows.append({"Age at forecast":age,"Days forecast":min(14,FA_HORIZON-age),"Cases":len(d),
                     "Observed":int(actual.sum()),"Expected":float(pred.sum()),
                     "Brier score":float(np.mean((pred-actual)**2)),"AUC":_fa_auc(actual,pred)})
    return pd.DataFrame(rows)

@gov.viewer_cache(st, lambda: g("_viewer_context")(), ttl=FA_TTL, show_spinner=False)
def fa_model(today, _rev=3):
    """Cases, case-weeks, the fitted model, a time-split check, and every open first aid case scored. Cached 1 hour;
    a failed query raises, so an error is never cached."""
    today = pd.Timestamp(today)
    c, k = _fa_load()
    pp = _fa_periods(c, today)
    if pp.empty or pp["Y"].sum() < 10:
        raise RuntimeError("Insufficient delayed-recordability history for fitting.")
    pain_fill = float(k["PAIN"].median()) if k["PAIN"].notna().any() else 2.0
    feats, _ = _fa_icare_feats(pp, k, pain_fill)
    pp = pp.join(feats[["LOG_N", "INHOUSE_WK", "NOT_LAST", "NO_ICARE"]].rename(
        columns={"LOG_N": "F_LOGN", "INHOUSE_WK": "F_INH", "NOT_LAST": "F_NOT", "NO_ICARE": "F_NONE"}))
    big = c["SITE"].value_counts()
    sites = sorted(big[big >= 40].index)
    # Time-split check: fit on case-weeks that ended 180+ days ago, score the weeks after that.
    cut = today - pd.Timedelta(days=180)
    tr = (pp["ED"] + pd.Timedelta(days=FA_HORIZON)) < cut
    te = (pp["ED"] >= cut) & ((pp["CUT"] + pd.Timedelta(days=7)) <= today)
    check = {"cut": cut, "auc": np.nan, "pred": np.nan, "obs": 0, "n": int(te.sum()), "top5": np.nan}
    if tr.sum() > 500 and pp.loc[tr, "Y"].sum() >= 30 and te.sum() > 200 and pp.loc[te, "Y"].sum() >= 10:
        train_ids = set(pp.loc[tr, "INC"])
        train_pain = k.loc[k["INC"].isin(train_ids) & (k["ICARE_D"] < cut), "PAIN"].dropna()
        train_fill = float(train_pain.median()) if len(train_pain) else 2.0
        train_feats, _ = _fa_icare_feats(pp[tr], k, train_fill)
        test_feats, _ = _fa_icare_feats(pp[te], k, train_fill)
        train_counts = c[c["INC"].isin(train_ids)]["SITE"].value_counts()
        train_sites = sorted(train_counts[train_counts >= 40].index)
        Xtr, sc = _fa_design(pp[tr], train_feats, train_sites)
        b_tr, _ = _fa_fit(Xtr, pp.loc[tr, "Y"])
        Xte, _ = _fa_design(pp[te], test_feats, train_sites, scale=sc)
        pte = _fa_prob(Xte, b_tr)
        yte = pp.loc[te, "Y"].to_numpy()
        # Rank check on weeks where the case was still open in Intelex (closed cases are easy calls and would flatter it).
        op_ = (Xte["CLOSED"] == 0).to_numpy()
        po, yo = pte[op_], yte[op_]
        top = po >= np.quantile(po, 0.8) if len(po) else np.zeros(0, bool)
        check.update(auc=_fa_auc(yo, po), pred=float(pte.sum()), obs=int(yte.sum()), n=int(op_.sum()),
                     top5=float(yo[top].sum() / max(yo.sum(), 1)), auc_all=_fa_auc(yte, pte))
    check["horizon_check"] = _fa_horizon_check(c, k, cut, today, b_tr, sc, train_sites, train_fill) if "b_tr" in locals() else pd.DataFrame()
    X, sc = _fa_design(pp, feats, sites)
    beta, cov = _fa_fit(X, pp["Y"])
    coef = pd.DataFrame({"TERM": X.columns, "B": beta, "SE": np.sqrt(np.clip(np.diag(cov), 0, None))})
    # Score open first aid cases: iCare history through today, then project week by week to day 113.
    age = (today - c["ED"]).dt.days
    recent = k.groupby("INC")["ICARE_D"].max()
    last_ic = c["INC"].map(recent)
    open_ = c["IS_REC"].eq(0) & age.between(0, FA_HORIZON) & (
        ~c["STAGE"].str.lower().eq("completed") | ((today - last_ic).dt.days <= 14))
    o = c[open_].copy()
    out = o.assign(AGE=age[open_])
    if len(o):
        o["CUT"] = today + pd.Timedelta(days=1)
        fo, raw = _fa_icare_feats(o, k, pain_fill)
        hz = []
        for j in range(FA_KMAX+1):
            Xj, _ = _fa_design(o.assign(K=j), fo, sites, scale=sc)
            hz.append(_fa_prob(Xj, beta))
        out["P14"] = gov.horizon_probability(hz, out["AGE"], 14, FA_START_DAY, FA_HORIZON)
        out["PEV"] = gov.horizon_probability(hz, out["AGE"], FA_HORIZON, FA_START_DAY, FA_HORIZON)
        out["FORECAST_DAYS"] = np.maximum(np.minimum(out["AGE"]+14,FA_HORIZON)-np.maximum(out["AGE"]+1,FA_START_DAY)+1,0)
        out["N_IC"] = raw["N"].astype(int)
        out["INHOUSE_D"] = (today - raw["FIRST_D"]).dt.days
        out["LAST_AGO"] = (today - raw["LAST_D"]).dt.days
        d = raw["P_LAST"] - raw["P_FIRST"]
        out["TREND"] = np.select([raw["P_LAST"].isna(), raw["N"] < 2, d >= 1, d <= -1],
                                 ["No pain level", "One reading", "Rising", "Falling"], "Flat")
        out["ANSWER"] = np.select([raw["N"].eq(0), raw["I_LAST"].eq(0), raw["I_LAST"].eq(1)],
                                  ["No follow-up yet", "Not improving", "Improving"], "Not answered")
    return {"cases": c, "contacts": k, "pp": pp, "coef": coef, "scale": sc, "sites": sites, "check": check,
            "open": out.reset_index(drop=True), "today": today, "n_weeks": len(pp), "n_events": int(pp["Y"].sum())}


def _fa_today():
    """Today in Eastern time (the app's own helper is defined later in the script than the iCare tab)."""
    f = g("_today_et")
    if f is not None:
        return pd.Timestamp(f())
    try:
        from zoneinfo import ZoneInfo
        return pd.Timestamp(pd.Timestamp.now(tz=ZoneInfo("America/New_York")).date())
    except Exception:
        return pd.Timestamp.now().normalize()


def fa_data():
    try:
        return fa_model(_fa_today()), None
    except Exception as exc:
        return None, str(exc)[:400]


# ---- what the data says about timing ----
def fa_timing(pp, sites):
    """Weekly chance of becoming recordable for cases still first aid at the start of each week, and cumulative."""
    p = pp[pp["SITE"].isin(sites)]
    t = p.groupby("K").agg(AT_RISK=("Y", "size"), BECAME=("Y", "sum")).reindex(range(FA_KMAX + 1), fill_value=0)
    t["RATE"] = t["BECAME"] / t["AT_RISK"].where(t["AT_RISK"] > 0)
    t["CUM"] = 1 - (1 - t["RATE"].fillna(0)).cumprod()
    t["WEEK"] = [_fa_week_label(k_) for k_ in t.index]
    return t.reset_index()


def fa_by(pp, sites, col, bins, labels):
    p = pp[pp["SITE"].isin(sites)]
    b = pd.cut(p[col], bins=bins, labels=labels, right=False)
    t = p.groupby(b, observed=False).agg(WEEKS=("Y", "size"), BECAME=("Y", "sum"))
    t["RATE"] = t["BECAME"] / t["WEEKS"].where(t["WEEKS"] > 0) * 100
    return t.reset_index().rename(columns={col: "BAND"})


def fa_answer_table(pp, sites):
    p = pp[pp["SITE"].isin(sites)]
    lab = np.select([p["F_NONE"].eq(1), p["F_NOT"].eq(1)], ["No follow-up yet", "Latest: not improving"], "Latest: improving or not answered")
    t = p.groupby(lab).agg(WEEKS=("Y", "size"), BECAME=("Y", "sum"))
    t["RATE"] = t["BECAME"] / t["WEEKS"].where(t["WEEKS"] > 0) * 100
    return t.reset_index().rename(columns={"index": "BAND"})


def fa_switch_stats(c, k, sites):
    """Cases that became recordable 2+ days after the injury: when, after how many follow-ups and how long in house."""
    r = c[c["IS_REC"].eq(1) & c["SITE"].isin(sites)]
    if r.empty:
        return None
    m = r[["INC", "ED", "DOR"]].merge(k, on="INC", how="left")
    m = m[m["ICARE_D"] < m["DOR"]]
    gp = m.sort_values("ICARE_D").groupby("INC")
    n_before = r["INC"].map(gp.size()).fillna(0)
    first = r["INC"].map(gp["ICARE_D"].min())
    last_ans = r["INC"].map(gp["IMPROVING"].last())
    fa = c[c["IS_REC"].eq(0) & c["SITE"].isin(sites)]
    fa_last = fa["INC"].map(k.sort_values("ICARE_D").groupby("INC")["IMPROVING"].last())
    fa_all = c[c["SITE"].isin(sites) & ((c["IS_REC"].eq(1)) | (c["IS_REC"].eq(0)))]
    return {"n": len(r), "share": len(r) / max(len(fa_all), 1) * 100,
            "days_med": float((r["DOR"] - r["ED"]).dt.days.median()),
            "days_q": (r["DOR"] - r["ED"]).dt.days.quantile([0.25, 0.75]).tolist(),
            "with_ic": float((n_before > 0).mean() * 100), "ic_med": float(n_before[n_before > 0].median()) if (n_before > 0).any() else np.nan,
            "inhouse_med": float((r["DOR"] - first).dt.days.median()) if first.notna().any() else np.nan,
            "not_last": float(last_ans.eq(0).sum() / max(last_ans.notna().sum(), 1) * 100),
            "fa_not_last": float(fa_last.eq(0).sum() / max(fa_last.notna().sum(), 1) * 100)}


def fa_site_totals(o, sites):
    s = o[o["SITE"].isin(sites)]
    if s.empty:
        return pd.DataFrame(columns=["SITE", "OPEN", "EXP14", "EXPEV", "HIGH"])
    t = s.groupby("SITE").agg(OPEN=("INC", "size"), EXP14=("P14", "sum"), EXPEV=("PEV", "sum"),
                              HIGH=("PEV", lambda v: int((v >= FA_HIGH).sum())))
    t = t.reset_index().sort_values(["EXP14", "HIGH"], ascending=False)
    sg = g("site_gen")
    t.insert(1, "GEN", t["SITE"].map(sg) if sg is not None else "Other")
    return t


def fa_factors(coef):
    """Odds ratios for the case and iCare terms (per 1 SD for counts and levels; yes vs no for flags)."""
    t = coef[~coef["TERM"].str.match(r"^(CONST|W\d|W5_6|W7_9|W10P|S_)")].copy()
    t["OR"] = np.exp(t["B"])
    t["LO"], t["HI"] = np.exp(t["B"] - 1.96 * t["SE"]), np.exp(t["B"] + 1.96 * t["SE"])
    t["FACTOR"] = t["TERM"].map(FA_LABELS).fillna(t["TERM"])
    t["UNIT"] = np.where(t["TERM"].isin(FA_CONT), "per 1 SD", "yes vs no")
    t["CLEAR"] = (t["LO"] > 1) | (t["HI"] < 1)
    return t.sort_values("OR", ascending=False)


def fa_note_line(nc, sites):
    """Weekly note: open first aid cases, how many are expected to become recordable soon, and where."""
    fm = nc.get("fa")
    if not fm or fm.get("open") is None:
        return ""
    t = fa_site_totals(fm["open"], sites)
    if t.empty:
        return ""
    n, e14, hi = int(t["OPEN"].sum()), float(t["EXP14"].sum()), int(t["HIGH"].sum())
    txt = (f"Delayed recordability (now): {n:,} open first aid cases; about {e14:.1f} expected to become recordable in "
           f"up to 14 future days, capped at day 113; {hi:,} at {FA_HIGH * 100:.0f}%+ risk by day {FA_HORIZON}")
    top = t[t["HIGH"] > 0].sort_values("HIGH", ascending=False).head(3)
    if len(sites) > 1 and len(top):
        txt += " (most at " + ", ".join(f"{r['SITE']} {int(r['HIGH'])}" for _, r in top.iterrows()) + ")"
    ss = fa_switch_stats(fm["cases"], fm["contacts"], sites)
    if ss:
        txt += (f". Cases that became recordable did so at a median of day {ss['days_med']:.0f}, after "
                f"{ss['ic_med']:.0f} in-house follow-ups" if np.isfinite(ss.get("ic_med", np.nan)) else
                f". Cases that became recordable did so at a median of day {ss['days_med']:.0f}")
    return txt + ". Use Intelex for individual follow-up; the general dashboard view is aggregate."


# ---- app section (Safety KPIs > iCare) ----
def render_fa():
    BORDER, STRETCH, NC = g("BORDER") or {}, g("STRETCH") or {}, st.column_config.NumberColumn
    st.markdown("#### Delayed recordability · follow-up support")
    st.info("Cohort proxy: recordability first recorded 2+ days after injury, or first aid now. This does not prove a documented first-aid-to-recordable transition. Probabilities do not determine care, recordability or work authorization.")
    gov.render_scope(st, _namespace(), "Current snapshot")
    dfact = _daily_fact()
    pool = sorted(g("scope")(dfact, g("sel_regions"), g("sel_sites"))["SITE"].unique()) if dfact is not None else []
    fm, err = fa_data()
    if fm is None:
        st.caption(f"Delayed recordability couldn't load: {err}")
        return
    sites = [s for s in pool if s in set(fm["cases"]["SITE"])] or pool
    c, k, pp, o = fm["cases"], fm["contacts"], fm["pp"], fm["open"]
    ss = fa_switch_stats(c, k, sites)
    t = fa_site_totals(o, sites)
    chk = fm["check"]
    kc = g("kpi_cols")(4)
    kc[0].metric("Open first aid cases", f"{int(t['OPEN'].sum()):,}" if len(t) else "0",
                 help=f"First aid now, injured in the last {FA_HORIZON} days, case not completed in Intelex or an iCare "
                      "follow-up in the last 14 days.", **BORDER)
    kc[1].metric("Expected delayed recordability · up to 14 days", f"{t['EXP14'].sum():.1f}" if len(t) else "0",
                 help="Sum of probabilities for future days through min(14 days, day 113 after injury).", **BORDER)
    kc[2].metric(f"High risk ({FA_HIGH * 100:.0f}%+ by day {FA_HORIZON})", f"{int(t['HIGH'].sum()):,}" if len(t) else "0", **BORDER)
    kc[3].metric("Became recordable later", f"{ss['n']:,}" if ss else "—",
                 f"{ss['share']:.1f}% of proxy-cohort cases" if ss else None, delta_color="off",
                 help="Since Jun 2025: recordable 2+ days after the injury, with no verified prior first-aid status history.", **BORDER)
    if ss:
        st.caption(f"In the sites in scope, cases that became recordable did so at a median of day {ss['days_med']:.0f} after "
                   f"the injury (middle half: day {ss['days_q'][0]:.0f}–{ss['days_q'][1]:.0f}). "
                   f"{ss['with_ic']:.0f}% had in-house iCare follow-ups first: a median of {ss['ic_med']:.0f} follow-ups over "
                   f"{ss['inhouse_med']:.0f} days. At the last follow-up before the switch, {ss['not_last']:.0f}% said pain was "
                   f"not improving, vs {ss['fa_not_last']:.0f}% at the last follow-up of cases that stayed first aid.")
    tm = fa_timing(pp, sites)
    a, b = g("pair_cols")(2)
    with a:
        st.markdown("**When it happens** · observed weekly delayed-recordability fraction in the proxy cohort")
        base = alt.Chart(tm).encode(x=alt.X("WEEK:N", sort=list(tm["WEEK"]), title=None,
                                            axis=alt.Axis(labelAngle=-40, labelLimit=120)))
        bars = base.mark_bar(color=(g("PALETTE") or ["#4c78a8"])[0], opacity=0.85).encode(
            y=alt.Y("RATE:Q", title="Became recordable that week", axis=alt.Axis(format="%")),
            tooltip=[alt.Tooltip("WEEK:N", title=""), alt.Tooltip("AT_RISK:Q", title="Cases still first aid", format=","),
                     alt.Tooltip("BECAME:Q", title="Became recordable", format=","), alt.Tooltip("RATE:Q", title="Rate", format=".1%")])
        line = base.mark_line(color=(g("PALETTE") or ["", "#e45756"])[1], point=True).encode(
            y=alt.Y("CUM:Q", title="Cumulative", axis=alt.Axis(format="%")),
            tooltip=[alt.Tooltip("WEEK:N", title=""), alt.Tooltip("CUM:Q", title="Cumulative by end of week", format=".1%")])
        g("show_chart")(alt.layer(bars, line).resolve_scale(y="independent").properties(height=260))
        st.caption("Bars: weekly rate. Line: share of first aid cases that became recordable by the end of that week. "
                   "Week 1 starts on day 2 (day 0–1 recordability is outside this cohort).")
    with b:
        st.markdown("**Time since first iCare follow-up** · weekly rate by iCare history at the start of the week")
        t1 = fa_by(pp, sites, "F_INH", [-0.1, 1, 2, 4, 8, 99], ["Under 1 week", "1–2 weeks", "2–4 weeks", "4–8 weeks", "8+ weeks"])
        t1 = t1.assign(DIM="Time in house (since first follow-up)")
        t2 = fa_by(pp, sites, "F_LOGN", [-0.1, np.log1p(1), np.log1p(3), np.log1p(6), np.log1p(10), 99],
                   ["0", "1–2", "3–5", "6–9", "10+"]).assign(DIM="iCare follow-ups so far")
        t3 = fa_answer_table(pp, sites).assign(DIM="Latest follow-up answer")
        tb = pd.concat([t2, t1, t3], ignore_index=True)
        tb["BAND"] = tb["BAND"].astype(str)
        st.dataframe(tb[["DIM", "BAND", "WEEKS", "BECAME", "RATE"]], hide_index=True, **STRETCH, column_config={
            "DIM": "iCare history", "BAND": "Band", "WEEKS": NC("Case-weeks", format="%d"),
            "BECAME": NC("Became recordable", format="%d"), "RATE": NC("Weekly rate", format="%.1f%%")})
        st.caption("Each row counts case-weeks in the proxy cohort with that history and how often it became recordable "
                   "that week. 'Time in house' for cases with no follow-up yet sits in 'Under 1 week'.")
    st.markdown("**Open first aid cases to watch** · chance of becoming recordable")
    w = o[o["SITE"].isin(sites)].sort_values(["P14", "PEV"], ascending=False)
    if w.empty:
        st.caption("No open first aid cases in the sidebar scope.")
    elif not gov.role_allowed(_namespace()):
        st.caption("Aggregate site view. Individual-case details and exports require an administrator-authorized runtime role. Open Intelex for case follow-up.")
    else:
        view = w[["INC", "SITE", "ED", "AGE", "INJ_TYPE", "MECH", "N_IC", "INHOUSE_D", "LAST_AGO", "TREND", "ANSWER", "P14", "PEV", "FORECAST_DAYS"]].copy()
        view["P14"], view["PEV"] = view["P14"] * 100, view["PEV"] * 100
        show_all = st.toggle("Show all open cases", value=False, key="fa_all")
        st.dataframe(view if show_all else view.head(25), hide_index=True, **STRETCH, column_config={
            "INC": "Incident #", "SITE": "Site", "ED": st.column_config.DateColumn("Injury date", format="MM/DD/YYYY"),
            "AGE": NC("Days since injury", format="%d"), "INJ_TYPE": "Injury type", "MECH": "Mechanism",
            "N_IC": NC("iCare follow-ups", format="%d"), "INHOUSE_D": NC("Days in house", format="%d",
                                                                          help="Days since the first iCare follow-up."),
            "LAST_AGO": NC("Last follow-up (days ago)", format="%d"), "TREND": "Pain trend", "ANSWER": "Latest answer",
            "P14": NC("Chance · up to 14 days", format="%.0f%%"), "PEV": NC(f"Chance · by day {FA_HORIZON}", format="%.0f%%")})
        g("download")(view, "first_aid_watch_list.csv", "dl_fa_watch")
        st.caption("Incident # and categorical fields only: no names, usernames, notes or pain scores. Pain trend compares "
                   "the latest pain level with the first (Rising or Falling = a change of 1+ point). Look the case up in "
                   "Intelex by Incident # to act on it.")
    st.markdown("**By site**")
    if len(t):
        st.dataframe(t, hide_index=True, **STRETCH, column_config={
            "SITE": "Site", "GEN": "Generation", "OPEN": NC("Open first aid cases", format="%d"),
            "EXP14": NC("Expected recordable · up to 14 days", format="%.1f"),
            "EXPEV": NC(f"Expected recordable · by day {FA_HORIZON}", format="%.1f"),
            "HIGH": NC(f"High risk ({FA_HIGH * 100:.0f}%+)", format="%d")})
    st.caption(f"Future days only, conditional on survival through today, capped at day {FA_HORIZON}. Weekly hazards are prorated assuming a constant daily hazard within each week. Current iCare features are held fixed. A day-112 case has only one modeled day left.")
    with st.expander("Model method and validation"):
        hc = chk.get("horizon_check", pd.DataFrame())
        if len(hc):
            st.markdown("**Later-cohort horizon check**")
            st.dataframe(hc, hide_index=True, use_container_width=True)
        st.caption("Training and later evaluation use different incident cohorts. Imputation, scaling and site selection are learned from training only. Checks use the latest source snapshot; historical source corrections are not reconstructable. Calibration by age remains exploratory, not clinical validation.")
        if np.isfinite(chk.get("auc", np.nan)):
            st.markdown(f"**Check on later data.** Fitted on cases whose full day-113 horizon ended before {chk['cut']:%b %d, %Y}, then "
                        f"scored cases injured after it. It predicted {chk['pred']:.0f} cases becoming recordable vs "
                        f"{chk['obs']:,} actual. On the {chk['n']:,} weeks where the case was still open in Intelex, it ranked "
                        f"a week where the case became recordable above one where it didn't {chk['auc'] * 100:.0f}% of the "
                        f"time (AUC {chk['auc']:.2f}; 50% = chance), and the riskiest fifth of those weeks held "
                        f"{chk['top5'] * 100:.0f}% of the cases that became recordable.")
        else:
            st.markdown("Not enough history yet for a check on later data.")
        f = fa_factors(fm["coef"])
        st.dataframe(f[["FACTOR", "UNIT", "OR", "LO", "HI", "CLEAR"]], hide_index=True, **STRETCH, column_config={
            "FACTOR": "Factor", "UNIT": "Compared", "OR": NC("Odds ratio", format="%.2f"),
            "LO": NC("95% low", format="%.2f"), "HI": NC("95% high", format="%.2f"),
            "CLEAR": st.column_config.CheckboxColumn("Clear of 1")})
        st.caption(
            f"A discrete-time hazard model: for every week a case is still first aid (from day {FA_START_DAY} to day "
            f"{FA_HORIZON}), a ridge logistic regression (penalty {FA_LAMBDA:g}) predicts whether it becomes recordable that "
            "week from the week since injury, its iCare history up to that week (follow-ups so far, time in house, weeks "
            "since the last follow-up, latest pain and its change, 'not improving' answers, high-pain follow-ups), whether "
            "the case is closed, injury type, mechanism, MSD, new hire, MET/VET and site (sites with 40+ cases). "
            f"Fitted on {fm['n_weeks']:,} case-weeks with {fm['n_events']:,} switches since Jun 2025, all sites. An open "
            "case's chance projects its current iCare history forward week by week. Odds ratios show association, not "
            "cause; counts and levels are per 1 SD. Recordable = Intelex date of recordability 2+ days after the injury; "
            "recordable cases with no date are left out. The sidebar date range doesn't apply (current snapshot).")


# ---------------------------------------------------------------------------------------------------------------
# Year-end injury rates: where each site, region and the network will finish the year (fiscal or calendar).
# Year-end count = injuries so far + open first aid cases expected to become recordable (they count on their injury
# date) + projected injuries for the days left. Days left use the S&OP plan hours and the hours-to-risk curve (as on
# Peak glidepath) and the site's normal week after the plan ends. The rate behind the projection is the "eventual"
# 13-week rate: recent first aid cases that are still likely to become recordable are moved into recordables first.
# ---------------------------------------------------------------------------------------------------------------
YE_METRICS = {"RECORDABLE_INCIDENTS": "Recordable rate", "FIRST_AID_INCIDENT": "First aid rate", "MSD_INJURIES": "MSD rate"}
YE_Z = 1.28   # 80% process-noise range


def ye_bounds(kind, last_day):
    L = pd.Timestamp(last_day).normalize()
    if kind == "Calendar":
        return f"Calendar {L.year}", pd.Timestamp(L.year,1,1), pd.Timestamp(L.year,12,31)
    cal, _ = g("load_calendar_and_regions")()
    return gov.authoritative_year_bounds(cal, L)


def _ye_prev(kind, start):
    if kind == "Calendar":
        return pd.Timestamp(start.year - 1, 1, 1), pd.Timestamp(start.year - 1, 12, 31)
    cal, _ = g("load_calendar_and_regions")()
    c = cal.dropna(subset=["FISCAL_YEAR"])
    prev = c[pd.to_datetime(c["DT"]) < start]
    if prev.empty:
        return None
    fy = prev.sort_values("DT")["FISCAL_YEAR"].iloc[-1]
    d = pd.to_datetime(prev.loc[prev["FISCAL_YEAR"] == fy, "DT"])
    return d.min().normalize(), start - pd.Timedelta(days=1)


@gov.viewer_cache(st, lambda: g("_viewer_context")(), ttl=3600, show_spinner=False)
def ye_frame(kind, pool, today, _rev=3):
    """One row per site x metric: so far, pending first aid conversions, projected rest of year, and last year."""
    pool = list(pool)
    today = pd.Timestamp(today)
    dfact = _daily_fact()
    b_ = ye_bounds(kind, dfact["DT"].max())
    if b_ is None:
        raise RuntimeError("A complete governed fiscal calendar with both year boundaries is unavailable. Use Calendar year; no 52/53-week endpoint is inferred.")
    label, A, Z = b_
    d_all = dfact[dfact["SITE"].isin(pool)]
    # One "data through" day for every scope, so a site reads the same alone or inside a region.
    L = gov.screened_through(dfact, today)
    if pd.isna(L):
        raise RuntimeError("No closed day passed the exposure/coverage screen. Upstream reconciliation required.")
    cur_wk = today - pd.Timedelta(days=(today.weekday() + 1) % 7)
    w_all = g("site_weeks")(dfact)
    w_sc = w_all[w_all["SITE"].isin(pool)]
    hol, _, sop, _ = g("load_plan")()
    hol_w = g("holiday_weeks")(hol) if hol is not None and len(hol) else None
    eps = 0.6
    if hol_w is not None:
        e_, _n = g("hours_elasticity")(w_all, hol_w)
        eps = float(np.clip(e_, 0.2, 1.2)) if pd.notna(e_) else 0.6
    fm, _ = fa_data()
    op = fm["open"] if fm is not None else pd.DataFrame(columns=["SITE", "ED", "PEV"])
    prev = _ye_prev(kind, A)
    weeks = pd.date_range(L + pd.Timedelta(days=1) - pd.Timedelta(days=((L + pd.Timedelta(days=1)).weekday() + 1) % 7),
                          Z, freq="7D")
    rows = []
    for m in YE_METRICS:
        cov = (g("coverage") or (lambda *a, **k: {}))(dfact, [m]) or {}
        first_nz = cov.get(m, (pd.NaT, pd.NaT))[0]
        x = g("load_response")(w_all, m, first_nz if pd.notna(first_nz) else w_all["WK"].min())  # same start as Peak glidepath
        fit = g("fit_hinge")(x)
        b_fit = fit[1] if fit else 0.0
        proj = pd.DataFrame()
        if hol_w is not None and sop is not None and len(sop):
            proj, _ = g("project_peak")(w_sc, hol_w, sop, m, cur_wk, eps, b_fit)
        pj = proj.set_index(["SITE", "WK"]) if len(proj) else None
        for s in pool:
            ds = d_all[d_all["SITE"] == s]
            if ds.empty:
                continue
            fs = w_sc[(w_sc["SITE"] == s) & (w_sc["DAYS"] == 7) & (w_sc["WK"] < cur_wk)].sort_values("WK")
            ytd = ds[(ds["DT"] >= A) & (ds["DT"] <= L)]
            n_ytd, h_ytd = float(ytd[m].sum()), float(ytd["TOTAL_HOURS"].sum())
            o = op[op["SITE"] == s]
            conv = o.loc[(o["ED"] >= A) & (o["ED"] <= L), "PEV"]
            conv_var = float((conv * (1-conv)).sum()) if fm is not None else np.nan
            rw = fs.tail(13)
            n13, h13 = float(rw[m].sum()), float(rw["TOTAL_HOURS"].sum())
            # Pending cases from the same 13 full weeks as the counts and hours (not the week in progress).
            w_lo = rw["WK"].min() if len(rw) else L
            w_hi = rw["WK"].max() + pd.Timedelta(days=6) if len(rw) else L
            conv13 = float(o.loc[(o["ED"] >= w_lo) & (o["ED"] <= w_hi), "PEV"].sum()) if fm is not None else np.nan
            sign = {"RECORDABLE_INCIDENTS": 1, "FIRST_AID_INCIDENT": -1}.get(m, 0)
            n13_eff = max(n13 + sign * conv13, 0) if sign and np.isfinite(conv13) else (n13 if not sign else np.nan)
            rate = n13_eff / h13 if h13 > 0 else np.nan
            ult = n13_eff / n13 if n13 > 0 else 1.0
            base_hrs = float(fs.tail(8)["TOTAL_HOURS"].median()) if len(fs) else np.nan
            h_fut = n_fut = 0.0
            in_plan = 0
            for wk in weeks:
                lo, hi = max(wk, L + pd.Timedelta(days=1)), min(wk + pd.Timedelta(days=6), Z)
                frac = max((hi - lo).days + 1, 0) / 7
                if frac <= 0:
                    continue
                if pj is not None and (s, wk) in pj.index and pd.notna(rate):
                    r = pj.loc[(s, wk)]
                    h_fut += float(r["HRS"]) * frac
                    n_fut += float(r["HRS"]) * float(r["MULT"]) * rate * frac   # plan hours x hours-to-risk curve x eventual rate
                    in_plan += 1
                elif pd.notna(base_hrs) and pd.notna(rate):
                    h_fut += base_hrs * frac
                    n_fut += base_hrs * rate * frac
            if Z > L and (h_fut <= 0 or not np.isfinite(rate)):
                n_fut, h_fut = np.nan, np.nan
            ly_n = ly_h = np.nan
            if prev is not None and pd.Timestamp(dfact["DT"].min()) <= prev[0]:
                lyd = ds[(ds["DT"] >= prev[0]) & (ds["DT"] <= prev[1])]
                needed = (prev[1]-prev[0]).days+1
                if lyd["DT"].nunique() == needed and lyd[[m,"TOTAL_HOURS"]].notna().all().all():
                    ly_n, ly_h = float(lyd[m].sum()), float(lyd["TOTAL_HOURS"].sum())
            rows.append({"SITE": s, "REGION": ds["REGION"].iloc[-1] if "REGION" in ds else "Other", "METRIC": m,
                         "N_YTD": n_ytd, "H_YTD": h_ytd, "CONV": sign * float(conv.sum()) if fm is not None or sign == 0 else np.nan, "CONV_VAR": conv_var * abs(sign) if sign else 0.0,
                         "N_FUT": n_fut, "H_FUT": h_fut, "PLAN_WEEKS": in_plan, "LY_N": ly_n, "LY_H": ly_h})
    return {"rows": pd.DataFrame(rows), "label": label, "start": A, "end": Z, "last": L, "prev": prev, "eps": eps,
            "fa_ok": fm is not None}


def ye_rollup(r, keys, basis):
    """Sum counts and hours to `keys` (or the whole scope), then rates: now, now with pending conversions, year end
    (80% process-noise range from Poisson noise on the days left plus the conversion uncertainty), and last year."""
    if r.empty:
        return pd.DataFrame()
    t = r.groupby(keys).agg(N_YTD=("N_YTD", gov.strict_sum), H_YTD=("H_YTD", gov.strict_sum), CONV=("CONV", gov.strict_sum), CONV_VAR=("CONV_VAR", gov.strict_sum),
                            N_FUT=("N_FUT", gov.strict_sum), H_FUT=("H_FUT", gov.strict_sum), LY_N=("LY_N", lambda v: v.sum(min_count=len(v))),
                            LY_H=("LY_H", lambda v: v.sum(min_count=len(v)))).reset_index()
    t["N_YE"] = t["N_YTD"] + t["CONV"] + t["N_FUT"]
    t["H_YE"] = t["H_YTD"] + t["H_FUT"]
    sd = np.sqrt(t["N_FUT"].clip(lower=0) + t["CONV_VAR"])
    k = basis / t["H_YE"].where(t["H_YE"] > 0)
    t["RATE_NOW"] = t["N_YTD"] * basis / t["H_YTD"].where(t["H_YTD"] > 0)
    t["RATE_NOW_ADJ"] = (t["N_YTD"] + t["CONV"]) * basis / t["H_YTD"].where(t["H_YTD"] > 0)
    t["RATE_YE"] = t["N_YE"] * k
    t["LO"] = (t["N_YE"] - YE_Z * sd).clip(lower=t["N_YTD"]) * k
    t["HI"] = (t["N_YE"] + YE_Z * sd) * k
    t["RATE_LY"] = t["LY_N"] * basis / t["LY_H"].where(t["LY_H"] > 0)
    t["VS_LY"] = (t["RATE_YE"] / t["RATE_LY"] - 1) * 100
    return t


def ye_data(kind, pool):
    try:
        return ye_frame(kind, tuple(sorted(pool)), _fa_today()), None
    except Exception as exc:
        return None, str(exc)[:400]


def ye_note_line(sites):
    """Weekly note (Ahead): fiscal year-end recordable rate for the scope, and the sites heading highest."""
    ye, _ = ye_data("Fiscal", sites)
    if ye is None or ye["rows"].empty:
        return ""
    basis = g("basis")
    r = ye["rows"][ye["rows"]["METRIC"] == "RECORDABLE_INCIDENTS"]
    net = ye_rollup(r.assign(ALL="all"), ["ALL"], basis)
    if net.empty or not np.isfinite(net["RATE_YE"].iloc[0]):
        return ""
    n = net.iloc[0]
    txt = (f"Year end ({ye['label']}, ends {ye['end']:%b %d}): recordable rate heading to {n['RATE_YE']:.2f} "
           f"(80% process-noise range {n['LO']:.2f}–{n['HI']:.2f}) vs {n['RATE_NOW']:.2f} so far")
    if n["CONV"] >= 0.5:
        txt += f"; about {n['CONV']:.0f} of that is open first aid cases expected to become recordable"
    if len(sites) > 1:
        s = ye_rollup(r, ["SITE"], basis).dropna(subset=["RATE_YE"]).sort_values("RATE_YE", ascending=False).head(3)
        if len(s):
            txt += "; highest: " + ", ".join(f"{x['SITE']} {x['RATE_YE']:.2f}" for _, x in s.iterrows())
    return txt + "."


# ---- app section (Peak glidepath) ----
def render_yearend():
    BORDER, STRETCH, NC = g("BORDER") or {}, g("STRETCH") or {}, st.column_config.NumberColumn
    basis, basis_label = g("basis"), g("basis_label")
    st.markdown("#### Year-end injury rates · site, region and network")
    dfact = _daily_fact()
    if dfact is None or dfact.empty:
        st.caption("No daily safety data to project from.")
        return
    pool = sorted(g("scope")(dfact, g("sel_regions"), g("sel_sites"))["SITE"].unique())
    c1, c2, _ = st.columns([1.2, 1.4, 1.4])
    kind = c1.radio("Year end", ["Calendar", "Fiscal"], horizontal=True, key="ye_kind",
                    help="Fiscal = Chewy fiscal year from the PLAT calendar. Calendar = Jan 1 – Dec 31 (the OSHA 300 log year).")
    m = c2.selectbox("Rate", list(YE_METRICS), format_func=YE_METRICS.get, key="ye_metric")
    ye, err = ye_data(kind, pool)
    if ye is None:
        st.caption(f"Year-end rates couldn't be built: {err}")
        return
    r = ye["rows"][ye["rows"]["METRIC"] == m]
    net = ye_rollup(r.assign(ALL="Network"), ["ALL"], basis)
    if net.empty:
        st.caption("No sites in scope with safety data this year.")
        return
    n = net.iloc[0]
    st.caption(f"{ye['label']}: {ye['start']:%b %d, %Y} – {ye['end']:%b %d, %Y} · data through {ye['last']:%b %d} · "
               f"{len(r)} sites in scope · rates {basis_label}")
    k = g("kpi_cols")(4)
    k[0].metric("So far", f"{n['RATE_NOW']:.2f}", help=f"{n['N_YTD']:,.0f} injuries over {n['H_YTD']:,.0f} hours, as reported now.", **BORDER)
    k[1].metric("Projected year end", fnum(n["RATE_YE"], ".2f", dash="Unavailable"), f"80% process-noise range {n['LO']:.2f}–{n['HI']:.2f}", delta_color="off",
                help=f"{n['N_YE']:,.1f} injuries over {n['H_YE']:,.0f} hours by {ye['end']:%b %d}.", **BORDER)
    if m == "RECORDABLE_INCIDENTS":
        k[2].metric("Pending delayed recordability", fnum(n["CONV"], "+.1f", dash="Unavailable"),
                    f"so far incl. these: {fnum(n['RATE_NOW_ADJ'], '.2f', dash='Unavailable')}", delta_color="off",
                    help="Open first aid cases injured this year, each weighted by its chance of becoming recordable "
                         "(iCare tab). They count on their injury date.", **BORDER)
    elif m == "FIRST_AID_INCIDENT":
        k[2].metric("Leaving first aid (→ recordable)", fnum(n["CONV"], "+.1f", dash="Unavailable"), delta_color="off", **BORDER)
    else:
        k[2].metric("Projected for the days left", f"{n['N_FUT']:,.1f}", delta_color="off", **BORDER)
    k[3].metric("Last year", f"{n['RATE_LY']:.2f}" if np.isfinite(n["RATE_LY"]) else "—",
                f"{n['VS_LY']:+.0f}% projected vs last year" if np.isfinite(n["VS_LY"]) else None, delta_color="inverse",
                help="Same metric over the whole previous year (fiscal or calendar), if the data covers it.", **BORDER)
    sg = g("site_gen") or (lambda s_: "Other")
    gen = r["SITE"].map(sg)
    r = r.assign(GEN=gen, RG=np.where(gen.ne("Other"), r["REGION"].astype(str) + " · " + gen, ""))
    reg = ye_rollup(r, ["REGION"], basis).assign(LEVEL="Region").rename(columns={"REGION": "NAME"})
    rg = ye_rollup(r[r["RG"] != ""], ["RG"], basis).assign(LEVEL="Region · generation").rename(columns={"RG": "NAME"})
    gn = ye_rollup(r[r["GEN"] != "Other"], ["GEN"], basis).assign(LEVEL="Generation").rename(columns={"GEN": "NAME"})
    sit = ye_rollup(r, ["SITE"], basis).assign(LEVEL="Site").rename(columns={"SITE": "NAME"})
    sit["NAME"] = sit["NAME"] + " · " + sit["NAME"].map(sg)
    nt = net.assign(LEVEL="Network").rename(columns={"ALL": "NAME"})
    nt["NAME"] = "All sites in scope"
    tb = pd.concat([nt, reg.sort_values("RATE_YE", ascending=False), gn, rg.sort_values("NAME"),
                    sit.sort_values("RATE_YE", ascending=False)], ignore_index=True)
    cols = ["LEVEL", "NAME", "RATE_NOW", "CONV", "RATE_YE", "LO", "HI", "RATE_LY", "VS_LY", "N_YTD", "N_FUT", "N_YE"]
    st.dataframe(tb[cols], hide_index=True, **STRETCH, column_config={
        "LEVEL": "Level", "NAME": "Name", "RATE_NOW": NC("So far", format="%.2f"),
        "CONV": NC("Pending delayed recordability", format="%+.1f"),
        "RATE_YE": NC("Projected year end", format="%.2f"), "LO": NC("Process-noise 80% low", format="%.2f"), "HI": NC("Process-noise 80% high", format="%.2f"),
        "RATE_LY": NC("Last year", format="%.2f"), "VS_LY": NC("vs last year", format="%+.0f%%"),
        "N_YTD": NC("Injuries so far", format="%.0f"), "N_FUT": NC("Projected · days left", format="%.1f"),
        "N_YE": NC("Projected · full year", format="%.1f")})
    g("download")(tb[cols], f"year_end_{m.lower()}_{kind.lower()}.csv", "dl_ye")
    fa_txt = ("" if ye["fa_ok"] else " Pending conversion estimates are unavailable. Dependent recordable/first-aid projections are incomplete.")
    st.caption(
        "Year end = injuries so far + open first aid cases expected to become recordable (each weighted by its chance; "
        "they count on their injury date, so they lift recordables and lower first aid) + projected injuries for the "
        "days left. Days left inside the S&OP plan use the planned hours and the hours-to-risk curve from Peak glidepath "
        f"(hours per unit of volume {ye['eps']:.2f}); after the plan ends, and for sites without a plan, the site's normal "
        "week (median of the last 8 full weeks). The rate behind the projection is the site's last 13 full weeks, with "
        "those weeks' still-open first aid cases moved by their chance of becoming recordable. The 80% process-noise range covers "
        "chance variation in the days left and in which open cases become recordable, not plan misses." + fa_txt)


# ---------------------------------------------------------------------------------------------------------------
# Today strip (top of the Site tab): what a site EHS lead checks each morning. Yesterday's injuries and MOE vs the same
# day last week, this week's provisional status, open first aid cases most likely to become recordable (Incident #),
# iCare cases not reached, and where the fiscal-year recordable rate is heading. Site codes, counts and Incident #s only.
# ---------------------------------------------------------------------------------------------------------------
def _yday(sfact, site, today):
    d = sfact[sfact["SITE"] == site]
    days = d.loc[d["TOTAL_HOURS"] > 0, "DT"]
    days = days[days < today]
    if days.empty:
        return None
    screen = gov.screened_through(d, today)
    if pd.notna(screen):
        days = days[days <= screen]
    if days.empty:
        return None
    day = pd.Timestamp(days.max())
    cols = [c for c in ("RECORDABLE_INCIDENTS", "FIRST_AID_INCIDENT", "MSD_INJURIES", "MOE_INCIDENTS") if c in d.columns]
    a = d[d["DT"] == day][cols].sum()
    b = d[d["DT"] == day - pd.Timedelta(days=7)][cols].sum()
    return day, a, b


def render_today(site, sfact, status_now):
    """App: the Today strip for one site."""
    BORDER = g("BORDER") or {}
    today = _fa_today()
    with st.container(**BORDER):
        st.markdown(f"#### Today · {site} · {today:%a %b %d}")
        k = g("kpi_cols")(5)
        y = _yday(sfact, site, today)
        if y is not None:
            day, a, b = y
            inj = int(a.get("RECORDABLE_INCIDENTS", 0) + a.get("FIRST_AID_INCIDENT", 0))
            inj_b = int(b.get("RECORDABLE_INCIDENTS", 0) + b.get("FIRST_AID_INCIDENT", 0))
            k[0].metric(f"Injuries · {day:%a %b %d}", f"{inj}", f"{inj - inj_b:+d} vs {day - pd.Timedelta(days=7):%a %b %d}",
                        delta_color="inverse",
                        help=f"Recordables {int(a.get('RECORDABLE_INCIDENTS', 0))} · first aid {int(a.get('FIRST_AID_INCIDENT', 0))} · "
                             f"MSD {int(a.get('MSD_INJURIES', 0))} · MOE {int(a.get('MOE_INCIDENTS', 0))}. "
                             "Latest day loaded before today.", **BORDER)
        else:
            k[0].metric("Injuries · yesterday", "—", **BORDER)
        k[1].metric("Status this week", status_now or "—", "provisional" if status_now else None, delta_color="off",
                    help="This week so far (ops days loaded so far); can't reach Elevated until the week is complete.", **BORDER)
        fm, _ = fa_data()
        o = fm["open"][fm["open"]["SITE"] == site].sort_values("P14", ascending=False) if fm is not None else pd.DataFrame()
        hi = o[o["PEV"] >= FA_HIGH] if len(o) else o
        k[2].metric("Delayed-recordability follow-up", f"{len(hi)}" if fm is not None else "—",
                    f"of {len(o)} open · {o['P14'].sum():.1f} expected in 2 wks" if len(o) else None, delta_color="off",
                    help=f"Open first aid cases with a {FA_HIGH * 100:.0f}%+ chance of becoming recordable by day {FA_HORIZON} "
                         "(Safety KPIs › iCare).", **BORDER)
        ic = (g("icare_frames") or {}).get("cases")
        nf = ni = None
        if ic is not None and len(ic) and "SITE" in ic:
            c = ic[ic["SITE"] == site]
            nf = int(pd.to_numeric(c.get("OPEN_NO_FOLLOWUP"), errors="coerce").fillna(0).sum()) if "OPEN_NO_FOLLOWUP" in c else None
            ni = int(pd.to_numeric(c.get("OPEN_NOT_IMPROVING"), errors="coerce").fillna(0).sum()) if "OPEN_NOT_IMPROVING" in c else None
        k[3].metric("iCare · open, no follow-up", f"{nf}" if nf is not None else "—",
                    f"{ni} latest not improving" if ni else None, delta_color="off",
                    help="Open iCare cases with no first follow-up date in the current snapshot.", **BORDER)
        ye, _ = ye_data("Fiscal", [site])
        yr = None
        if ye is not None and len(ye["rows"]):
            rr = ye_rollup(ye["rows"][ye["rows"]["METRIC"] == "RECORDABLE_INCIDENTS"].assign(ALL=site), ["ALL"], g("basis"))
            yr = rr.iloc[0] if len(rr) else None
        k[4].metric(f"Recordable rate · {ye['label'] if ye else 'year'} end", f"{yr['RATE_YE']:.2f}" if yr is not None else "—",
                    f"{yr['RATE_NOW']:.2f} so far" if yr is not None else None, delta_color="off",
                    help="Projected year-end recordable rate incl. open first aid cases likely to become recordable "
                         "(Peak glidepath › Year-end injury rates).", **BORDER)
        do = []
        if len(o) and gov.role_allowed(_namespace()):
            top = o.head(3)
            do.append("Check in on first aid cases most likely to become recordable: " + ", ".join(
                f"Incident # {r_['INC']} ({r_['P14'] * 100:.0f}% over up to {int(r_['FORECAST_DAYS'])} days, {r_['ANSWER'].lower()})" for _, r_ in top.iterrows()))
        if nf:
            do.append(f"Reach the {nf} open iCare case{'s' if nf != 1 else ''} with no follow-up yet")
        if status_now in ("Elevated", "Moderate Elevation", "Watch"):
            do.append(f"Work the {status_now} triggers below with owners and dates")
        if do:
            st.markdown("**Today:** " + " · ".join(do) + ".")
        st.caption(f"Bookmark this site: add ?streamlit-site={site} to the end of the app link (…/#/streamlit-apps/"
                   f"EDLDB.SNOP_SANDBOX.MOE_INTELEX_DASHBOARD?streamlit-site={site}). Individual case follow-up takes place in Intelex; the dashboard defaults to aggregate data.")
