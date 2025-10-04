import os
import json
import requests
from datetime import datetime, timedelta
# Import the Darwin LDB Session for live data
from nredarwin.webservice import DarwinLdbSession

# --- Configuration ---
TFL_APP_ID = os.getenv("TFL_APP_ID", "")
TFL_APP_KEY = os.getenv("TFL_APP_KEY", "")
OUTPUT_FILE = "live_data.json"

# --- DARWIN LDBWS Configuration ---
# Read key from environment variable/GitHub Secret
DARWIN_API_KEY = os.getenv("DARWIN_API_KEY", "")
# Recommended specific WSDL version
DARWIN_WSDL = "https://lite.realtime.nationalrail.co.uk/OpenLDBWS/wsdl.aspx?ver=2021-01-01" 

# CRS Codes for the journey stations (3-letter codes required for Darwin)
STREATHAM_COMMON_CRS = "SRC" 
IMPERIAL_WHARF_CRS = "IMW"
CLAPHAM_JUNCTION_CRS = "CLJ"

# Journey parameters
ORIGIN = "Streatham Common Rail Station"
DESTINATION = "Imperial Wharf Rail Station"
TFL_BASE_URL = "https://api.tfl.gov.uk"
NUM_JOURNEYS = 4 # Target the next four journeys
TFL_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"


# --- Darwin Session Initialization ---
try:
    if DARWIN_API_KEY:
        DARWIN_SESSION = DarwinLdbSession(
            wsdl=DARWIN_WSDL, 
            api_key=DARWIN_API_KEY
        )
        print("✓ Darwin LDB Session initialized.")
    else:
        DARWIN_SESSION = None
        print("⚠ Darwin API Key missing. Platform and live arrival data will not be available.")
except Exception as e:
    DARWIN_SESSION = None
    print(f"ERROR initializing Darwin Session: {e}")


# --- Utility Functions ---

def get_journey_plan(origin, destination):
    """Fetch journey plans from TFL Journey Planner API."""
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
        # print(f"API Response Status: {response.status_code}")
        # print(f"Response Keys: {json_data.keys()}")
        # -----------------------
        
        return json_data
        
    except requests.exceptions.HTTPError as errh:
        print(f"ERROR HTTP: {errh}")
    except requests.exceptions.ConnectionError as errc:
        print(f"ERROR Connecting: {errc}")
    except requests.exceptions.Timeout as errt:
        print(f"ERROR Timeout: {errt}")
    except requests.exceptions.RequestException as err:
        print(f"ERROR Unknown Request Error: {err}")
    
    return None


def get_darwin_live_data(departure_crs, scheduled_dep_time, destination_crs):
    """
    Fetches live platform and estimated arrival time for a specific service 
    by matching the scheduled departure time against the Darwin Departure Board.
    """
    if not DARWIN_SESSION:
        return None

    try:
        # Get the station departure board, filtered for the destination
        board = DARWIN_SESSION.get_departures(
            departure_crs, 
            destination_crs=destination_crs,
            rows=10 # Check the next 10 services to find a match
        )
        
        tfl_dep_time = datetime.strptime(scheduled_dep_time, "%H:%M").time()

        for service in board.train_services:
            # Darwin's scheduled time (std) might have an asterisk for uncertainty
            scheduled_time_str = service.std.split('*')[0] 
            
            try:
                darwin_scheduled_time = datetime.strptime(scheduled_time_str, "%H:%M").time()

                # Match the service based on scheduled departure time
                if darwin_scheduled_time == tfl_dep_time:
                    
                    # 1. Get initial departure platform (for leg 1 or Direct)
                    departure_platform = service.platform.text if service.platform else 'TBC'
                    
                    # 2. Get the final ETA and Arrival Platform at the destination
                    service_details = DARWIN_SESSION.get_service_details(service.service_id)
                    
                    # Find the specific calling point (the destination) to get the ETA/Platform
                    live_arrival = 'Unknown'
                    arrival_platform = 'TBC'
                    
                    if service_details and service_details.subsequent_calling_points:
                        # Iterate through subsequent calling points to find the final destination
                        for point in service_details.subsequent_calling_points.calling_point:
                            if point.crs == destination_crs:
                                # ETA (Estimated Time of Arrival) or STA (Scheduled Time of Arrival)
                                live_arrival = point.eta.split('*')[0] if point.eta else point.sta
                                arrival_platform = point.platform.text if point.platform else 'TBC'
                                break
                    
                    return {
                        'liveArrival': live_arrival,
                        'departurePlatform': departure_platform,
                        'arrivalPlatform': arrival_platform
                    }

            except ValueError:
                # Skip services where scheduled time isn't HH:MM (e.g., 'Cancelled')
                continue

        return None # Service not found
    
    except Exception as e:
        print(f"ERROR Darwin lookup failed for {departure_crs} to {destination_crs}: {e}")
        return None

def is_valid_train_journey(journey):
    """Checks if the journey is a 'Direct' train or a 'One Change' train journey."""
    rail_legs = [l for l in journey['legs'] if l['mode']['id'] in ['overground', 'national-rail']]
    
    if len(rail_legs) == 1:
        return True, "Direct"
    elif len(rail_legs) == 2:
        # A two-leg train journey must have a transfer between them (a walking leg)
        # Check if the middle leg is a transfer
        if len(journey['legs']) == 3 and journey['legs'][1].get('mode', {}).get('id') == 'walking':
            return True, "One Change"
    
    return False, "Not Rail"


def process_journey(journey, journey_id):
    """Extracts and standardizes key data from a single TFL journey."""
    
    is_valid, journey_type = is_valid_train_journey(journey)
    if not is_valid:
        return None # Skip invalid journeys
        
    journey_legs = journey['legs']
    train_legs = [l for l in journey_legs if l['mode']['id'] in ['overground', 'national-rail']]
    
    # Initial values from TFL
    journey_start_time = datetime.strptime(journey['startDateTime'], TFL_TIME_FORMAT).strftime("%H:%M")
    journey_end_time = datetime.strptime(journey['arrivalDateTime'], TFL_TIME_FORMAT).strftime("%H:%M")
    total_duration = f"{journey['duration']} min"
    status = journey_legs[0].get('status', 'On Time')
    
    # Overwrite these with live Darwin data if available
    overall_arrival_time = journey_end_time
    
    legs = []
    
    # Map TFL names to CRS codes (Crucial for Darwin)
    CRS_MAP = {
        "Streatham Common Rail Station": STREATHAM_COMMON_CRS,
        "Imperial Wharf Rail Station": IMPERIAL_WHARF_CRS,
        "Clapham Junction Rail Station": CLAPHAM_JUNCTION_CRS,
    }

    for leg_idx, leg in enumerate(journey_legs):
        
        # --- Handle Train/Rail Legs ---
        if leg['mode']['id'] in ['overground', 'national-rail']:
            origin_name = leg['departurePoint']['commonName']
            destination_name = leg['arrivalPoint']['commonName']
            
            # Scheduled times from TFL
            scheduled_dep = datetime.strptime(leg['scheduledDepartureTime'], TFL_TIME_FORMAT).strftime("%H:%M")
            scheduled_arr = datetime.strptime(leg['scheduledArrivalTime'], TFL_TIME_FORMAT).strftime("%H:%M")
            
            operator = leg['instruction'].get('summary', 'Rail Operator')
            
            leg_data = {
                "origin": origin_name.replace(" Rail Station", ""),
                "destination": destination_name.replace(" Rail Station", ""),
                "departure": scheduled_dep,
                "arrival": scheduled_arr,
                "operator": operator.split('(')[0].strip(), # Clean up operator name
                "status": leg.get('status', 'On Time'),
            }
            
            # Get CRS codes for Darwin lookup
            origin_crs = CRS_MAP.get(origin_name)
            destination_crs = CRS_MAP.get(destination_name)

            # --- DARWIN INTEGRATION ---
            if origin_crs and destination_crs:
                darwin_data = get_darwin_live_data(origin_crs, scheduled_dep, destination_crs)
                
                if darwin_data:
                    # 1. Update leg details with live data
                    leg_data["live_arrival"] = darwin_data['liveArrival']
                    leg_data["departurePlatform"] = darwin_data['departurePlatform']
                    leg_data["arrivalPlatform"] = darwin_data['arrivalPlatform']
                    
                    # 2. Update the transfer point platforms (e.g., at Clapham Junction)
                    if journey_type == 'One Change':
                        if leg_idx == 0:
                            # First train leg (SRC -> CLJ): Update CLJ arrival platform
                            leg_data["arrivalPlatform_ClaphamJunction"] = darwin_data['arrivalPlatform']
                        elif leg_idx == 2: # This assumes the transfer is leg 1 and is skipped below
                            # Second train leg (CLJ -> IMW): Update CLJ departure platform
                            leg_data["departurePlatform_ClaphamJunction"] = darwin_data['departurePlatform']

                    # 3. Update the overall journey arrival time with the final leg's live arrival
                    if leg == train_legs[-1]: # Check if this is the last train leg
                        overall_arrival_time = darwin_data['liveArrival']
                        # Recalculate duration based on the live arrival time
                        try:
                            # TFL departure time and live arrival time
                            dep_dt = datetime.strptime(journey_start_time, "%H:%M")
                            arr_dt = datetime.strptime(overall_arrival_time, "%H:%M")
                            
                            # Handle midnight wrap-around for duration calculation
                            if arr_dt < dep_dt:
                                arr_dt += timedelta(days=1)
                                
                            total_duration_minutes = (arr_dt - dep_dt).total_seconds() / 60
                            total_duration = f"{int(total_duration_minutes)} min"
                        except ValueError:
                            # Fallback if live_arrival is 'Unknown' or invalid
                            pass
                
                else:
                    # Darwin failed or service not found - fall back to TFL scheduled data and TBC platforms
                    leg_data["live_arrival"] = scheduled_arr
                    leg_data["departurePlatform"] = "TBC"
                    leg_data["arrivalPlatform"] = "TBC"
            else:
                # If CRS codes are missing (should not happen with the defined journeys)
                leg_data["live_arrival"] = scheduled_arr
                leg_data["departurePlatform"] = "TBC"
                leg_data["arrivalPlatform"] = "TBC"

            legs.append(leg_data)

        # --- Handle Transfer/Walking Legs ---
        elif leg['mode']['id'] == 'walking':
            transfer_time = f"{leg['duration']} min"
            location = leg['departurePoint']['commonName']
            
            legs.append({
                "type": "transfer",
                "location": location.replace(" Rail Station", ""),
                "transferTime": transfer_time,
            })
            
    # Return the dictionary with the potentially updated overall_arrivalTime and totalDuration
    return {
        "id": journey_id,
        "type": journey_type,
        "departureTime": journey_start_time,
        "arrivalTime": overall_arrival_time, # Now the live estimated arrival time
        "totalDuration": total_duration,     # Now based on the live arrival
        "status": status,
        "live_updated_at": datetime.now().strftime("%H:%M:%S"),
        "legs": legs
    }


def fetch_and_process_tfl_data(num_journeys):
    """Fetches TFL data, processes it, and returns data for a fixed number of valid train journeys."""
    
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
        print("\n⚠ Could not generate journey data. Check API keys and logs.")


if __name__ == "__main__":
    main()
