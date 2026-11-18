"""Police Stop and Search Explorer.

A Streamlit app for exploring UK police stop and search records published
through the data.police.uk API (https://data.police.uk/docs/).
"""

from __future__ import annotations

import math
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime

import altair as alt
import pandas as pd
import pydeck as pdk
import requests
import streamlit as st

API = "https://data.police.uk/api"
HEADERS = {"User-Agent": "police-stop-and-search-explorer (streamlit app)"}
STOPS_TTL_SECONDS = 24 * 60 * 60
STOPS_MAX_ENTRIES = 300
TREND_WORKERS = 4  # parallel API requests on the Trend tab (the API allows ~15 requests/second)
NOT_RECORDED = "Not recorded"

st.set_page_config(page_title="Stop and Search Explorer", page_icon=":material/local_police:", layout="wide")


# --------------------------------------------------------------------------------------
# Colours: a colour-blind-checked categorical palette, with separate steps for dark mode
# --------------------------------------------------------------------------------------
IS_DARK = (st.context.theme.type or "light") == "dark"
PALETTE = (
    ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c", "#9085e9"]
    if IS_DARK
    else ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6"]
)
SERIES_1 = PALETTE[0]
HEAT_RANGE = ["#1c5cab", "#86b6ef"] if IS_DARK else ["#cde2fb", "#184f95"]
# Ink for numbers printed on heatmap cells: [on low cells, on high cells]
HEAT_INK = ["#ffffff", "#0e1117"] if IS_DARK else ["#262730", "#ffffff"]
# Map heatmap: one blue hue, faint at low density and strongest where searches cluster
MAP_HEAT_COLOURS = (
    [[28, 92, 171, 90], [45, 115, 205, 150], [70, 140, 230, 190], [110, 170, 240, 215], [160, 200, 248, 235],
     [215, 232, 253, 250]]
    if IS_DARK
    else [[134, 182, 239, 90], [90, 150, 225, 150], [52, 118, 205, 190], [32, 92, 175, 215], [24, 72, 140, 235],
          [16, 48, 100, 250]]
)

# Fixed category orders so a category keeps the same colour whatever force/month is shown.
AGE_ORDER = ["under 10", "10-17", "18-24", "25-34", "over 34", NOT_RECORDED]
OBJECT_ORDER = [
    "Controlled drugs",
    "Offensive weapons",
    "Stolen goods",
    "Article for use in theft",
    "Articles for use in criminal damage",
    "Anything to threaten or harm anyone",
    "Firearms",
]
OUTCOME_ORDER = [
    "A no further action disposal",
    "Arrest",
    "Community resolution",
    "Khat or Cannabis warning",
    "Summons / charged by post",
    "Penalty Notice for Disorder",
    "Caution (simple or conditional)",
]
DAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# --------------------------------------------------------------------------------------
# Data access (cached so the API is only hit when something new is requested)
# --------------------------------------------------------------------------------------
def _get_json(path: str, **params):
    """GET an API endpoint, retrying briefly on rate limiting or temporary server errors."""
    for attempt in range(4):
        try:
            res = requests.get(f"{API}/{path}", params=params, headers=HEADERS, timeout=180)
            if res.status_code == 404:
                return []
            if res.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(1.5 * (attempt + 1))
                continue
            res.raise_for_status()
            return res.json()
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 3:
                raise
            time.sleep(1.5 * (attempt + 1))


@st.cache_data(ttl="12h", show_spinner=False)
def get_forces() -> dict[str, str]:
    """Force id -> friendly name."""
    names = {f["id"]: f["name"] for f in _get_json("forces")}
    # British Transport Police publishes stop and search data but isn't in the API's forces list
    names.setdefault("btp", "British Transport Police")
    return names


@st.cache_data(ttl="12h", show_spinner=False)
def get_availability() -> dict[str, list[str]]:
    """Month (YYYY-MM) -> force ids that have published stop and search data, newest first."""
    rows = _get_json("crimes-street-dates")
    avail = {r["date"]: sorted(r.get("stop-and-search") or []) for r in rows}
    return {m: f for m, f in sorted(avail.items(), reverse=True) if f}


@st.cache_resource
def _stops_cache() -> dict:
    """Shared cache of fetched months: {(force, month): (fetched_at, seconds_taken, DataFrame)}.

    Hand-rolled rather than st.cache_data so the app can tell in advance whether a request will be slow,
    show progress while it runs, and fetch several months in parallel threads.
    """
    return {"lock": threading.Lock(), "items": {}}


def is_cached(force: str, month: str) -> bool:
    entry = _stops_cache()["items"].get((force, month))
    return entry is not None and time.time() - entry[0] < STOPS_TTL_SECONDS


def get_stops(force: str, month: str) -> pd.DataFrame:
    """All stop and search records for a force and month, tidied into a flat table. Treat as read-only."""
    cache = _stops_cache()
    entry = cache["items"].get((force, month))
    if entry is not None and time.time() - entry[0] < STOPS_TTL_SECONDS:
        return entry[2]
    started = time.time()
    df = tidy(_get_json("stops-force", force=force, date=month))
    with cache["lock"]:
        items = cache["items"]
        items[(force, month)] = (time.time(), time.time() - started, df)
        while len(items) > STOPS_MAX_ENTRIES:
            items.pop(min(items, key=lambda k: items[k][0]))
    return df


def typical_seconds(force: str) -> float | None:
    """How long recent requests for this force took, used to estimate waiting times."""
    took = [v[1] for k, v in list(_stops_cache()["items"].items()) if k[0] == force]
    return sum(took) / len(took) if took else None


def format_wait(seconds: float) -> str:
    if seconds >= 90:
        return f"about {max(1, round(seconds / 60))} min"
    return f"about {max(5, round(seconds / 5) * 5)} seconds"


def tidy(records: list[dict]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()
    df = pd.json_normalize(records)
    out = pd.DataFrame(
        {
            "datetime": pd.to_datetime(df.get("datetime"), utc=True, errors="coerce").dt.tz_convert("Europe/London"),
            "type": df.get("type"),
            "object_of_search": df.get("object_of_search"),
            "outcome": df.get("outcome"),
            "outcome_linked_to_object_of_search": df.get("outcome_linked_to_object_of_search"),
            "legislation": df.get("legislation"),
            "age_range": df.get("age_range"),
            "gender": df.get("gender"),
            "officer_defined_ethnicity": df.get("officer_defined_ethnicity"),
            "self_defined_ethnicity": df.get("self_defined_ethnicity"),
            "removal_of_more_than_outer_clothing": df.get("removal_of_more_than_outer_clothing"),
            "operation_name": df.get("operation_name"),
            "street": df.get("location.street.name"),
            "lat": pd.to_numeric(df.get("location.latitude"), errors="coerce"),
            "lon": pd.to_numeric(df.get("location.longitude"), errors="coerce"),
        }
    )
    text_cols = [
        "type", "object_of_search", "outcome", "legislation", "age_range",
        "gender", "officer_defined_ethnicity", "self_defined_ethnicity",
    ]
    for col in text_cols:
        out[col] = out[col].astype("string").str.strip().replace("", pd.NA).fillna(NOT_RECORDED)
    return out.sort_values("datetime", ignore_index=True)


def month_label(month: str) -> str:
    return datetime.strptime(month, "%Y-%m").strftime("%B %Y")


# --------------------------------------------------------------------------------------
# Chart helpers (Altair)
# --------------------------------------------------------------------------------------
def fold_categories(series: pd.Series, keep: list[str]) -> pd.Series:
    """Keep the listed categories and group everything else as 'Other'."""
    return series.where(series.isin(keep), "Other")


def ranked_bars(df: pd.DataFrame, field: str, title: str, order: list[str] | None = None, height: int | None = None):
    """Horizontal bar chart of counts per category, biggest first (or in a given order)."""
    counts = df[field].value_counts().rename_axis(field).reset_index(name="searches")
    counts["share"] = counts["searches"] / counts["searches"].sum()
    sort = order if order else "-x"
    return (
        alt.Chart(counts, title=title)
        .mark_bar(color=SERIES_1, cornerRadiusEnd=4)
        .encode(
            x=alt.X("searches:Q", title="Searches"),
            y=alt.Y(f"{field}:N", sort=sort, title=None, axis=alt.Axis(labelLimit=260),
                    scale=alt.Scale(paddingInner=0.3)),
            tooltip=[
                alt.Tooltip(f"{field}:N", title="Category"),
                alt.Tooltip("searches:Q", title="Searches", format=","),
                alt.Tooltip("share:Q", title="Share", format=".1%"),
            ],
        )
        .properties(height=height or max(140, 30 * len(counts)))
    )


SHORT_OUTCOME = {
    "A no further action disposal": "No further action",
    "Khat or Cannabis warning": "Khat/cannabis warning",
    "Summons / charged by post": "Summons",
    "Penalty Notice for Disorder": "Penalty notice",
    "Caution (simple or conditional)": "Caution",
}


def _category_order(values: pd.Series, counts: pd.Series, order: list[str] | None) -> list[str]:
    """The categories present, in the given order (then 'Other'), or biggest first."""
    present = set(values)
    if order:
        return [c for c in order if c in present] + (["Other"] if "Other" in present else [])
    return counts.groupby(values).sum().sort_values(ascending=False).index.tolist()


def share_matrix(df: pd.DataFrame, row_field: str, col_field: str, title: str, *, share_of: str = "col",
                 row_keep: list[str] | None = None, col_keep: list[str] | None = None,
                 row_order: list[str] | None = None, col_order: list[str] | None = None,
                 col_labels: dict[str, str] | None = None, col_sort_by_value: bool = False,
                 col_tips: dict[str, str] | None = None, show_values: bool = True,
                 col_name: str | None = None, share_title: str = "Share"):
    """Heatmap of two categories against each other. Each cell's shade is its share of its column (or row),
    so the mix can be compared across columns without one big category drowning out the rest."""
    data = df[[row_field, col_field]].copy()
    if row_keep:
        data[row_field] = fold_categories(data[row_field], row_keep)
    if col_keep:
        data[col_field] = fold_categories(data[col_field], col_keep)
    # Full grid, so empty combinations show as empty cells rather than gaps
    grid = pd.crosstab(data[row_field], data[col_field]).stack().rename("searches").reset_index()
    group = col_field if share_of == "col" else row_field
    totals = grid.groupby(group)["searches"].transform("sum")
    grid["share"] = (grid["searches"] / totals).fillna(0.0)
    rows = _category_order(grid[row_field], grid["searches"], row_order)
    cols = (sorted(grid[col_field].unique()) if col_sort_by_value
            else _category_order(grid[col_field], grid["searches"], col_order))
    grid["col_label"] = grid[col_field].map(lambda c: (col_labels or {}).get(c, c))
    grid["col_tip"] = grid[col_field].map(lambda c: (col_tips or {}).get(c, c))
    col_label_order = [(col_labels or {}).get(c, c) for c in cols]
    top = max(float(grid["share"].max()), 0.01)
    grid["label"] = grid["share"].map(lambda s: "" if s < 0.005 else f"{s:.0%}")
    grid["strong"] = grid["share"] > top * 0.55

    base = alt.Chart(grid, title=title).encode(
        x=alt.X("col_label:N", sort=col_label_order, title=None,
                axis=alt.Axis(orient="top", labelAngle=0 if len(cols) <= 12 else -45, labelLimit=120,
                              labelOverlap=False, ticks=False, domain=False),
                scale=alt.Scale(paddingInner=0.06)),
        y=alt.Y(f"{row_field}:N", sort=rows, title=None,
                axis=alt.Axis(labelLimit=260, ticks=False, domain=False), scale=alt.Scale(paddingInner=0.06)),
    )
    cells = base.mark_rect(cornerRadius=2).encode(
        color=alt.Color("share:Q", scale=alt.Scale(domain=[0, top], range=HEAT_RANGE),
                        legend=alt.Legend(title=share_title, format="%", orient="bottom", gradientLength=220)),
        tooltip=[
            alt.Tooltip(f"{row_field}:N", title=row_field.replace("_", " ").capitalize()),
            alt.Tooltip("col_tip:N", title=col_name or col_field.replace("_", " ").capitalize()),
            alt.Tooltip("searches:Q", title="Searches", format=","),
            alt.Tooltip("share:Q", title=share_title, format=".1%"),
        ],
    )
    layers = cells
    if show_values:
        text = base.mark_text(fontSize=12).encode(
            text="label:N",
            color=alt.condition("datum.strong", alt.value(HEAT_INK[1]), alt.value(HEAT_INK[0])),
            tooltip=alt.value(None),
        )
        layers = cells + text
    return layers.properties(height=max(160, 34 * len(rows)))


def trend_line(df: pd.DataFrame, x: alt.X, title: str, tooltip: list, height: int = 260):
    """A single-series count over time: a line with a light fill beneath and a marker per period."""
    base = alt.Chart(df, title=title).encode(x=x, y=alt.Y("searches:Q", title="Searches"))
    return (
        base.mark_area(color=SERIES_1, opacity=0.12)
        + base.mark_line(color=SERIES_1, strokeWidth=2)
        + base.mark_point(color=SERIES_1, filled=True, size=60, opacity=1).encode(tooltip=tooltip)
    ).properties(height=height)


def rate(mask: pd.Series) -> float | None:
    return float(mask.mean()) if len(mask) else None


def linked_rate(df: pd.DataFrame) -> float | None:
    """Share of all searches with an outcome linked to the object of search (Home Office style measure).

    The field is normally blank for 'no further action'. Some forces don't fill it in for other outcomes
    either, or record False for everything (even arrests); the figure would mislead then, so return None.
    """
    if df.empty:
        return None
    col = df["outcome_linked_to_object_of_search"]
    acted_on = ~df["outcome"].isin(["A no further action disposal", NOT_RECORDED])
    if acted_on.sum() == 0 or col[acted_on].notna().mean() < 0.5 or not col.fillna(False).astype(bool).any():
        return None
    return float(col.fillna(False).astype(bool).mean())


def fmt_pct(x: float | None) -> str:
    return "–" if x is None or pd.isna(x) else f"{x:.0%}"


def chart(c, legend_rows: int = 0):
    # Streamlit's chart theme squashes charts to a default size unless the element height is given
    # explicitly, so pass the plot height plus room for the title, axis and any bottom legend.
    plot_height = c.height if isinstance(c.height, int) else 300
    st.altair_chart(c, width="stretch", height=plot_height + 90 + 22 * legend_rows)


# --------------------------------------------------------------------------------------
# Sidebar: selections and filters (bound to the URL so a view can be shared as a link)
# --------------------------------------------------------------------------------------
try:
    forces = get_forces()
    availability = get_availability()
except requests.RequestException as exc:
    st.error(f"Couldn't reach the Police Data API right now ({exc}). Please try again in a few minutes.")
    st.stop()

if not availability:
    st.error("The Police Data API didn't return any months with stop and search data.")
    st.stop()

months = list(availability)

with st.sidebar:
    st.title(":material/local_police: Stop and Search Explorer")
    st.caption("Explore stop and search records published by police forces in England, Wales and Northern Ireland.")

    month = st.selectbox("Month", months, format_func=month_label, key="month", bind="query-params")
    all_forces = sorted(set(forces) | {f for fs in availability.values() for f in fs}, key=lambda f: forces.get(f, f))
    force = st.selectbox(
        "Police force",
        all_forces,
        index=all_forces.index("avon-and-somerset") if "avon-and-somerset" in all_forces else 0,
        format_func=lambda f: forces.get(f, f),
        key="force",
        bind="query-params",
        help="Forces upload their stop and search data monthly, and some run a few months behind. If a force "
             "has no data for the chosen month, the app says so.",
    )

# Has this force published data for this month? If not, skip the API call and explain instead.
has_data = force in availability[month]
force_months_all = [m for m in months if force in availability[m]]  # newest first

try:
    if not has_data:
        stops = pd.DataFrame()
    elif is_cached(force, month):
        stops = get_stops(force, month)
    else:
        # The API builds each month's file on request: a second or two for most forces, but 20-30 seconds
        # or more for the largest (e.g. the Met). Nothing arrives until it's done, so show a note and a timer.
        note = st.empty()
        note.info(
            f"**Fetching {forces.get(force, force)} records for {month_label(month)} from the Police Data API.**  \n"
            "Most forces load in a few seconds, but the largest (such as the Metropolitan Police) can take 30 "
            "seconds or more per month while the API prepares the data. Once loaded, it's cached and quick to revisit.",
            icon=":material/hourglass_top:",
        )
        with st.spinner("Waiting for the Police Data API…", show_time=True):
            stops = get_stops(force, month)
        note.empty()
except requests.RequestException as exc:
    st.error(f"The Police Data API request failed ({exc}). Try again, or pick another force or month.")
    st.stop()

with st.sidebar:
    if stops.empty:
        type_filter, object_filter = [], []
    else:
        st.subheader("Filters")
        type_filter = st.multiselect("Search type", sorted(stops["type"].unique()), placeholder="All search types")
        object_filter = st.multiselect(
            "Object of search", sorted(stops["object_of_search"].unique()), placeholder="All objects of search"
        )

    st.divider()
    st.markdown(
        "Data comes directly from the [Police Data API](https://data.police.uk/docs/method/stops-force/) and is as "
        "current as each force's uploads. The API keeps roughly the last three years.\n\n"
        "Created by [Simon Conyard](https://www.linkedin.com/in/simonconyard/)"
    )
    st.caption("Contains public sector information licensed under the Open Government Licence v3.0.")


def apply_filters(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    if type_filter:
        df = df[df["type"].isin(type_filter)]
    if object_filter:
        df = df[df["object_of_search"].isin(object_filter)]
    return df


data = apply_filters(stops)
force_name = forces.get(force, force)

# --------------------------------------------------------------------------------------
# Headline numbers
# --------------------------------------------------------------------------------------
st.title(force_name)
st.caption(f"Stop and search · {month_label(month)}" + (" · filtered" if type_filter or object_filter else ""))

def go_to_month(m: str):
    st.session_state["month"] = m


if not has_data:
    if not force_months_all:
        no_data_msg = (
            f"{force_name} hasn't published any stop and search data to the Police Data API in the last three "
            "years, so there's nothing to show."
        )
    else:
        latest = force_months_all[0]
        no_data_msg = (
            f"No stop and search data is available yet for {force_name} in {month_label(month)}. Forces upload "
            "their data monthly, and some run a few months behind or miss months. "
            f"The latest month with data for {force_name} is {month_label(latest)}."
            if month > latest
            else f"{force_name} didn't publish stop and search data for {month_label(month)}. "
                 "Forces occasionally miss a month, or haven't uploaded it to the Police Data API."
        )
    st.info(no_data_msg, icon=":material/event_busy:")
    if force_months_all:
        target = force_months_all[0] if month > force_months_all[0] else next(
            (m for m in force_months_all if m < month), force_months_all[0])
        st.button(f"Show {month_label(target)}", icon=":material/arrow_forward:", on_click=go_to_month,
                  args=(target,))
elif data.empty:
    no_data_msg = "No stop and search records match the selected filters for this force and month."
    st.info(no_data_msg, icon=":material/filter_alt_off:")

total = len(data)
located = data["lat"].notna() if not data.empty else pd.Series(dtype=bool)

# Previous published month for this force, for a month-on-month comparison. If it isn't cached yet it's
# fetched at the very end of the run, so a slow force doesn't hold up the rest of the page.
prev_month = next((m for m in months if m < month and force in availability[m]), None)


def searches_metric(prev_total: int | None = None, pending: bool = False):
    if prev_total is not None:
        delta = f"{total - prev_total:+,} vs {month_label(prev_month)[:3]}"
    elif pending:
        delta = f"loading {month_label(prev_month)[:3]}…"
    else:
        delta = None
    k1_slot.metric("Searches", f"{total:,}", delta=delta, delta_color="off", border=True,
                   delta_arrow="off" if pending else "auto")


if data.empty:
    prev_month = None  # nothing to compare
else:
    k1, k2, k3, k4, k5 = st.columns(5)
    k1_slot = k1.empty()
    if prev_month and is_cached(force, prev_month):
        searches_metric(len(apply_filters(get_stops(force, prev_month))))
    else:
        searches_metric(pending=bool(prev_month))
    k2.metric("Arrest rate", fmt_pct(rate(data["outcome"].eq("Arrest"))), border=True,
              help="Share of searches with an outcome of arrest.")
    k3.metric("No further action", fmt_pct(rate(data["outcome"].eq("A no further action disposal"))), border=True,
              help="Share of searches where no further action was taken.")
    linked = linked_rate(data)
    k4.metric("Linked outcome", "n/a" if linked is None else fmt_pct(linked), border=True,
              help="Share of searches where the outcome was linked to what the officer was searching for (e.g. a "
                   "drugs search that found drugs). Shown as n/a when this force doesn't reliably record it.")
    k5.metric("With a location", fmt_pct(rate(located)), border=True,
              help="Some records are published without a location and can't be placed on the map.")

# --------------------------------------------------------------------------------------
# Tabs (only the open tab runs, and the open tab is kept in the URL)
# --------------------------------------------------------------------------------------
tab_map, tab_people, tab_reasons, tab_time, tab_trend, tab_data = st.tabs(
    ["Map", "Who was searched", "Reasons & outcomes", "When", "Trend", "Data"],
    key="view", on_change="rerun", bind="query-params",
)


def tab_ready(tab) -> bool:
    """True if the tab is open and there's data to draw; otherwise show a short message in the tab."""
    if not tab.open:
        return False
    if data.empty:
        with tab:
            if has_data:
                st.caption("No records match the selected filters.")
            elif force_months_all:
                st.caption("No data available yet for this force and month.")
            else:
                st.caption("This force doesn't publish stop and search data to the Police Data API.")
        return False
    return True


with tab_map:
    if tab_ready(tab_map):
        pts = data.dropna(subset=["lat", "lon"]).copy()
        if pts.empty:
            st.info("None of these records include a location, so there's nothing to map.")
        else:
            style = st.segmented_control("Map style", ["Heatmap", "Hexagons", "Points"], default="Heatmap",
                                         required=True, label_visibility="collapsed", key="map_style",
                                         bind="query-params")
            # Centre and zoom on the bulk of the data (ignoring stray outliers)
            lat_lo, lat_hi = pts["lat"].quantile([0.05, 0.95])
            lon_lo, lon_hi = pts["lon"].quantile([0.05, 0.95])
            span = max(lat_hi - lat_lo, (lon_hi - lon_lo) * 0.6, 0.02)
            zoom = min(13.0, max(5.0, math.log2(360 / span) - 0.6))
            pts["when"] = pts["datetime"].dt.strftime("%a %d %b %H:%M")
            pts = pts.fillna({"street": "Location withheld"})
            tooltip_cols = ["lat", "lon", "street", "when", "type", "object_of_search", "outcome"]
            if style == "Heatmap":
                layer = pdk.Layer(
                    "HeatmapLayer", data=pts[["lat", "lon"]], get_position="[lon, lat]", get_weight=1,
                    aggregation="SUM", radius_pixels=40, intensity=1, threshold=0.05,
                    color_range=MAP_HEAT_COLOURS,
                )
                tooltip = False  # a density surface has no individual records to describe
                pitch = 0
            elif style == "Hexagons":
                layer = pdk.Layer(
                    "HexagonLayer", data=pts[["lat", "lon"]], get_position="[lon, lat]",
                    radius=max(150, int(400 / 2 ** (zoom - 9))), elevation_scale=6, elevation_range=[0, 1500],
                    extruded=True, pickable=True, coverage=0.9,
                )
                tooltip = {"text": "{elevationValue} searches in this area"}
                pitch = 40
            else:
                layer = pdk.Layer(
                    "ScatterplotLayer", data=pts[tooltip_cols], get_position="[lon, lat]",
                    get_fill_color=[214, 64, 52, 170], get_radius=60, radius_min_pixels=3, radius_max_pixels=12,
                    pickable=True,
                )
                tooltip = {"html": "<b>{street}</b><br/>{when}<br/>{type} · {object_of_search}<br/>Outcome: {outcome}"}
                pitch = 0
            st.pydeck_chart(
                pdk.Deck(
                    map_style=None,  # follow the Streamlit light/dark theme
                    initial_view_state=pdk.ViewState(
                        latitude=float(pts["lat"].median()), longitude=float(pts["lon"].median()),
                        zoom=zoom, pitch=pitch,
                    ),
                    layers=[layer],
                    tooltip=tooltip,
                ),
                height=560,
            )
            missing = total - len(pts)
            st.caption(
                ("Darker areas had more searches; switch to Hexagons for counts or Points for individual records. "
                 if style == "Heatmap" else "")
                + "Locations are approximate: the API snaps each one to a nearby anonymised point."
                + (f" {missing:,} record(s) had no location and aren't shown." if missing else "")
            )

with tab_people:
    if tab_ready(tab_people):
        c1, c2 = st.columns(2)
        with c1:
            chart(ranked_bars(data, "age_range", "Age range", order=AGE_ORDER))
        with c2:
            chart(ranked_bars(data, "gender", "Gender"))
        eth_kind = st.segmented_control(
            "Ethnicity as", ["Officer-defined", "Self-defined"], default="Officer-defined", required=True,
            help="Officer-defined ethnicity is the officer's perception; self-defined is what the person stated.",
        )
        eth_field = "officer_defined_ethnicity" if eth_kind == "Officer-defined" else "self_defined_ethnicity"
        chart(ranked_bars(data, eth_field, f"Ethnicity ({eth_kind.lower()})"))
        st.caption(
            "Counts only: comparing groups fairly needs local population figures (e.g. the Census), "
            "which aren't part of this data."
        )
        chart(share_matrix(data, "object_of_search", "age_range", "What people were searched for, by age range",
                           share_of="col", row_keep=OBJECT_ORDER, row_order=OBJECT_ORDER, col_order=AGE_ORDER,
                           col_name="Age range", share_title="Share of age range"), legend_rows=2)
        st.caption("Each column adds up to 100%: the mix of reasons for searching people in that age range.")

with tab_reasons:
    if tab_ready(tab_reasons):
        c1, c2 = st.columns(2)
        with c1:
            chart(ranked_bars(data, "object_of_search", "Object of search"))
        with c2:
            chart(ranked_bars(data, "outcome", "Outcome"))
        chart(share_matrix(data, "object_of_search", "outcome", "Outcome by object of search", share_of="row",
                           col_keep=OUTCOME_ORDER, col_order=OUTCOME_ORDER, col_labels=SHORT_OUTCOME,
                           share_title="Share of object's searches"),
              legend_rows=2)
        st.caption("Each row adds up to 100%: what happened after searches for that object.")
        c3, c4 = st.columns(2)
        with c3:
            chart(ranked_bars(data, "legislation", "Legislation used"))
        with c4:
            chart(ranked_bars(data, "type", "Search type"))

with tab_time:
    if tab_ready(tab_time):
        timed = data.dropna(subset=["datetime"]).copy()
        timed["day"] = pd.Categorical(timed["datetime"].dt.strftime("%a"), DAY_ORDER)
        timed["hour"] = timed["datetime"].dt.hour
        grid = (
            timed.groupby(["day", "hour"], observed=False).size().reset_index(name="searches")
        )
        heat = (
            alt.Chart(grid, title="Searches by day of week and hour (UK time)")
            .mark_rect(cornerRadius=2, stroke=None)
            .encode(
                x=alt.X("hour:O", title="Hour of day"),
                y=alt.Y("day:O", sort=DAY_ORDER, title=None),
                color=alt.Color("searches:Q", scale=alt.Scale(range=HEAT_RANGE), legend=alt.Legend(title="Searches")),
                tooltip=[alt.Tooltip("day:O", title="Day"), alt.Tooltip("hour:O", title="Hour"),
                         alt.Tooltip("searches:Q", title="Searches")],
            )
            .properties(height=260)
        )
        chart(heat)
        daily = timed.groupby(timed["datetime"].dt.date).size().rename_axis("date").reset_index(name="searches")
        daily["date"] = pd.to_datetime(daily["date"])
        chart(trend_line(daily, alt.X("date:T", title=None, axis=alt.Axis(format="%d %b")), "Searches per day",
                         [alt.Tooltip("date:T", title="Date", format="%a %d %b %Y"),
                          alt.Tooltip("searches:Q", title="Searches")], height=240))

with tab_trend:
    if tab_trend.open:
        force_months = [m for m in months if m <= month and force in availability[m]]
        if len(force_months) > 3:
            n = st.slider("Months to include", 3, len(force_months), min(12, len(force_months)),
                          help="Counted back from the selected month. Each month is a separate API request the "
                               "first time it's loaded, so longer ranges take longer for large forces.")
        else:
            n = len(force_months)
            if n:
                st.caption(f"Only {n} month(s) of data are available up to {month_label(month)}.")
        wanted = list(reversed(force_months[:n]))
        to_fetch = [m for m in wanted if not is_cached(force, m)]
        failed = []
        if to_fetch:
            per_month = typical_seconds(force) or 5
            estimate = math.ceil(len(to_fetch) / TREND_WORKERS) * per_month
            note = st.empty()
            if estimate > 20:
                note.info(
                    f"Loading {len(to_fetch)} months of {force_name} data. Each month takes "
                    f"{format_wait(per_month)} for this force, so this should take **{format_wait(estimate)}**. "
                    "Months load in parallel and are cached afterwards.",
                    icon=":material/hourglass_top:",
                )
            progress = st.progress(0.0, text=f"Loading {len(to_fetch)} months…")
            started = time.time()
            with ThreadPoolExecutor(max_workers=TREND_WORKERS) as pool:
                futures = {pool.submit(get_stops, force, m): m for m in to_fetch}
                pending = set(futures)
                while pending:
                    # Wake at least once a second so the elapsed time keeps ticking during slow requests
                    finished, pending = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
                    failed += [futures[f] for f in finished if f.exception() is not None]
                    done = len(to_fetch) - len(pending)
                    progress.progress(
                        done / len(to_fetch),
                        text=f"Loaded {done} of {len(to_fetch)} months · {time.time() - started:.0f}s elapsed",
                    )
            progress.empty()
            note.empty()
        frames = [apply_filters(get_stops(force, m)).assign(month=m) for m in wanted if m not in failed]
        if failed:
            st.warning("Couldn't load: " + ", ".join(month_label(m) for m in failed))
        trend = pd.concat([f for f in frames if not f.empty], ignore_index=True) if frames else pd.DataFrame()
        if trend.empty:
            st.caption("No data available for this force up to this month." if not force_months
                       else "No records match the selected filters in this range.")
        else:
            trend["month_start"] = pd.to_datetime(trend["month"])
            month_axis = alt.X("label:O", title=None, sort=None, axis=alt.Axis(labelAngle=0))
            monthly = pd.DataFrame(
                [
                    {"month_start": m, "searches": len(g), "arrest": g["outcome"].eq("Arrest").mean(),
                     "linked": linked_rate(g)}
                    for m, g in trend.groupby("month_start")
                ]
            )
            monthly["label"] = monthly["month_start"].dt.strftime("%b %y")
            chart(trend_line(monthly, month_axis, "Searches per month",
                             [alt.Tooltip("label:O", title="Month"),
                              alt.Tooltip("searches:Q", title="Searches", format=",")]))
            rates = monthly.melt(["month_start", "label"], ["arrest", "linked"], var_name="measure", value_name="rate").dropna()
            rates["measure"] = rates["measure"].map({"arrest": "Arrest rate", "linked": "Linked outcome"})
            base = alt.Chart(rates, title="Arrest rate and linked-outcome rate").encode(
                x=month_axis,
                y=alt.Y("rate:Q", title=None, axis=alt.Axis(format="%")),
                color=alt.Color("measure:N", scale=alt.Scale(domain=["Arrest rate", "Linked outcome"],
                                                             range=PALETTE[:2]),
                                legend=alt.Legend(title=None, orient="bottom")),
                tooltip=[alt.Tooltip("label:O", title="Month"),
                         alt.Tooltip("measure:N", title="Measure"), alt.Tooltip("rate:Q", title="Rate", format=".1%")],
            )
            chart((base.mark_line(strokeWidth=2) + base.mark_point(size=60, filled=True)).properties(height=260), legend_rows=1)
            # Month labels sort chronologically as "YYYY-MM", then are shown as "Jan 26"
            obj_src = trend.assign(month_key=trend["month"])
            chart(
                share_matrix(obj_src, "object_of_search", "month_key", "Object of search by month",
                             share_of="col", row_keep=OBJECT_ORDER, row_order=OBJECT_ORDER, col_sort_by_value=True,
                             col_labels={m: month_label(m)[:3] + " " + m[2:4] for m in wanted},
                             col_tips={m: month_label(m) for m in wanted}, col_name="Month",
                             show_values=len(wanted) <= 12, share_title="Share of month"),
                legend_rows=2,
            )
            st.caption("Each column adds up to 100%: the mix of reasons for searching in that month. "
                       "Hover a cell for the number of searches.")
            with st.expander("Monthly figures as a table"):
                st.dataframe(
                    monthly.assign(month=monthly["month_start"].dt.strftime("%B %Y"))[
                        ["month", "searches", "arrest", "linked"]],
                    hide_index=True,
                    column_config={
                        "month": "Month",
                        "searches": st.column_config.NumberColumn("Searches", format="localized"),
                        "arrest": st.column_config.NumberColumn("Arrest rate", format="percent"),
                        "linked": st.column_config.NumberColumn("Linked outcome", format="percent"),
                    },
                )

with tab_data:
    if tab_ready(tab_data):
        st.dataframe(
            data,
            hide_index=True,
            column_config={
                "datetime": st.column_config.DatetimeColumn("Date/time", format="ddd D MMM YYYY, HH:mm"),
                "type": "Type",
                "object_of_search": "Object of search",
                "outcome": "Outcome",
                "outcome_linked_to_object_of_search": st.column_config.CheckboxColumn("Outcome linked"),
                "legislation": "Legislation",
                "age_range": "Age range",
                "gender": "Gender",
                "officer_defined_ethnicity": "Ethnicity (officer)",
                "self_defined_ethnicity": "Ethnicity (self-defined)",
                "removal_of_more_than_outer_clothing": st.column_config.CheckboxColumn("More than outer clothing"),
                "operation_name": "Operation",
                "street": "Street",
                "lat": st.column_config.NumberColumn("Latitude", format="%.5f"),
                "lon": st.column_config.NumberColumn("Longitude", format="%.5f"),
            },
        )
        export = data.copy()
        export["datetime"] = export["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S%z")
        st.download_button(
            "Download as CSV",
            export.to_csv(index=False).encode("utf-8"),
            file_name=f"stop-and-search_{force}_{month}.csv",
            mime="text/csv",
            icon=":material/download:",
        )
        api_url = f"{API}/stops-force?force={force}&date={month}"
        st.caption(f"Source query: [{api_url}]({api_url})")


# --------------------------------------------------------------------------------------
# Finally, fill in the month-on-month comparison if it wasn't already cached
# --------------------------------------------------------------------------------------
if prev_month and not is_cached(force, prev_month):
    try:
        searches_metric(len(apply_filters(get_stops(force, prev_month))))
    except requests.RequestException:
        searches_metric()
