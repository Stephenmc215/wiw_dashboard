import os
from datetime import datetime, timezone
from typing import Dict, List

import requests
import streamlit as st
from dotenv import load_dotenv

# Load .env locally (Streamlit Cloud will inject env vars via Secrets)
load_dotenv()

# ----------------- CONFIG ----------------- #

SITE_CONFIG = {
    "dublin15": {
        "env_var": "WIW_ICS_URL_DUBLIN15",
        "label": "IE Dublin 15",
        "flag": "🇮🇪",
    },
    "espoo": {
        "env_var": "WIW_ICS_URL_ESPOO",
        "label": "FI Espoo",
        "flag": "🇫🇮",
    },
}


# ----------------- ICS PARSING ----------------- #


def parse_ics_datetime(dt_str: str) -> datetime:
    """Parse strings like 20251201T080000Z into timezone-aware UTC datetimes."""
    dt = datetime.strptime(dt_str, "%Y%m%dT%H%M%SZ")
    return dt.replace(tzinfo=timezone.utc)


def parse_events_from_ics(ics_text: str) -> List[Dict]:
    """
    Read BEGIN:VEVENT ... END:VEVENT blocks and extract
    DTSTART, DTEND, SUMMARY, LOCATION.
    """
    events: List[Dict] = []
    current: Dict | None = None

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
            elif line.startswith("LOCATION:"):
                loc = line[len("LOCATION:") :]
                current["location_raw"] = loc

    return events


def extract_name_and_role(summary: str) -> Dict[str, str]:
    """
    SUMMARY example:
      'Stephen McSherry (Shift as Mission Control (MC) at MANNA HQ at Dublin 15 Operations Schedule)'
    Returns: {"name": ..., "role": ...}
    """
    name = summary
    role = "Unknown"

    if " (Shift as " in summary:
        name_part, rest = summary.split(" (Shift as ", 1)
        name = name_part.strip()

        if " at " in rest:
            role_part, _ = rest.split(" at ", 1)
        else:
            role_part = rest.rstrip(")")

        role = role_part.strip()

    return {"name": name, "role": role}


def classify_role(role: str) -> str:
    """
    Put roles into buckets: MC, Pilot, Other (but keep real role text).
    """
    r = role.lower()
    if "mission control" in r or "(mc)" in r:
        return "MC"
    if "flight operator" in r or "(fo)" in r or "pilot" in r:
        return "Pilot"
    return "Other"


# ----------------- DATA FETCHING ----------------- #


def load_active_sites() -> Dict[str, Dict]:
    """Return only sites that have an ICS URL configured."""
    active: Dict[str, Dict] = {}
    for site_id, cfg in SITE_CONFIG.items():
        url = os.getenv(cfg["env_var"])
        if url:
            active[site_id] = {**cfg, "url": url}
    return active


ACTIVE_SITES = load_active_sites()


@st.cache_data(ttl=60)
def fetch_ics(site_id: str) -> str:
    cfg = ACTIVE_SITES[site_id]
    url = cfg["url"]
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.text


def get_active_shifts(now_utc: datetime) -> Dict[str, Dict]:
    """
    Group active shifts by site and role.

    Returns:
    {
      "dublin15": {
         "meta": {label, flag, ...},
         "roles": {"MC": [...], "Pilot": [...], "Other": [...]}
      },
      ...
    }
    """
    results: Dict[str, Dict] = {}

    for site_id, meta in ACTIVE_SITES.items():
        ics_text = fetch_ics(site_id)
        events = parse_events_from_ics(ics_text)

        roles: Dict[str, List[Dict]] = {"MC": [], "Pilot": [], "Other": []}

        for ev in events:
            start = ev.get("start")
            end = ev.get("end")
            summary = ev.get("summary", "")

            if not start or not end:
                continue

            # Only keep shifts active right now (in UTC)
            if not (start <= now_utc <= end):
                continue

            info = extract_name_and_role(summary)
            bucket = classify_role(info["role"])

            roles[bucket].append(
                {
                    "name": info["name"],
                    "role": info["role"],
                    "start": start,
                    "end": end,
                }
            )

        # Sort names for neatness
        for key in roles:
            roles[key].sort(key=lambda x: x["name"])

        results[site_id] = {"meta": meta, "roles": roles}

    return results


# ----------------- UI HELPERS ----------------- #


def format_end_time_local(end_utc: datetime) -> str:
    """Return local time as HH:MM (24h)."""
    local_dt = end_utc.astimezone()
    return local_dt.strftime("%H:%M")


def render_person_card(person: Dict):
    """Small white card for a single person."""
    name = person["name"]
    role = person["role"]
    end_label = format_end_time_local(person["end"])

    card_html = f"""
    <div style="
        padding:0.70rem 0.95rem;
        border-radius:0.75rem;
        background-color:#ffffff;
        margin-top:0.40rem;
        box-shadow:0 0 0 1px #e5e7eb;
    ">
      <div style="font-weight:600; color:#111827; font-size:0.95rem;">{name}</div>
      <div style="font-size:0.85rem; color:#4b5563; margin-top:0.15rem;">{role}</div>
      <div style="font-size:0.80rem; color:#047857; margin-top:0.35rem;">
        <span style="font-size:0.7rem;">●</span> On until {end_label}
      </div>
    </div>
    """
    st.markdown(card_html, unsafe_allow_html=True)


def render_role_column(title: str, colour: str, people: List[Dict], is_other: bool = False):
    """
    Render MC / Pilot column, or an 'Other roles' expander.
    colour = background colour of the header bar.
    """
    header_html = f"""
    <div style="
        padding:0.40rem 0.85rem;
        border-radius:0.80rem;
        background-color:{colour};
        font-weight:600;
        font-size:0.86rem;
        color:#0f172a;
        margin-bottom:0.40rem;
        border:1px solid rgba(148,163,184,0.6);
    ">
      {title}
    </div>
    """

    if is_other:
        count = len(people)
        with st.expander(f"Other roles ({count})", expanded=False):
            st.markdown(header_html, unsafe_allow_html=True)
            if not people:
                st.caption("None on shift")
            else:
                for p in people:
                    render_person_card(p)
    else:
        st.markdown(header_html, unsafe_allow_html=True)
        if not people:
            st.caption("None on shift")
        else:
            for p in people:
                render_person_card(p)


def apply_search_filter(
    all_sites: Dict[str, Dict], search_text: str
) -> Dict[str, Dict]:
    """Filter people by name/role across all sites."""
    if not search_text:
        return all_sites

    search = search_text.lower().strip()
    filtered: Dict[str, Dict] = {}

    for site_id, site_data in all_sites.items():
        roles = site_data["roles"]
        new_roles: Dict[str, List[Dict]] = {}
        for bucket, people in roles.items():
            new_roles[bucket] = [
                p
                for p in people
                if search in p["name"].lower() or search in p["role"].lower()
            ]
        # Only keep site if at least one person matches
        if any(new_roles[b] for b in new_roles):
            filtered[site_id] = {"meta": site_data["meta"], "roles": new_roles}

    return filtered


# ----------------- MAIN APP ----------------- #


def main():
    st.set_page_config(
        page_title="Who’s on shift?", layout="wide"
    )

    # Soft background + hide Streamlit chrome
    st.markdown(    """
    <style>
    .title-underline {
        font-size: 2rem;
        font-weight: 700;
        border-bottom: 3px solid #6366f1; /* indigo */
        padding-bottom: 0.4rem;
        margin-bottom: 1rem;
    }
    </style>
    <div class="title-underline">Who’s on shift?</div>
    """,
    unsafe_allow_html=True,
    )

    if not ACTIVE_SITES:
        st.error(
            "No ICS URLs configured. Set WIW_ICS_URL_DUBLIN15 / WIW_ICS_URL_ESPOO in your environment or Streamlit secrets."
        )
        return

    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now().astimezone()
    local_tz_label = (
        getattr(now_local.tzinfo, "key", None) or now_local.tzname() or "Local time"
    )

    # ----- Header ----- #
    st.markdown("# Who's on shift?")
    st.caption(
        f"Current time (UTC): {now_utc.strftime('%Y-%m-%d %H:%M:%S')}  |  Local zone label: {local_tz_label}"
    )

    st.markdown("")  # small spacing

    # ----- Controls row ----- #
    controls_left, controls_mid, controls_right = st.columns([0.32, 0.48, 0.20])

    with controls_left:
        site_options = ["All locations"] + [
            f"{cfg['flag']} {cfg['label']}" for cfg in ACTIVE_SITES.values()
        ]
        site_choice = st.selectbox("Location", site_options, index=0)

    with controls_mid:
        search_text = st.text_input(
            "Search by name or role",
            placeholder="e.g. 'Shauna', 'MC', 'Flight Operator'",
        )

    with controls_right:
        st.write("")  # vertical spacing
        if st.button("Refresh now", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    st.markdown("---")

    # ----- Data ----- #
    try:
        all_sites = get_active_shifts(now_utc)
    except Exception as e:  # noqa: BLE001
        st.error(f"Error fetching or parsing schedule: {e}")
        return

    if not all_sites:
        st.info("No one is currently on shift according to the schedule.")
        return

    # Filter by site selection
    if site_choice != "All locations":
        # Map choice back to site_id by matching label+flag
        chosen = None
        for site_id, cfg in ACTIVE_SITES.items():
            label = f"{cfg['flag']} {cfg['label']}"
            if label == site_choice:
                chosen = site_id
                break
        if chosen:
            all_sites = {
                chosen: all_sites.get(
                    chosen,
                    {
                        "meta": ACTIVE_SITES[chosen],
                        "roles": {"MC": [], "Pilot": [], "Other": []},
                    },
                )
            }

    # Apply search filter
    all_sites = apply_search_filter(all_sites, search_text)

    if not all_sites:
        st.info("No matching people on shift for that search / site selection.")
        return

    st.markdown("")  # small spacing

    # ----- Per-site sections ----- #
    for site_id, site_data in all_sites.items():
        meta = site_data["meta"]
        roles = site_data["roles"]

        flag = meta["flag"]
        label = meta["label"]

        mc_count = len(roles["MC"])
        pilot_count = len(roles["Pilot"])
        other_count = len(roles["Other"])

        # Location header
        st.markdown(f"### {flag} {label}")
        st.markdown("")  # spacing under site heading

        col1, col2, col3 = st.columns([1, 1, 1.05])

        with col1:
            render_role_column(
                f"MC ({mc_count})",
                "#dcfce7",  # green-ish
                roles["MC"],
                is_other=False,
            )

        with col2:
            render_role_column(
                f"Pilot ({pilot_count})",
                "#dbeafe",  # blue-ish
                roles["Pilot"],
                is_other=False,
            )

        with col3:
            render_role_column(
                f"Other roles ({other_count})",
                "#f3e8ff",  # purple-ish
                roles["Other"],
                is_other=True,
            )

        st.markdown("---")


if __name__ == "__main__":
    main()
