import requests
import re
import pandas as pd
from datetime import datetime
import os

# --- Output Settings ---
OUTPUT_DIR = "../data/output"
OUTPUT_FILE = "surf_zone_forecast.csv"

# --- Helper Functions ---

def abbreviate_wind_direction(text):
    """Convert wind direction text to abbreviations."""
    replacements = {
        "northeast": "NE",
        "southeast": "SE",
        "northwest": "NW",
        "southwest": "SW",
        "north": "N",
        "south": "S",
        "east": "E",
        "west": "W"
    }

    # Longer directions first
    for word in sorted(replacements.keys(), key=len, reverse=True):
        abbr = replacements[word]
        text = re.sub(rf"\b{word}\b", abbr, text, flags=re.I)

    return text


def uv_to_category(uvi):
    """Convert numeric UV to exposure category."""
    if uvi <= 2:
        return "Low"
    elif uvi <= 5:
        return "Moderate"
    elif uvi <= 7:
        return "High"
    elif uvi <= 10:
        return "Very High"
    else:
        return "Extreme"


def strip_hour_leading_zero(time_str):
    """Remove leading zero from hour only (for tides)."""
    return re.sub(r"(?<=\b)0(\d{1,2}):", r"\1:", time_str)


def get_surf_forecast():
    """Fetch and parse the Surf Zone Forecast for FLZ172-173."""

    url = (
        "https://forecast.weather.gov/product.php?"
        "site=MFL&issuedby=MFL&product=SRF&format=CI&version=1&glossary=0&text=1"
    )

    resp = requests.get(url, timeout=15)
    resp.raise_for_status()

    srf_text = resp.text

    # --- Extract the block for FLZ172 or FLZ173 ---
    zone_pattern = re.compile(
        r"FLZ(?:172-173|173)-\d{6}-[\s\S]+?(?=FLZ|\Z)",
        re.M
    )

    zone_block = zone_pattern.search(srf_text)

    if not zone_block:
        print("No surf zone forecast block found")
        return pd.DataFrame()

    zone_text = zone_block.group(0)

    # --- Cleanup ---
    zone_text = re.sub(
        r"&&[\s\S]+?(?=FLZ|\Z)",
        "",
        zone_text,
        flags=re.MULTILINE
    )

    zone_text = re.sub(
        r"Rip Current Risk Category[\s\S]+?(?=\Z|FLZ)",
        "",
        zone_text,
        flags=re.MULTILINE
    )

    zone_text = re.sub(
        r"\*\* For thunderstorm[\s\S]+",
        "",
        zone_text,
        flags=re.IGNORECASE
    )

    # --- Split into forecast periods ---
    periods = re.split(r"\n\.", zone_text)

    rows = []

    # --- Get CPC UV ---
    uv_url = "https://www.cpc.ncep.noaa.gov/products/stratosphere/uv_index/bulletin.txt"

    uv_resp = requests.get(uv_url, timeout=15)
    uv_resp.raise_for_status()

    uv_text = uv_resp.text

    uv_match = re.search(r"MIAMI\s+FL\s+(\d+)", uv_text)

    uv_cpc_value = int(uv_match.group(1)) if uv_match else None

    uv_cpc_category = (
        uv_to_category(uv_cpc_value)
        if uv_cpc_value is not None
        else None
    )

    water_temp = None

    # --- Parse each forecast period ---
    for i, p in enumerate(periods):

        p = p.strip()

        if not p or not re.match(r"[A-Z /]{3,}\.\.\.", p):
            continue

        header_match = re.match(r"([A-Z /]+)\.\.\.(.*)", p, re.S)

        if not header_match:
            continue

        period = header_match.group(1).title().strip()

        forecast_text = header_match.group(2).replace("\n", " ").strip()

        # --- Rip Current Risk ---
        rip_match = re.search(
            r"Rip Current Risk\*.*?\.*\s*(High|Moderate|Low)",
            forecast_text,
            re.I
        )

        rip_text = rip_match.group(1).lower() if rip_match else ""

        rip_flags = {
            "RipLow": 1 if "low" in rip_text else 0,
            "RipMedium": 1 if "moderate" in rip_text else 0,
            "RipHigh": 1 if "high" in rip_text else 0
        }

        # --- Surf Height ---
        surf_match = re.search(
            r"Surf Height.*?\.*\s*([A-Za-z0-9 ,/]+?)(?:\.|\n|$)",
            forecast_text,
            re.I
        )

        surf = None

        if surf_match:
            surf = surf_match.group(1).strip()
            surf = surf.replace("feet", "ft")
            surf = surf.replace("foot", "ft")
            surf = surf.replace(" to ", "-")

        # --- UV Index ---
        uv_match_text = re.search(
            r"UV Index\*\*.*?\.*\s*([A-Za-z0-9 ]+)",
            forecast_text,
            re.I
        )

        uv_index = (
            uv_match_text.group(1)
            if uv_match_text
            else (uv_cpc_category if i > 0 else None)
        )

        # --- Full Wind Text ---
        full_wind_text = ""

        wind_match_full = re.search(
            r"Winds\s*\.{0,}\s*(.+?)(?=\bTides\b|$)",
            forecast_text,
            re.I | re.S
        )

        if wind_match_full:
            full_wind_text = wind_match_full.group(1).strip()
            full_wind_text = re.sub(r"\s+", " ", full_wind_text)

        # --- Wind Parsing ---
        wind = ""

        forecast_text_clean = forecast_text.replace("\n", " ").strip()

        becoming_match = re.search(
            r"[,]*\s*becoming\s+([A-Za-z ]+)\s*(?:around\s*)?(\d+(?: to \d+)?)?\s*mph",
            forecast_text_clean,
            re.I
        )

        if becoming_match:

            dir_text = becoming_match.group(1).strip()

            dir_text = re.sub(
                r"\bwinds?\b",
                "",
                dir_text,
                flags=re.I
            ).strip()

            dir_abbr = (
                abbreviate_wind_direction(dir_text)
                if dir_text.lower() != "variable"
                else "Variable"
            )

            speed = becoming_match.group(2) if becoming_match.group(2) else ""

            speed = speed.replace("around", "")
            speed = speed.replace(" to ", "-").strip()

            wind = f"{dir_abbr} {speed} mph".strip() if speed else dir_abbr

        else:

            match = re.search(
                r"(?:Winds|winds)\s*(?:\.\s*)*([A-Za-z ]+?)\s*(?:winds?)?\s*(?:around|near\s*)?(\d+(?: to \d+)?)?\s*mph",
                forecast_text_clean,
                re.I
            )

            if match:

                dir_text = match.group(1).strip() if match.group(1) else ""

                dir_text = re.sub(
                    r"\bwinds?\b",
                    "",
                    dir_text,
                    flags=re.I
                ).strip()

                dir_abbr = abbreviate_wind_direction(dir_text)

                speed = match.group(2) if match.group(2) else ""

                speed = speed.replace("around", "")
                speed = speed.replace(" to ", "-").strip()

                wind = f"{dir_abbr} {speed} mph".strip() if speed else dir_abbr

        # --- Water Temperature ---
        if not water_temp:

            temp_match = re.search(
                r"Water Temperature.*?\.*\s*In the (lower|mid|upper) (\d+)",
                forecast_text,
                re.I
            )

            if temp_match:
                water_temp = (
                    f"{temp_match.group(1).capitalize()} "
                    f"{temp_match.group(2)}s"
                )

        # --- Sunrise / Sunset ---
        sunrise_match = re.search(
            r"Sunrise\s*\.{0,}\s*([\d:APM ]+)",
            forecast_text,
            re.I
        )

        sunset_match = re.search(
            r"Sunset\s*\.{0,}\s*([\d:APM ]+)",
            forecast_text,
            re.I
        )

        sunrise = (
            strip_hour_leading_zero(sunrise_match.group(1).strip())
            if sunrise_match else None
        )

        sunset = (
            strip_hour_leading_zero(sunset_match.group(1).strip())
            if sunset_match else None
        )

        # --- Tides ---
        tide_matches = re.findall(
            r"(High|Low)\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)",
            forecast_text
        )

        tide1 = (
            f"{tide_matches[0][0]} "
            f"{strip_hour_leading_zero(tide_matches[0][1])}"
            if len(tide_matches) >= 1 else ""
        )

        tide2 = (
            f"{tide_matches[1][0]} "
            f"{strip_hour_leading_zero(tide_matches[1][1])}"
            if len(tide_matches) >= 2 else ""
        )

        rows.append({
            "Zone": "FLZ172-173",
            "Period": period,
            "Wind": wind,
            "Surf Height": surf,
            "Water Temperature": water_temp,
            "Rip Current Risk": (
                rip_match.group(1).capitalize()
                if rip_match else None
            ),
            "RipLow": rip_flags["RipLow"],
            "RipMedium": rip_flags["RipMedium"],
            "RipHigh": rip_flags["RipHigh"],
            "UV Index": uv_index,
            "Sunrise": sunrise,
            "Sunset": sunset,
            "Tide 1": tide1,
            "Tide 2": tide2,
            "FullWindText": full_wind_text,
            "Retrieved": datetime.now().strftime("%m-%d %I:%M %p")
        })

    return pd.DataFrame(rows)


# --- Main Execution ---

if __name__ == "__main__":

    try:

        df = get_surf_forecast()

        if df.empty:
            raise ValueError("No surf forecast rows found")

        print(df[["Period"]])

        now_hour = datetime.now().hour

        # --- Safe row selection ---
        if now_hour < 12 or len(df) == 1:
            single_row_df = df.iloc[[0]].copy()
            single_row_df["Period"] = "Today"
        else:
            single_row_df = df.iloc[[1]].copy()
            single_row_df["Period"] = "Tomorrow"

        # --- Ensure output directory exists ---
        os.makedirs(OUTPUT_DIR, exist_ok=True)

        # --- Local Save ---
        local_path = os.path.join(OUTPUT_DIR, OUTPUT_FILE)

        single_row_df.to_csv(local_path, index=False)

        print(f"Saved CSV: {local_path}")

    except Exception as e:

        print(f"Script failed: {e}")
