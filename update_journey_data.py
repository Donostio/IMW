import os
import json
import requests
from datetime import datetime

# --- Configuration & Credentials ---

# Fetch credentials from GitHub Actions environment variables
TAPI_APP_ID = os.environ.get("TAPI_APP_ID")
TAPI_APP_KEY = os.environ.get("TAPI_APP_KEY")

if not TAPI_APP_ID or not TAPI_APP_KEY:
    print("CRITICAL ERROR: TAPI credentials not found in environment variables. Stopping.")
    exit(1)

OUTPUT_FILE = "live_data.json" 
TAPI_BASE_URL = "https://transportapi.com"

# Journey parameters (using CRS codes for reliability)
ORIGIN_CRS = "SRC" # Streatham Common
DESTINATION_CRS = "IMW" # Imperial Wharf

# --- Utility Functions ---

def get_journey_plan(origin, destination, time_str):
    """
    Step 1: Fetch scheduled journey plans using the authorized TfL service (public_journey [service=tfl]).
    This service is included in your free plan, replacing the failing 'silverrail' service.
    """
    url = f"{TAPI_BASE_URL}/v3/uk/public_journey.json"
    
    now = datetime.now()
    current_date = now.strftime("%Y-%m-%d")
    
    params = {
        "from": origin,
        "to": destination,
        "date": current_date,
        "time": time_str, 
        "modes": "train",
        "service": "tfl", # CRUCIAL: Using the authorized 'tfl' service
        "app_id": TAPI_APP_ID,
        "app_key": TAPI_APP_KEY
    }
    
    try:
        print(f"[{now.isoformat()}] Step 1: Fetching scheduled journeys from {origin} to {destination} using TFL service...")
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status() 
        
        json_data = response.json()
        return json_data
        
    except requests.exceptions.RequestException as e:
        print(f"ERROR: Failed to fetch TAPI Journey Planner (TFL service) data: {e}")
        return None

def get_live_departures_timetable(station_code):
    """
    Step 2: Fetch the live departure timetable for a specific station.
    Uses 'train/station_timetables [live=true]' which is included in your free plan.
    Note: This is less granular than the missing 'actual_journeys' endpoint.
    """
    url = f"{TAPI_BASE_URL}/v3/uk/train/station/{station_code}/timetable.json"
    
    params = {
        "live": "true",
        "app_id": TAPI_APP_ID,
        "app_key": TAPI_APP_KEY
    }
    
    try:
        print(f"[{datetime.now().isoformat()}] Step 2: Fetching live departures timetable for {station_code}...")
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        
        live_data = response.json()
        # Extract services member list
        return live_data.get('departures', {}).get('all', [])
        
    except requests.exceptions.RequestException as e:
        print(f"ERROR: Failed to fetch TAPI Live Timetable data: {e}")
        return []

def format_time(time_str):
    """Formats time to HH:MM from TAPI's 'HH:MM' string."""
    return time_str if time_str else "N/A"
        
def parse_duration(duration_str):
    """Converts 'HH:MM:SS' to minutes."""
    try:
        h, m, s = map(int, duration_str.split(':'))
        return h * 60 + m
    except ValueError:
        return 0
        
def time_to_seconds(time_str):
    """Converts 'HH:MM' string to total seconds for comparison."""
    if not time_str: return -1
    try:
        h, m = map(int, time_str.split(':'))
        return h * 3600 + m * 60
    except ValueError:
        return -1

def find_live_data(scheduled_departure_time, live_departures):
    """
    Matches the scheduled time (from Journey Planner) to a live service (from Timetable).
    """
    scheduled_seconds = time_to_seconds(scheduled_departure_time)
    
    default_live = {"status": "Scheduled", "platform": "TBC", "live_time": scheduled_departure_time}

    # Match within a 5-minute window of the scheduled time
    for service in live_departures:
        # Timetable uses 'expected_departure_time' for the scheduled time
        aimed_time_str = service.get('expected_departure_time')
        
        if not aimed_time_str:
            continue
            
        aimed_seconds = time_to_seconds(aimed_time_str)
        
        # Match using a generous time margin
        if abs(scheduled_seconds - aimed_seconds) <= 300: # 5 minutes
            
            platform = service.get('platform') or "TBC"
            
            # The 'best_departure_estimate' is the live time provided by this less-granular endpoint
            best_departure = service.get('best_departure_estimate') 
            
            status = "Scheduled"
            if best_departure and best_departure != aimed_time_str:
                status = "Delayed"
            elif service.get('status') == 'CANCELLED':
                status = "Canceled"
                best_departure = "N/A"
            else:
                 status = "On Time"

            return {"status": status, "platform": platform, "live_time": best_departure or aimed_time_str}
            
    return default_live


# --- Core Logic ---

def process_journey_data(journey_data, filter_config):
    """Extract and format journey data from TAPI response, applying time and type filters."""
    routes = journey_data.get('routes', [])
    processed_journeys = []
    
    # Fetch live departures once for the current time window for the origin station (SRC)
    live_departures_src = get_live_departures_timetable(ORIGIN_CRS)
    
    print(f"Found {len(live_departures_src)} live trains in the timetable for {ORIGIN_CRS}.")
    
    for route in routes:
        
        # 1. ROUTE TYPE CHECK (Direct vs One Change)
        route_parts = route.get('route_parts', [])
        train_legs = [part for part in route_parts if part.get('mode') == 'train']
        
        # IMPORTANT: TfL public_journey often reports rail journeys as having 2 legs even if they are direct
        # because it splits the journey internally. We rely on route_parts length for changes.
        
        # Check if the destination of the first leg is Imperial Wharf. If so, it's considered direct.
        is_direct = (len(train_legs) == 1) or (len(train_legs) > 1 and train_legs[0].get('to_point_name', '') == DESTINATION_CRS)
        
        route_type = "Direct" if is_direct else "One Change"
        
        # 2. DEPARTURE TIME CHECK 
        scheduled_departure_time = route.get('departure_time')
        dep_seconds = time_to_seconds(scheduled_departure_time)
        
        # --- FILTERS ---
        
        if filter_config['is_weekend']:
            # Weekend: First direct journey after 07:20
            target_after_seconds = time_to_seconds("07:20")
            
            # Skip if: 1) Not Direct OR 2) Departs before 7:20
            if route_type != "Direct" or dep_seconds < target_after_seconds:
                continue
                
            # If we find the first valid direct journey, we take it and break
            if len(processed_journeys) >= 1:
                break
                
        else: # Weekday (Mon-Fri)
            # Weekday: Next two indirect journeys after 07:25
            target_after_seconds = time_to_seconds("07:25")
            
            # Skip if: 1) Is Direct OR 2) Departs before 7:25
            if route_type != "One Change" or dep_seconds < target_after_seconds:
                continue
            
            # Stop once we have the two indirect journeys
            if len(processed_journeys) >= 2:
                break
        
        # If the journey passes all filters:
        
        total_duration = parse_duration(route.get('duration', '00:00:00'))
        scheduled_arrival_time = route.get('arrival_time')
        first_train_leg = train_legs[0]
        
        # Get live status for the FIRST leg (departure from SRC)
        live_info = find_live_data(first_train_leg.get('departure_time'), live_departures_src)
        
        # Build the legs array
        processed_legs = []
        if route_type == "Direct":
            processed_legs.append({
                "origin": ORIGIN_CRS, "destination": DESTINATION_CRS,
                "scheduledDeparture": format_time(scheduled_departure_time),
                "liveDeparture": live_info['live_time'],
                "scheduledArrival": format_time(scheduled_arrival_time),
                "departurePlatform": live_info['platform'],
                "operator": first_train_leg.get('operator_name', 'Rail'),
                "status": live_info['status']
            })
        else:
            # One Change logic (simplified for output)
            # We assume the second leg is the one to IMW (Imperial Wharf)
            leg1 = train_legs[0]
            
            # Find the actual interchange point (usually Clapham Junction, but check TFL data)
            interchange = leg1.get('to_point_name', 'Clapham Junction')
            
            # If there are multiple train legs, use the first two
            if len(train_legs) >= 2:
                leg2 = train_legs[1]
            else:
                 # Fallback if TFL only returns one leg but it was classified as 'One Change'
                leg2 = {"departure_time": "N/A", "arrival_time": "N/A", "operator_name": "Unknown"}

            # Leg 1: Streatham Common to Interchange (e.g., Clapham Junction)
            processed_legs.append({
                "origin": ORIGIN_CRS, "destination": interchange,
                "scheduledDeparture": leg1.get('departure_time'), "liveDeparture": live_info['live_time'],
                "scheduledArrival": leg1.get('arrival_time'), "departurePlatform": live_info['platform'],
                "operator": leg1.get('operator_name', 'Rail'), "status": live_info['status']
            })
            
            processed_legs.append({"type": "transfer", "location": interchange, "transferTime": "Check schedule"})
            
            # Leg 2: Interchange to Imperial Wharf
            # NOTE: We cannot easily get live data for the second leg without another API call/more complexity, 
            # so we use scheduled times for the second leg.
            processed_legs.append({
                "origin": interchange, "destination": DESTINATION_CRS,
                "scheduledDeparture": leg2.get('departure_time'), "liveDeparture": leg2.get('departure_time'),
                "scheduledArrival": leg2.get('arrival_time'), "departurePlatform": "TBC (Scheduled)", 
                "operator": leg2.get('operator_name', 'Rail'), "status": "Scheduled" 
            })
            
        processed_journeys.append({
            "id": len(processed_journeys) + 1,
            "type": route_type,
            "scheduledDepartureTime": format_time(scheduled_departure_time),
            "scheduledArrivalTime": format_time(scheduled_arrival_time),
            "totalDuration": f"{total_duration} min",
            "overallStatus": live_info['status'], # Status based on the first leg
            "live_updated_at": datetime.now().strftime("%H:%M:%S"),
            "legs": processed_legs
        })
        
    return processed_journeys


def fetch_and_process_tapi_data():
    """Main function to fetch and process TAPI data based on day of week."""
    
    # --- DYNAMIC FILTER CONFIGURATION ---
    now = datetime.now()
    # 0 = Monday, 6 = Sunday
    day_of_week = now.weekday() 
    
    # Saturday (5) or Sunday (6)
    is_weekend = day_of_week >= 5 
    
    filter_config = {
        'is_weekend': is_weekend,
        'search_start_time': "06:30" 
    }
    
    if is_weekend:
        print("Weekend Mode: Targeting the first direct journey after 07:20.")
    else:
        print("Weekday Mode: Targeting the next two indirect journeys after 07:25.")
    # --- END DYNAMIC CONFIGURATION ---

    # 1. TAPI Journey Planner Call (using authorized TFL service)
    journey_data = get_journey_plan(ORIGIN_CRS, DESTINATION_CRS, filter_config['search_start_time'])
    
    if not journey_data or 'routes' not in journey_data:
        print("ERROR: No route data received from TAPI Journey Planner (TFL service).")
        return []
    
    # 2. Process, filter, and enrich with Live Data
    data = process_journey_data(journey_data, filter_config)
    
    print(f"Successfully processed {len(data)} train journeys.")
    return data


def main():
    data = fetch_and_process_tapi_data()
    
    if data:
        with open(OUTPUT_FILE, 'w') as f:
            json.dump(data, f, indent=4)
        print(f"\n✓ Successfully saved {len(data)} journeys to {OUTPUT_FILE}")
    else:
        print("\n⚠ No journey data generated.")


if __name__ == "__main__":
    main()
