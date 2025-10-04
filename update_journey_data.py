import os
import json
import requests
from datetime import datetime, timedelta
from nredarwin.webservice import DarwinLdbSession # <--- NEW IMPORT

# --- Configuration ---
TFL_APP_ID = os.getenv("TFL_APP_ID", "")
TFL_APP_KEY = os.getenv("TFL_APP_KEY", "")

# --- DARWIN LDBWS Configuration ---
# Read key from environment variable/GitHub Secret
DARWIN_API_KEY = os.getenv("DARWIN_API_KEY", "") 
DARWIN_WSDL = "https://lite.realtime.nationalrail.co.uk/OpenLDBWS/wsdl.aspx?ver=2021-01-01" 
# Note: Using the recommended specific version.

# CRS Codes for the journey stations (3-letter codes required for Darwin)
STREATHAM_COMMON_CRS = "SRC" 
IMPERIAL_WHARF_CRS = "IMW"
CLAPHAM_JUNCTION_CRS = "CLJ"

# Journey parameters
# ... (rest of your existing configuration)

# Journey parameters
ORIGIN = "Streatham Common Rail Station"
DESTINATION = "Imperial Wharf Rail Station"

# TFL API endpoint
TFL_BASE_URL = "https://api.tfl.gov.uk"
NUM_JOURNEYS = 4 # Target the next four journeys

# --- Utility Functions ---

def get_journey_plan(origin, destination):
    """Fetch journey plans from TFL Journey Planner API and log the full response."""
    url = f"{TFL_BASE_URL}/Journey/JourneyResults/{origin}/to/{destination}"
    
    params = {
        "mode": "overground,national-rail",
        "timeIs": "Departing",
        "journeyPreference": "LeastTime",
        "alternativeRoute": "true"
    }
    
    if TFL_APP_ID and TFL_APP_KEY:
        params["app_id"] = TFL_APP_ID
        params["app_key"] = TFL_APP_KEY
    
    try:
        print(f"[{datetime.now().isoformat()}] Fetching journeys from {origin} to {destination}...")
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        
        json_data = response.json()
        
        # --- VERBOSE LOGGING ---
        print("\n" + "="*80)
        print(">>> START TFL RAW JSON RESPONSE <<<")
        print(json.dumps(json_data, indent=2))
        print(">>> END TFL RAW JSON RESPONSE <<<")
        print("="*80 + "\n")
        # --- END VERBOSE LOGGING ---
        
        return json_data
        
    except requests.exceptions.RequestException as e:
        print(f"ERROR: Failed to fetch TFL data: {e}")
        return None


def parse_datetime(dt_string):
    """Parse TFL datetime string."""
    try:
        return datetime.fromisoformat(dt_string.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return None


def format_time(dt):
    """Format datetime to HH:MM."""
    if dt:
        return dt.strftime("%H:%M")
    return "N/A"


def extract_platform_from_instruction(instruction_text):
    """Try to extract platform from instruction text (LAST RESORT fallback)."""
    if not instruction_text:
        return None
    
    text_lower = instruction_text.lower()
    
    # Look for "platform X" pattern
    if 'platform' in text_lower:
        parts = text_lower.split('platform')
        if len(parts) > 1:
            after = parts[1].strip()
            platform = ''
            for char in after:
                # Allows for digits and single letters (e.g., Platform 1a)
                if char.isdigit() or (char.isalpha() and len(platform) == 1):
                    platform += char
                elif platform:
                    break
            if platform:
                return platform.upper()
    return None


def get_platform_from_leg(leg, is_departure=False):
    """
    Tries to extract platform from the leg details (TFL specific fields).
    Prioritizes the 'indicator' field, which often holds the platform number.
    """
    
    # Define the point we are checking (Departure or Arrival)
    point = leg.get('departurePoint' if is_departure else 'arrivalPoint', {})
    
    # 1. PRIMARY CHECK: Check the 'indicator' field 
    if point.get('indicator'):
        platform_indicator = str(point['indicator']).upper()
        if platform_indicator and platform_indicator != 'TBC':
            return platform_indicator

    # 2. Secondary Check: Check the 'platform' field 
    if point.get('platform'):
        platform_val = str(point['platform']).upper()
        if platform_val and platform_val != 'TBC':
            return platform_val

    # 3. Tertiary Check: Check the 'platformName' field 
    if point.get('platformName'):
        name = str(point['platformName'])
        if "Platform" in name:
             platform_name_extracted = name.split('Platform')[-1].strip().upper()
             if platform_name_extracted:
                return platform_name_extracted
        elif name and name.isalnum(): 
            return name.upper()
    
    # 4. Fallback to Instruction Text (Least reliable)
    instruction = leg.get('instruction', {})
    instruction_text = instruction.get('detailed', instruction.get('summary', ''))
    
    platform_from_text = extract_platform_from_instruction(instruction_text)
    if platform_from_text:
        return platform_from_text
        
    return None

# --- New/Modified Core Logic ---

def process_journey(journey, journey_id):
    """Process a TFL journey for direct or two-train routes."""
    start_time = parse_datetime(journey.get('startDateTime'))
    arrival_time = parse_datetime(journey.get('arrivalDateTime'))
    duration_mins = journey.get('duration', 0)
    
    legs = journey.get('legs', [])
    
    # Filter for only rail legs (National Rail or Overground)
    rail_legs = [
        leg for leg in legs 
        if leg.get('mode', {}).get('name', '') in ['national-rail', 'overground']
    ]
    
    if not rail_legs:
        return None
    
    # Check for multi-modal (non-train/non-walk) legs which should be excluded
    allowed_modes = ['walking', 'walk', 'national-rail', 'overground']
    if any(leg.get('mode', {}).get('name', '') not in allowed_modes for leg in legs):
        return None

    # --- REVISED STATUS LOGIC: Focus ONLY on Timing Delays ---
    status = "On Time"
    
    # Check if any leg has an explicitly reported arrival delay greater than 60 seconds (1 minute).
    # This filters out minor fluctuations and general station advisories.
    has_explicit_delay = any(
        leg.get('arrivalPoint', {}).get('timing', {}).get('arrivalDelay', 0) > 60 
        for leg in legs
    )
    
    if has_explicit_delay:
        status = "Delayed" 
    # --- END REVISED STATUS LOGIC ---
            
    processed_legs = []
    transfer_mins = 0

    # --- Direct Train (1 rail leg) ---
    if len(rail_legs) == 1:
        leg1 = rail_legs[0]
        leg1_depart = parse_datetime(leg1.get('departureTime'))
        leg1_arrive = parse_datetime(leg1.get('arrivalTime'))
        leg1_route = leg1.get('routeOptions', [])
        leg1_line = leg1_route[0].get('name', 'Rail') if leg1_route else 'Rail'
        
        # Departure platform (at Streatham Common)
        leg1_platform = get_platform_from_leg(leg1, is_departure=True)
        
        processed_legs.append({
            "origin": "Streatham Common",
            "destination": "Imperial Wharf",
            "departure": format_time(leg1_depart),
            "arrival": format_time(leg1_arrive),
            "departurePlatform": leg1_platform or "TBC",
            "operator": leg1_line,
            "status": status
        })

    # --- Two-Train Journey (2 rail legs) ---
    elif len(rail_legs) == 2:
        
        # Leg 1: Streatham Common to Interchange (e.g. Clapham Junction)
        leg1 = rail_legs[0]
        leg1_depart = parse_datetime(leg1.get('departureTime'))
        leg1_arrive = parse_datetime(leg1.get('arrivalTime'))
        leg1_route = leg1.get('routeOptions', [])
        leg1_line = leg1_route[0].get('name', 'Rail') if leg1_route else 'Rail'

        # Leg 2: Interchange to Imperial Wharf
        leg2 = rail_legs[1]
        leg2_depart = parse_datetime(leg2.get('departureTime'))
        leg2_arrive = parse_datetime(leg2.get('arrivalTime'))
        leg2_route = leg2.get('routeOptions', [])
        leg2_line = leg2_route[0].get('name', 'Rail') if leg2_route else 'Rail'

        # Interchange Station (e.g., Clapham Junction)
        interchange = leg1.get('arrivalPoint', {}).get('commonName', 'Interchange')

        # Platform Extraction at Interchange
        leg1_arrival_platform = get_platform_from_leg(leg1, is_departure=False)
        leg2_departure_platform = get_platform_from_leg(leg2, is_departure=True)

        # Calculate transfer time
        if leg1_arrive and leg2_depart:
            transfer_mins = int((leg2_depart - leg1_arrive).total_seconds() / 60)
        
        # First Train Leg
        processed_legs.append({
            "origin": "Streatham Common",
            "destination": interchange,
            "departure": format_time(leg1_depart),
            "arrival": format_time(leg1_arrive),
            "arrivalPlatform_ClaphamJunction": leg1_arrival_platform or "TBC",
            "operator": leg1_line,
            "status": status
        })
        
        # Transfer Detail
        processed_legs.append({
            "type": "transfer",
            "location": interchange,
            "transferTime": f"{transfer_mins} min"
        })
        
        # Second Train Leg
        processed_legs.append({
            "origin": interchange,
            "destination": "Imperial Wharf",
            "departure": format_time(leg2_depart),
            "arrival": format_time(leg2_arrive),
            "departurePlatform_ClaphamJunction": leg2_departure_platform or "TBC",
            "operator": leg2_line,
            "status": status
        })

    else:
        # Exclude journeys with 0, 3, or more rail legs
        return None

    return {
        "id": journey_id,
        "type": "Direct" if len(rail_legs) == 1 else "One Change",
        "departureTime": format_time(start_time),
        "arrivalTime": format_time(arrival_time),
        "totalDuration": f"{duration_mins} min",
        "status": status,
        "live_updated_at": datetime.now().strftime("%H:%M:%S"),
        "legs": processed_legs
    }


def fetch_and_process_tfl_data(num_journeys):
    """Fetch and process TFL journey data for a fixed number of valid train journeys."""
    
    journey_data = get_journey_plan(ORIGIN, DESTINATION)
    
    if not journey_data or 'journeys' not in journey_data:
        print("ERROR: No journey data received from TFL API")
        return []
    
    journeys = journey_data.get('journeys', [])
    print(f"Found {len(journeys)} total journeys from TFL in the response.")
    
    processed = []
    for idx, journey in enumerate(journeys, 1):
        try:
            processed_journey = process_journey(journey, len(processed) + 1)
            if processed_journey:
                processed.append(processed_journey)
                print(f"✓ Journey {len(processed)} ({processed_journey['type']}): {processed_journey['departureTime']} → {processed_journey['arrivalTime']} | Status: {processed_journey['status']}")
                
                if len(processed) >= num_journeys:
                    break
        except Exception as e:
            print(f"ERROR processing journey {idx}: {e}")
            continue
    
    print(f"Successfully processed {len(processed)} train journeys (Direct or One Change)")
    return processed


def main():
    data = fetch_and_process_tfl_data(NUM_JOURNEYS)
    
    if data:
        with open(OUTPUT_FILE, 'w') as f:
            json.dump(data, f, indent=4)
        print(f"\n✓ Successfully saved {len(data)} journeys to {OUTPUT_FILE}")
    else:
        print("\n⚠ No journey data generated.")


if __name__ == "__main__":
    main()
