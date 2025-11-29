import os
from datetime import datetime, timezone
from typing import List, Dict

import requests
import streamlit as st
from dotenv import load_dotenv

# ---------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------

load_dotenv()

# Two site-specific ICS feeds
ICS_URL_DUBLIN15 = os.getenv("WIW_ICS_URL_DUBLIN15")
ICS_URL_ESPOO = os.getenv("WIW_ICS_URL_ESPOO")

# If in future you add more sites, you can optionally put their ICS URLs
# here as a comma-separated list in .env: WIW_EXTRA_ICS_URLS="url1,url2"
EXTRA_ICS_URLS = [
    u.strip()
    for u in os.getenv("WIW_EXTRA_ICS_URLS", "").split(",")
    if u.strip()
]

# Combine all configured ICS URLs
ICS_URLS = [u for u in [ICS_URL_DUBLIN15, ICS_URL_ESPOO] if u] + EXTRA_ICS_URLS

# Local timezone label for display only (we still use UTC internally)
LOCAL_TZ_LABEL = "Europe/Dublin"

# If you ever want to limit which schedules are shown, you can set:
# WIW_ALLOWED_SCHEDULES="Dublin 15 Operations Schedule,Espoo Operations Schedule"
_allowed = os.getenv("WIW_ALLOWED_SCHEDULES")
ALLOWED_SCHEDULES = (
    {s.strip() for s in _allowed.split(",") if s.strip()} if _allowed else None
)

# Icons per schedule (others fall back to 📍)
SCHEDULE_ICONS = {
    "Dublin 15 Operations Schedule": "🇮🇪",
    "Espoo Operations Schedule": "🇫🇮",
}

# ---------------------------------------------------------------------
# STYLING
# ---------------------------------------------------------------------


def inject_css():
    """Single soft background + simple cards, no dark mode."""
    css = """
    <style>
    .stApp {
        background-color: #f5f5f9;
    }

    .main .block-container {
        padding-top: 1.5rem;
        padding-bottom: 2rem;
    }

    .role-header {
        font-weight: 600;
        font-size: 0.85rem;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        margin-bottom: 0.35rem;
        border-radius: 0.65rem;
        padding: 0.45rem 0.9rem;
    }

    .person-card {
        background-color: #ffffff;
        border-radius: 0.6rem;
        padding: 0.45rem 0.8rem;
        margin-bottom: 0.35rem;
        box-shadow: 0 1px 2px rgba(15, 23, 42, 0.06);
        border: 1px solid #e5e7eb;
        font-size: 0.95rem;
    }

    .status-pill {
        display: inline-block;
        margin-top: 0.2rem;
        padding: 0.1rem 0.55rem;
        border-radius: 999px;
        font-size: 0.75rem;
        font-weight: 500;
    }

    .muted-text {
        color: #6b7280;
        font-size: 0.9rem;
    }
    </style>
    """
    st.markdown(css, unsafe_allow_html=True)


# ---------------------------------------------------------------------
# ICS PARSING
# ---------------------------------------------------------------------


def parse_ics_datetime(dt_str: str) -> datetime:
    """Parse strings like 20251201T080000Z into timezone-aware UTC datetimes."""
    dt = datetime.strptime(dt_str, "%Y%m%dT%H%M%SZ")
    return dt.replace(tzinfo=timezone.utc)


def parse_events_from_ics(ics_text: str) -> List[Dict]:
    """
    Read BEGIN:VEVENT ... END:VEVENT blocks and extract
    DTSTART, DTEND, SUMMARY.
    """
    events = []
    current = None

    for raw_line in ics_text.splitlines():
        line = raw_line.strip()

        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            if current:
                events.append(current)
            current = None
        elif current is not None:
            if line.startswith("DTSTART:"):
                current["start"] = parse_ics_datetime(line[len("DTSTART:") :])
            elif line.startswith("DTEND:"):
                current["end"] = parse_ics_datetime(line[len("DTEND:") :])
            elif line.startswith("SUMMARY:"):
                current["summary"] = line[len("SUMMARY:") :]

    return events


def parse_summary(summary: str):
    """
    SUMMARY example:
      'Shauna Brady (Shift as Mission Control (MC) at MANNA HQ at Dublin 15 Operations Schedule)'

    We want:
      name     -> 'Shauna Brady'
      role     -> 'Mission Control (MC)'
      schedule -> 'Dublin 15 Operations Schedule'

    If the pattern doesn't match cleanly, we just skip that event
    (so you don't get an 'Unknown location' bucket).
    """
    if " (Shift as " not in summary:
        return None

    try:
        name_part, rest = summary.split(" (Shift as ", 1)
        rest = rest.rstrip(")")
        parts = [p.strip() for p in rest.split(" at ")]
        if len(parts) < 2:
            return None

        role = parts[0]
        schedule = parts[-1]

        return {
            "name": name_part.strip(),
            "role": role,
            "schedule": schedule,
        }
    except Exception:
        return None


def classify_role(role: str) -> str:
    """
    Put roles into buckets: MC, Pilot, Other.
    (We still display the full role text beside the name.)
    """
    r = role.lower()
    if "mission control" in r or "(mc)" in r:
        return "MC"
    if "flight operator" in r or "(fo)" in r or "pilot" in r:
        return "Pilot"
    return "Other"


# ---------------------------------------------------------------------
# DATA FETCHING
# ---------------------------------------------------------------------


@st.cache_data(ttl=60)
def fetch_ics(url: str) -> str:
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.text


def get_active_shifts(now_utc: datetime) -> Dict[str, Dict[str, List[Dict]]]:
    """
    Returns:
    {
      'Dublin 15 Operations Schedule': {'MC': [...], 'Pilot': [...], 'Other': [...]},
      'Espoo Operations Schedule': {...},
      ...
    }
    Only active shifts (now between start and end) are included in the lists.
    Schedules with no current shifts still appear with empty lists.
    """
    if not ICS_URLS:
        raise RuntimeError(
            "No ICS URLs configured. Set WIW_ICS_URL_DUBLIN15 / WIW_ICS_URL_ESPOO "
            "in your .env file."
        )

    schedules: Dict[str, Dict[str, List[Dict]]] = {}

    for url in ICS_URLS:
        ics_text = fetch_ics(url)
        events = parse_events_from_ics(ics_text)

        for ev in events:
            start = ev.get("start")
            end = ev.get("end")
            summary = ev.get("summary", "")
            if not start or not end:
                continue

            info = parse_summary(summary)
            if not info:
                # Skip anything we can't cleanly parse to avoid 'Unknown' sites
                continue

            schedule_name = info["schedule"]

            if ALLOWED_SCHEDULES and schedule_name not in ALLOWED_SCHEDULES:
                continue

            # Ensure schedule exists in dict even if nobody active
            if schedule_name not in schedules:
                schedules[schedule_name] = {"MC": [], "Pilot": [], "Other": []}

            # Only active shifts
            if not (start <= now_utc <= end):
                continue

            bucket = classify_role(info["role"])

            schedules[schedule_name][bucket].append(
                {
                    "name": info["name"],
                    "role": info["role"],
                    "start": start,
                    "end": end,
                }
            )

    # Sort names in each bucket for neatness
    for sched_data in schedules.values():
        for key in ["MC", "Pilot", "Other"]:
            sched_data[key].sort(key=lambda x: x["name"])

    return schedules


# ---------------------------------------------------------------------
# UI HELPERS
# ---------------------------------------------------------------------


def prettify_schedule_name(schedule_name: str) -> str:
    """Shorten 'Dublin 15 Operations Schedule' -> 'Dublin 15'."""
    suffix = "Operations Schedule"
    if schedule_name.endswith(suffix):
        return schedule_name[: -len(suffix)].rstrip()
    return schedule_name


def build_status_badge(end_dt: datetime, now_utc: datetime) -> str:
    """Return HTML badge 'On until 21:30' etc."""
    remaining = end_dt - now_utc
    minutes = int(remaining.total_seconds() // 60)

    end_time_str = end_dt.strftime("%H:%M")

    if minutes <= 0:
        emoji = "⚪"
        bg = "#E5E7EB"
        text = "Ending now"
    elif minutes < 30:
        emoji = "🔴"
        bg = "#FEE2E2"
        text = f"Ends in {minutes} min"
    elif minutes < 120:
        emoji = "🟡"
        bg = "#FEF3C7"
        text = f"On until {end_time_str}"
    else:
        emoji = "🟢"
        bg = "#DCFCE7"
        text = f"On until {end_time_str}"

    return f'<span class="status-pill" style="background-color:{bg};">{emoji} {text}</span>'


def filter_people(people: List[Dict], query: str) -> List[Dict]:
    q = (query or "").strip().lower()
    if not q:
        return people
    return [
        p
        for p in people
        if q in p["name"].lower() or q in p["role"].lower()
    ]


def render_people_list(people: List[Dict], now_utc: datetime):
    if not people:
        st.markdown('<div class="muted-text"><em>None</em></div>', unsafe_allow_html=True)
        return

    for p in people:
        badge_html = build_status_badge(p["end"], now_utc)
        html = f"""
        <div class="person-card">
            <div>
                <span style="font-weight:600;">{p['name']}</span>
                <span style="color:#6b7280;">&nbsp;({p['role']})</span>
            </div>
            <div style="margin-top:0.1rem;">{badge_html}</div>
        </div>
        """
        st.markdown(html, unsafe_allow_html=True)


def render_role_column(
    label: str,
    people: List[Dict],
    color: str,
    now_utc: datetime,
    expandable: bool = False,
):
    header_html = f"""
    <div class="role-header" style="background-color:{color};">
        {label}
    </div>
    """
    st.markdown(header_html, unsafe_allow_html=True)

    if expandable:
        with st.expander(f"{label} ({len(people)})", expanded=False):
            render_people_list(people, now_utc)
    else:
        render_people_list(people, now_utc)


# ---------------------------------------------------------------------
# MAIN APP
# ---------------------------------------------------------------------


def main():
    st.set_page_config(
        page_title="Who’s On Shift – MCs in Dublin 15 & Espoo",
        layout="wide",
    )

    inject_css()

    now_utc = datetime.now(timezone.utc)

    # ----- VERY TOP TITLE (this is the one that appears under "Deploy") -----
    st.title("Who’s On Shift – MCs in 🇮🇪 Dublin 15 & 🇫🇮 Espoo")
    st.caption(
        f"Current time (UTC): {now_utc.strftime('%Y-%m-%d %H:%M:%S')}  |  Local zone label: {LOCAL_TZ_LABEL}"
    )

    # Controls row
    controls_left, controls_right = st.columns([4, 1])
    with controls_right:
        if st.button("Refresh now"):
            st.cache_data.clear()
            st.rerun()

    # Load data
    try:
        schedules = get_active_shifts(now_utc)
    except Exception as e:
        st.error(f"Error fetching or parsing schedule: {e}")
        return

    if not schedules:
        st.info("No schedules were found in the ICS feeds.")
        return

    schedule_names = sorted(schedules.keys())

    # ---- Filters ----
    options = ["All locations"] + schedule_names

    def format_site(opt: str) -> str:
        if opt == "All locations":
            return "🌍 All locations"
        icon = SCHEDULE_ICONS.get(opt, "📍")
        pretty = prettify_schedule_name(opt)
        return f"{icon} {pretty}"

    selected = st.selectbox("Select site", options, index=0, format_func=format_site)

    search_query = st.text_input(
        "Search by name or role",
        "",
        placeholder="Type to filter (e.g. 'Darragh', 'MC', 'Flight Operator')",
    )

    if selected == "All locations":
        schedules_to_show = schedule_names
    else:
        schedules_to_show = [selected]

    any_data = False

    for schedule_name in schedules_to_show:
        st.markdown("---")
        icon = SCHEDULE_ICONS.get(schedule_name, "📍")
        pretty_name = prettify_schedule_name(schedule_name)
        st.subheader(f"{icon} {pretty_name}")

        sched_data = schedules.get(schedule_name, {"MC": [], "Pilot": [], "Other": []})

        mc_list = filter_people(sched_data["MC"], search_query)
        pilot_list = filter_people(sched_data["Pilot"], search_query)
        other_list = filter_people(sched_data["Other"], search_query)

        if mc_list or pilot_list or other_list:
            any_data = True

        # Summary chips
        summary_html = f"""
        <div style="display:flex; gap:0.5rem; font-size:0.85rem; margin-bottom:0.8rem; flex-wrap:wrap;">
            <div style="background:#DCFCE7; padding:0.15rem 0.7rem; border-radius:999px;">🟢 MC: {len(mc_list)}</div>
            <div style="background:#DBEAFE; padding:0.15rem 0.7rem; border-radius:999px;">🟦 Pilot: {len(pilot_list)}</div>
            <div style="background:#E5E7EB; padding:0.15rem 0.7rem; border-radius:999px;">⚪ Other: {len(other_list)}</div>
        </div>
        """
        st.markdown(summary_html, unsafe_allow_html=True)

        col1, col2, col3 = st.columns(3)
        with col1:
            render_role_column("MC", mc_list, "#E8F5E9", now_utc)
        with col2:
            render_role_column("Pilot", pilot_list, "#E3F2FD", now_utc)
        with col3:
            render_role_column("Other roles", other_list, "#F5F5F5", now_utc, expandable=True)

    if not any_data:
        st.info(
            "No one is currently on shift in the selected site(s), "
            "based on the current When I Work schedules."
        )


if __name__ == "__main__":
    main()
