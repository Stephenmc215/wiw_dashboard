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

# Optional: URL or identifier for OTNs feed (SharePoint, etc.)
# For now this is unused – your SW team can wire this into fetch_latest_otns().
OTN_SOURCE = os.getenv("OTN_SHAREPOINT_URL", "")


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


def classify_role_standard(role: str) -> str:
    """
    Buckets for the normal view: MC, Pilot, Other.
    """
    r = role.lower()
    if "mission control" in r or "(mc)" in r:
        return "MC"
    if "flight operator" in r or "(fo)" in r or "pilot" in r:
        return "Pilot"
    return "Other"


def classify_role_mc_focus(role: str) -> str:
    """
    Buckets for the MC view: Flight Operator, Loader, Collector, Other.
    """
    r = role.lower()
    if "flight operator" in r or "(fo)" in r:
        return "Flight Operator"
    if "loader" in r:
        return "Loader"
    if "collector" in r:
        return "Collector"
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
    Group active shifts by site and role for the *standard* view.

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
            bucket = classify_role_standard(info["role"])

            roles[bucket].append(
                {
                    "name": info["name"],
                    "role": info["role"],
                    "start": start,
                    "end": end,
                }
            )

        # Sort by name for neatness
        for key in roles:
            roles[key].sort(key=lambda x: x["name"])

        results[site_id] = {"meta": meta, "roles": roles}

    return results


def regroup_for_mc_view(all_sites: Dict[str, Dict]) -> Dict[str, Dict]:
    """
    Take the standard-view structure and regroup everyone into
    Flight Operator / Loader / Collector / Other buckets for each site.
    """
    mc_results: Dict[str, Dict] = {}

    for site_id, site_data in all_sites.items():
        meta = site_data["meta"]
        std_roles = site_data["roles"]

        # Flatten all people in this site
        everyone: List[Dict] = (
            std_roles.get("MC", [])
            + std_roles.get("Pilot", [])
            + std_roles.get("Other", [])
        )

        buckets: Dict[str, List[Dict]] = {
            "Flight Operator": [],
            "Loader": [],
            "Collector": [],
            "Other": [],
        }

        for p in everyone:
            bucket = classify_role_mc_focus(p["role"])
            buckets[bucket].append(p)

        # Sort by name
        for k in buckets:
            buckets[k].sort(key=lambda x: x["name"])

        mc_results[site_id] = {"meta": meta, "roles": buckets}

    return mc_results


# ----------------- OTN (SharePoint) ----------------- #


def fetch_latest_otns() -> List[Dict]:
    """
    Fetch the latest 2 OTNs for MCs.

    Right now this is just a placeholder that returns an empty list.
    Your SW / IT team can plug SharePoint here using the OTN_SOURCE
    settings and something like `office365-rest-python-client` or a
    simple CSV/JSON export.

    Expected return shape:
    [
      {
        "title": "OTN-123 – Something happened",
        "created": "2025-11-29 14:31",
        "summary": "Short description...",
        "link": "https://some.sharepoint.url/..."
      },
      ...
    ]
    """
    # If you want to test the layout with fake data, uncomment below:

    # return [
    #     {
    #         "title": "OTN-123 – Example only",
    #         "created": "2025-11-29 14:31",
    #         "summary": "This is a demo OTN card. Wire SharePoint to replace this.",
    #         "link": "https://example.com",
    #     },
    #     {
    #         "title": "OTN-122 – Another example",
    #         "created": "2025-11-29 13:10",
    #         "summary": "Second demo OTN card for layout.",
    #         "link": "https://example.com",
    #     },
    # ]

    # No SharePoint integration yet → return empty
    return []


def render_otn_cards():
    """Render the latest OTNs (if any) at the top of the MC tab."""
    otns = fetch_latest_otns()
    st.subheader("Latest OTNs")

    if not otns:
        st.info(
            "No OTN feed is configured yet. Once SharePoint is connected, the latest OTNs will appear here."
        )
        return

    cols = st.columns(len(otns))
    for col, otn in zip(cols, otns):
        with col:
            card_html = f"""
            <div style="
                padding:0.75rem 0.9rem;
                border-radius:0.7rem;
                background-color:#ffffff;
                box-shadow:0 0 0 1px #e5e7eb;
                margin-bottom:0.6rem;
            ">
              <div style="font-weight:600; color:#111827; margin-bottom:0.25rem;">
                {otn.get('title', 'OTN')}
              </div>
              <div style="font-size:0.8rem; color:#6b7280; margin-bottom:0.35rem;">
                {otn.get('created', '')}
              </div>
              <div style="font-size:0.85rem; color:#374151; margin-bottom:0.5rem;">
                {otn.get('summary', '')}
              </div>
              <div style="font-size:0.8rem;">
                <a href="{otn.get('link', '#')}" target="_blank" style="color:#2563eb; text-decoration:none;">
                  Open in SharePoint ↗
                </a>
              </div>
            </div>
            """
            st.markdown(card_html, unsafe_allow_html=True)


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
        padding:0.55rem 0.8rem;
        border-radius:0.6rem;
        background-color:#ffffff;
        margin-top:0.35rem;
        box-shadow:0 0 0 1px #e5e7eb;
    ">
      <div style="font-weight:600; color:#111827;">{name}</div>
      <div style="font-size:0.85rem; color:#4b5563; margin-top:0.1rem;">{role}</div>
      <div style="font-size:0.8rem; color:#047857; margin-top:0.25rem;">
        ● On until {end_label}
      </div>
    </div>
    """
    st.markdown(card_html, unsafe_allow_html=True)


def render_role_column(title: str, colour: str, people: List[Dict], is_other: bool = False):
    """
    Render a role column (header + list of people), or an 'Other roles' expander.
    colour = background colour of the header bar.
    """
    header_html = f"""
    <div style="
        padding:0.35rem 0.75rem;
        border-radius:0.6rem;
        background-color:{colour};
        font-weight:600;
        font-size:0.9rem;
        color:#111827;
        margin-bottom:0.3rem;
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


def apply_search_filter(all_sites: Dict[str, Dict], search_text: str) -> Dict[str, Dict]:
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


def render_standard_site_section(site_id: str, site_data: Dict):
    """Per-site layout for the standard view (MC / Pilot / Other)."""
    meta = site_data["meta"]
    roles = site_data["roles"]

    flag = meta["flag"]
    label = meta["label"]

    mc_count = len(roles["MC"])
    pilot_count = len(roles["Pilot"])
    other_count = len(roles["Other"])

    st.markdown(f"### {flag} {label}")

    # Tiny counts above the columns
    col_mc, col_pilot, col_other = st.columns(3)
    with col_mc:
        st.markdown(
            f"<div style='font-size:0.8rem; color:#059669;'>● MC: {mc_count}</div>",
            unsafe_allow_html=True,
        )
    with col_pilot:
        st.markdown(
            f"<div style='font-size:0.8rem; color:#2563eb;'>● Pilot: {pilot_count}</div>",
            unsafe_allow_html=True,
        )
    with col_other:
        st.markdown(
            f"<div style='font-size:0.8rem; color:#6b21a8;'>● Other: {other_count}</div>",
            unsafe_allow_html=True,
        )

    st.markdown("")

    col1, col2, col3 = st.columns([1, 1, 1.1])

    with col1:
        render_role_column(f"MC ({mc_count})", "#e9f7ef", roles["MC"], is_other=False)

    with col2:
        render_role_column(f"Pilot ({pilot_count})", "#e5f0ff", roles["Pilot"], is_other=False)

    with col3:
        render_role_column("Other roles", "#f4e9ff", roles["Other"], is_other=True)

    st.markdown("---")


def render_mc_site_section(site_id: str, site_data: Dict):
    """Per-site layout for the MC-focused view."""
    meta = site_data["meta"]
    roles = site_data["roles"]

    flag = meta["flag"]
    label = meta["label"]

    fo_count = len(roles["Flight Operator"])
    loader_count = len(roles["Loader"])
    collector_count = len(roles["Collector"])
    other_count = len(roles["Other"])

    st.markdown(f"### {flag} {label}")

    col_fo, col_loader, col_collector = st.columns(3)
    with col_fo:
        st.markdown(
            f"<div style='font-size:0.8rem; color:#2563eb;'>● Flight operators: {fo_count}</div>",
            unsafe_allow_html=True,
        )
    with col_loader:
        st.markdown(
            f"<div style='font-size:0.8rem; color:#16a34a;'>● Loaders: {loader_count}</div>",
            unsafe_allow_html=True,
        )
    with col_collector:
        st.markdown(
            f"<div style='font-size:0.8rem; color:#a855f7;'>● Collectors: {collector_count}</div>",
            unsafe_allow_html=True,
        )

    st.markdown("")

    col1, col2, col3 = st.columns([1, 1, 1.1])

    with col1:
        render_role_column(
            f"Flight operators ({fo_count})",
            "#e5f0ff",
            roles["Flight Operator"],
            is_other=False,
        )

    with col2:
        render_role_column(
            f"Loaders ({loader_count})", "#e9f7ef", roles["Loader"], is_other=False
        )

    with col3:
        # We treat collectors as the primary content, and any "Other" in an expander below
        render_role_column(
            f"Collectors ({collector_count})", "#fef3c7", roles["Collector"], is_other=False
        )
        # Optional extra expander for "Other" if present
        if other_count:
            st.markdown("")
            render_role_column("Other roles", "#f4e9ff", roles["Other"], is_other=True)

    st.markdown("---")


# ----------------- MAIN APP ----------------- #


def main():
    st.set_page_config(page_title="Who’s on shift?", layout="wide")

    # Soft background + minimal Streamlit chrome
    st.markdown(
        """
        <style>
        .stApp {background-color: #f6f7fb;}
        #MainMenu {visibility: hidden;}
        footer {visibility: hidden;}
        </style>
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
    st.markdown("## Who’s on shift?")
    st.markdown(
        "<hr style='margin-top:0.1rem; margin-bottom:0.9rem; border: none; height: 2px; background-color: #6366f1;' />",
        unsafe_allow_html=True,
    )

    st.caption(
        f"Current time (UTC): {now_utc.strftime('%Y-%m-%d %H:%M:%S')}  |  Local zone label: {local_tz_label}"
    )

    # ----- Controls ----- #
    st.markdown("")
    col_loc, col_search, col_refresh = st.columns([0.32, 0.48, 0.20])

    with col_loc:
        site_options = ["All locations"] + [
            f"{cfg['flag']} {cfg['label']}" for cfg in ACTIVE_SITES.values()
        ]
        site_choice = st.selectbox("Location", site_options, index=0)

    with col_search:
        search_text = st.text_input(
            "Search by name or role",
            placeholder="e.g. 'Shauna', 'MC', 'Flight Operator'",
        )

    with col_refresh:
        st.write("")  # vertical spacing
        if st.button("Refresh now", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    st.markdown("")

    # Get full picture once
    try:
        all_sites = get_active_shifts(now_utc)
    except Exception as e:
        st.error(f"Error fetching or parsing schedule: {e}")
        return

    if not all_sites:
        st.info("No one is currently on shift according to the schedule.")
        return

    # Filter by site dropdown
    if site_choice != "All locations":
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

    # Apply free text search (works on standard buckets)
    filtered_sites = apply_search_filter(all_sites, search_text)

    if not filtered_sites:
        st.info("No matching people on shift for that search / site selection.")
        return

    # ----- Tabs ----- #
    tab_standard, tab_mc = st.tabs(["Standard view", "MC view"])

    with tab_standard:
        st.markdown("")
        for site_id, site_data in filtered_sites.items():
            render_standard_site_section(site_id, site_data)

    with tab_mc:
        st.markdown("")
        # OTNs at the top for MC
        render_otn_cards()
        st.markdown("")

        # Regroup into FO / Loader / Collector buckets
        mc_sites = regroup_for_mc_view(filtered_sites)
        for site_id, site_data in mc_sites.items():
            render_mc_site_section(site_id, site_data)


if __name__ == "__main__":
    main()
