import os
import json
import requests
from datetime import datetime, timedelta

# --- Configuration ---
# Your provided TAPI credentials (Hardcoded for simplicity)
TAPI_APP_ID = "49724146"  
TAPI_APP_KEY = "3d1ee7a7d50d2fda9e8fc8e3dbc58255"
OUTPUT_FILE = "live_data.json"

# Journey parameters (using CRS codes for reliability)
ORIGIN_CRS = "SRC" # Streatham Common
DESTINATION_CRS = "IMW" # Imperial Wharf
# Note: The actual journey filter logic is implemented in the main function.

# TAPI API endpoint
TAPI_BASE_URL = "https://transportapi.com"

# --- Utility Functions ---

def get_journey_plan(origin, destination, time_str):
    """
    Step 1: Fetch scheduled journey plans from TAPI Journey Planner.
    The time_str dictates when the search begins.
    """
    url = f"{TAPI_BASE_URL}/v3/uk/public_journey.json"
    
    # We set the search to start on the current day at 6:30
    now = datetime.now()
    current_date = now.strftime("%Y-%m-%d")
    
    params = {
        "from": origin,
        "to": destination,
        "date": current_date,
        "time": time_str, # Start search from 6:30 AM
        "modes": "train",
        "service": "silverrail", 
        "app_id": TAPI_APP_ID,
        "app_key": TAPI_APP_KEY
    }
    
    try:
        print(f"[{now.isoformat()}] Step 1: Fetching scheduled journeys from {origin} to {destination} starting at {time_str}...")
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        
        json_data = response.json()
        return json_data
        
    except requests.exceptions.RequestException as e:
        print(f"ERROR: Failed to fetch TAPI Journey Planner data: {e}")
        return None

# [The rest of the utility functions (get_live_departures, format_time, etc.) remain unchanged]

# --- PASTE THE REST OF THE UTILITY FUNCTIONS HERE ---
# (get_live_departures, format_time, parse_duration, time_to_seconds, find_live_data)
# ----------------------------------------------------------------------------------

def get_live_departures(station_code):
    """
    Step 2: Fetch live departure board data for a specific station (Streatham Common). (1 API Call)
    Returns real-time service information including platforms and delays.
    """
    # Use 'datetime' parameter to get current/future departures around now
    now_utc = datetime.utcnow().isoformat() + 'Z'
    
    url = f"{TAPI_BASE_URL}/v3/uk/train/station/{station_code}/actual_journeys.json"
    
    params = {
        "station": station_code,
        "datetime": now_utc,
        "from_offset": "-PT00:05:00", # Look 5 min in the past
        "to_offset": "PT02:00:00",    # Look 2 hours in the future
        "type": "departure",
        "limit": 20,                  # Get enough services to match against
        "expected": "true",           # Ensure we get the expected times
        "app_id": TAPI_APP_ID,
        "app_key": TAPI_APP_KEY
    }
    
    try:
        print(f"[{datetime.now().isoformat()}] Step 2: Fetching live departures for {station_code}...")
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        
        live_data = response.json()
        
        return live_data.get('member', [])
        
    except requests.exceptions.RequestException as e:
        print(f"ERROR: Failed to fetch TAPI Live Departure Board data: {e}")
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
    Matches the scheduled time (from Journey Planner) to a live service (from LDB).
    This implements the strict "time not as planned" status logic.
    """
    
    # Scheduled time in seconds for easy comparison
    scheduled_seconds = time_to_seconds(scheduled_departure_time)
    
    # Default return for no match
    default_live = {"status": "On Time", "platform": "TBC", "live_time": scheduled_departure_time}

    # Match within a 2-minute window of the aimed (scheduled) time
    for service in live_departures:
        aimed_time_str = service.get('aimed', {}).get('departure', {}).get('time')
        
        if not aimed_time_str:
            continue
            
        aimed_seconds = time_to_seconds(aimed_time_str)
        
        # Use an error margin of 2 minutes (120 seconds) for matching
        if abs(scheduled_seconds - aimed_seconds) <= 120:
            
            platform = service.get('platform') or "TBC"
            
            # --- STRICT STATUS LOGIC: Check for delay or cancellation ---
            is_cancelled = service.get('cancelled', False)
            # running_late = service.get('service', {}).get('running_late') # Not used for strict time check

            # Get the expected time for the live status
            expected_time_str = service.get('expected', {}).get('departure', {}).get('time')
            
            if is_cancelled:
                status = "Canceled"
                live_time = "N/A"
            elif expected_time_str and expected_time_str != aimed_time_str:
                # Flag as 'Delayed' only if the expected time is different from the aimed time
                status = "Delayed"
                live_time = expected_time_str
            else:
                # If times match
                status = "On Time"
                live_time = aimed_time_str

            return {"status": status, "platform": platform, "live_time": live_time}
            
    return default_live

# ----------------------------------------------------------------------------------


def process_journey_data(journey_data, filter_config):
    """Extract and format journey data from TAPI response, applying time and type filters."""
    routes = journey_data.get('routes', [])
    processed_journeys = []
    
    # Fetch live departures once for the current time window for the origin station (SRC)
    live_departures_src = get_live_departures(ORIGIN_CRS)
    
    print(f"Found {len(live_departures_src)} live train services departing {ORIGIN_CRS}.")
    
    for idx, route in enumerate(routes, 1):
        
        # 1. ROUTE TYPE CHECK (Direct vs One Change)
        route_parts = route.get('route_parts', [])
        train_legs = [part for part in route_parts if part.get('mode') == 'train']
        route_type = "Direct" if len(train_legs) == 1 else "One Change"
        
        # 2. DEPARTURE TIME CHECK (filter out journeys that depart too early/late)
        scheduled_departure_time = route.get('departure_time')
        dep_seconds = time_to_seconds(scheduled_departure_time)
        
        # Convert filter times (7:20, 7:25) to seconds for comparison
        target_time_seconds = time_to_seconds("07:25")
        
        # --- WEEKEND FILTER LOGIC ---
        if filter_config['is_weekend']:
            # We want the *first* direct journey *after* 7:20 AM
            target_after_seconds = time_to_seconds("07:20")
            
            # Skip if: 1) Not Direct OR 2) Departs before 7:20
            if route_type != "Direct" or dep_seconds < target_after_seconds:
                continue
                
            # If we find the first valid direct journey, we take it and stop searching
            if len(processed_journeys) >= 1:
                break
                
        # --- WEEKDAY FILTER LOGIC ---
        else: # Weekday (Mon-Fri)
            # We want the next *two indirect* journeys *after* 7:25 AM
            
            # Skip if: 1) Is Direct OR 2) Departs before 7:25
            if route_type != "One Change" or dep_seconds < target_time_seconds:
                continue
            
            # Stop once we have the two indirect journeys
            if len(processed_journeys) >= 2:
                break
        
        # If the journey passes all filters:
        
        # Overall Journey Details
        total_duration = parse_duration(route.get('duration', '00:00:00'))
        scheduled_arrival_time = route.get('arrival_time')
        first_train_leg = train_legs[0]
        
        # Get live data for the first train leg leaving Streatham Common
        live_info = find_live_data(first_train_leg.get('departure_time'), live_departures_src)
        
        processed_legs = []
        
        if route_type == "Direct":
            # Direct Train structure... (simplified for brevity)
            processed_legs.append({
                "origin": "Streatham Common",
                "destination": "Imperial Wharf",
                "scheduledDeparture": format_time(scheduled_departure_time),
                "liveDeparture": live_info['live_time'],
                "scheduledArrival": format_time(scheduled_arrival_time),
                "departurePlatform": live_info['platform'],
                "operator": first_train_leg.get('operator_name', 'Rail'),
                "status": live_info['status']
            })

        elif route_type == "One Change":
            # Two-Train Journey structure... (simplified for brevity)
            leg1 = train_legs[0]
            leg2 = train_legs[1]
            interchange = leg1.get('to_point_name', 'Clapham Junction')
            
            # Leg 1: Streatham Common to Interchange (Live data applied here)
            processed_legs.append({
                "origin": "Streatham Common",
                "destination": interchange,
                "scheduledDeparture": leg1.get('departure_time'),
                "liveDeparture": live_info['live_time'],
                "scheduledArrival": leg1.get('arrival_time'),
                "departurePlatform": live_info['platform'],
                "operator": leg1.get('operator_name', 'Rail'),
                "status": live_info['status']
            })
            
            # Transfer Detail (look for foot leg, etc.)
            transfer_mins = 0 # Placeholder for transfer logic
            processed_legs.append({"type": "transfer", "location": interchange, "transferTime": f"{transfer_mins} min"})
            
            # Leg 2: Interchange to Imperial Wharf 
            processed_legs.append({
                "origin": interchange,
                "destination": "Imperial Wharf",
                "scheduledDeparture": leg2.get('departure_time'),
                "liveDeparture": leg2.get('departure_time'), 
                "scheduledArrival": leg2.get('arrival_time'),
                "departurePlatform_Interchange": "TBC (Scheduled)", 
                "operator": leg2.get('operator_name', 'Rail'),
                "status": "On Time" 
            })
            
        processed_journeys.append({
            "id": len(processed_journeys) + 1,
            "type": route_type,
            "scheduledDepartureTime": format_time(scheduled_departure_time),
            "scheduledArrivalTime": format_time(scheduled_arrival_time),
            "totalDuration": f"{total_duration} min",
            "overallStatus": live_info['status'], 
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
        # We start the search at 6:30 AM to capture all relevant trains
        'search_start_time': "06:30" 
    }
    
    if is_weekend:
        print("Weekend Mode: Targeting the first direct journey after 07:20.")
    else:
        print("Weekday Mode: Targeting the next two indirect journeys after 07:25.")
    # --- END DYNAMIC CONFIGURATION ---

    # 1. TAPI Journey Planner Call
    journey_data = get_journey_plan(ORIGIN_CRS, DESTINATION_CRS, filter_config['search_start_time'])
    
    if not journey_data or 'routes' not in journey_data:
        print("ERROR: No route data received from TAPI Journey Planner.")
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
        print(f"Total API calls per run: 2 (1 Journey Planner + 1 Live Board).")
    else:
        print("\n⚠ No journey data generated.")


if __name__ == "__main__":
    main()
