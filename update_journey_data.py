import json
import os
from datetime import datetime, timedelta, time as time_obj
import time
from zeep import Client, Settings
from zeep.exceptions import Fault
from zeep.transports import Transport

# --- Darwin API Configuration (Official Live Data) ---
# NOTE: Your Darwin token is hardcoded here, as requested.
DARWIN_TOKEN = "8aaaf362-b5d6-4886-9c24-08e137bd4a7b"
DARWIN_WSDL = "https://lite.realtime.nationalrail.co.uk/OpenLDBWS/ldb.asmx?WSDL"

# --- CRS Codes (National Rail Stations) ---
STREATHAM_COMMON_CRS = "SRC" # Streatham Common
CLAPHAM_JUNCTION_CRS = "CLJ" # Clapham Junction
IMPERIAL_WHARF_CRS = "IMW" # Imperial Wharf

MAX_RETRIES = 5

def create_darwin_client():
    """Initializes the Darwin SOAP client with the required token."""
    try:
        # Zeep settings for robust parsing
        settings = Settings(strict=False, xml_huge_tree=True)
        
        # Initialize the client using the WSDL definition
        client = Client(DARWIN_WSDL, settings=settings)
        
        # Create the SOAP header structure containing the access token
        header = client.get_element('ns0:AccessToken')
        token_header = header(TokenValue=DARWIN_TOKEN)
        
        # Set the header on the client object for all future calls
        client.set_default_header(token_header)
        return client
    except Exception as e:
        print(f"ERROR: Failed to initialize Darwin client: {e}")
        return None

def get_darwin_departure_board(client, crs_code, time_offset_minutes=0, time_window_minutes=60):
    """
    Fetches the live departure board for a station using the Darwin API.
    
    :param client: The initialized Zeep client with the auth header.
    :param crs_code: The CRS code of the station (e.g., "SRC").
    :param time_offset_minutes: Lookahead offset in minutes from now.
    :param time_window_minutes: Total duration of the results to retrieve.
    :returns: JSON-like structure of the departure board or None on failure.
    """
    
    print(f"Fetching Darwin LDB for {crs_code} with offset {time_offset_minutes} minutes.")
    
    for attempt in range(MAX_RETRIES):
        try:
            # Call the GetDepartureBoard method
            response = client.service.GetDepartureBoard(
                crs=crs_code,
                timeOffset=time_offset_minutes,
                timeWindow=time_window_minutes
            )
            
            # The response is a complex Zeep object, we convert it to a dict for easier parsing
            return json.loads(json.dumps(response, default=lambda o: getattr(o, '__dict__', str(o))))
            
        except Fault as e:
            print(f"ERROR: Darwin SOAP Fault on attempt {attempt + 1} ({crs_code}): {e}")
            time.sleep(2 ** attempt)
        except Exception as e:
            print(f"ERROR: General Darwin API failure on attempt {attempt + 1} ({crs_code}): {e}")
            time.sleep(2 ** attempt)
            
    return None

def extract_train_details(service):
    """Parses a single Darwin service object into a simplified dictionary."""
    if not service:
        return None

    # Get the final destination name
    destination_name = 'Unknown'
    destinations = service.get('destination', {}).get('location', [])
    if isinstance(destinations, list) and destinations:
        destination_name = destinations[0].get('locationName', 'Unknown')
    elif isinstance(destinations, dict):
        destination_name = destinations.get('locationName', 'Unknown')
        
    # Get the real-time arrival at Imperial Wharf (IMW) using calling points
    target_arrival_imw = 'N/A'
    # SubsequentCallingPoints is a list of lists/dicts depending on the data structure
    calling_point_lists = service.get('subsequentCallingPoints', [])
    if calling_point_lists and isinstance(calling_point_lists, list):
        # We need to dig into the structure to find the actual list of calling points
        if isinstance(calling_point_lists[0], dict) and calling_point_lists[0].get('callingPoint'):
            for cp in calling_point_lists[0]['callingPoint']:
                if cp.get('crs') == IMPERIAL_WHARF_CRS:
                    # eta (Expected Time of Arrival), sta (Scheduled Time of Arrival)
                    target_arrival_imw = cp.get('eta', cp.get('sta', 'N/A'))
                    break

    return {
        'service_id': service.get('serviceID', 'N/A'),
        'aimed_dep': service.get('std', 'N/A'), # Scheduled Departure Time
        'expected_dep': service.get('etd', 'On Time'), # Expected Departure Time (Live)
        'platform': service.get('platform', 'TBC'), # Live Platform Number
        'destination_name': destination_name,
        'live_arrival_at_IMW': target_arrival_imw, 
    }

def find_trains_for_leg(ldb_data, target_crs, start_time_str, max_results=None):
    """
    Filters the Darwin LDB data based on the required destination and scheduled time.
    """
    if not ldb_data or 'trainServices' not in ldb_data:
        return []

    filtered_trains = []
    
    try:
        # Convert required start time string (e.g., "07:25") to time object
        target_dt = datetime.strptime(start_time_str, "%H:%M").time()
    except ValueError:
        return []
    
    train_services = ldb_data['trainServices']
    if not isinstance(train_services, list):
        # If there's only one service, it might be returned as a dict, convert to list
        train_services = [train_services] 

    for service in train_services:
        
        std_time_str = service.get('std')
        if not std_time_str: continue
            
        try:
            std_dt = datetime.strptime(std_time_str, "%H:%M").time()
        except ValueError:
            continue
            
        # 1. Check time: must be after the required scheduled time
        if std_dt >= target_dt:
            
            # 2. Check destination: 
            
            destination_name = 'Unknown'
            destinations = service.get('destination', {}).get('location', [])
            if isinstance(destinations, list) and destinations:
                destination_name = destinations[0].get('locationName', 'Unknown')
            
            is_valid_train = False
            
            if target_crs == IMPERIAL_WHARF_CRS: # Weekend direct train or CLJ connection
                if destination_name in ['Imperial Wharf', 'Clapham Junction']:
                     is_valid_train = True
            
            if target_crs == CLAPHAM_JUNCTION_CRS: # Weekday first leg train (SRC -> CLJ)
                 # From SRC, almost all trains stop at CLJ, so we only need to filter by time here.
                 is_valid_train = True 

            if is_valid_train:
                details = extract_train_details(service)
                filtered_trains.append(details)
        
        if max_results and len(filtered_trains) >= max_results:
            break

    return filtered_trains

def process_morning_data():
    """
    Executes the scheduled data fetching using Darwin.
    """
    now = datetime.now()
    # Note: Darwin works best using the actual current time and internal time windows.
    target_date = now.strftime("%Y-%m-%d") 
    
    is_weekend = now.weekday() >= 5
    result_data = {"query_time": now.isoformat(), "journeys": []}
    
    darwin_client = create_darwin_client()
    if not darwin_client: return

    # --- Determine the earliest time we care about today ---
    if is_weekend:
        target_start_time = "07:20"
    else:
        target_start_time = "07:25"
    
    # --- API Lookahead is handled by the GitHub Action schedule running near the target time ---
    time_offset_minutes = 0 # Look 0 minutes ahead (start now)
    
    if is_weekend:
        # Weekend: Direct SRC to IMW after 07:20
        print(f"Running Weekend Logic (Direct train after {target_start_time} via Darwin)")
        
        # 1. Fetch LDB for Streatham Common (SRC)
        src_ldb = get_darwin_departure_board(darwin_client, STREATHAM_COMMON_CRS, time_offset_minutes=time_offset_minutes)
        if not src_ldb: return

        # 2. Find the first direct train from SRC to IMW after 07:20
        direct_trains = find_trains_for_leg(
            src_ldb, 
            IMPERIAL_WHARF_CRS, 
            target_start_time, 
            max_results=1
        )

        result_data["journeys"].extend([{"leg1": t} for t in direct_trains])
        
    else: # Weekday logic
        # Weekday: SRC to CLJ, then CLJ to IMW (next two indirect after 07:25)
        print(f"Running Weekday Logic (2 indirect journeys after {target_start_time} via Darwin)")
        
        # 1. Fetch LDB for Streatham Common (SRC)
        src_ldb = get_darwin_departure_board(darwin_client, STREATHAM_COMMON_CRS, time_offset_minutes=time_offset_minutes)
        if not src_ldb: return

        # 2. Find the next two trains from SRC going via Clapham Junction (CLJ) after 07:25
        first_leg_trains = find_trains_for_leg(
            src_ldb, 
            CLAPHAM_JUNCTION_CRS, 
            target_start_time, 
            max_results=2
        )

        for leg1 in first_leg_trains:
            # 3. Estimate Connection Time at CLJ
            # We assume a 10 minute journey from SRC to CLJ + 5 min connection buffer = 15 mins total.
            
            try:
                # Use the scheduled departure time (aimed_dep) for the calculation
                dep_dt = datetime.strptime(f"{target_date} {leg1['aimed_dep']}", "%Y-%m-%d %H:%M")
                earliest_connection_dt = dep_dt + timedelta(minutes=15)
                connection_time_str = earliest_connection_dt.strftime("%H:%M")
            except:
                 # Fallback to current time if parsing fails
                 connection_time_str = now.strftime("%H:%M") 

            # 4. Fetch LIVE LDB for Clapham Junction (CLJ)
            clj_ldb = get_darwin_departure_board(darwin_client, CLAPHAM_JUNCTION_CRS)
            if not clj_ldb: continue

            # 5. Find the next available train from CLJ to IMW after the estimated connection time
            connection_trains = find_trains_for_leg(
                clj_ldb, 
                IMPERIAL_WHARF_CRS, 
                connection_time_str, 
                max_results=1
            )

            if connection_trains:
                result_data["journeys"].append({
                    "leg1": leg1,
                    "leg2": connection_trains[0]
                })

    # --- Save Data to JSON File ---
    try:
        with open('live_data.json', 'w') as f:
            json.dump(result_data, f, indent=4)
        print(f"Successfully saved {len(result_data['journeys'])} journeys to live_data.json using Darwin.")
    except Exception as e:
        print(f"ERROR: Failed to write to live_data.json: {e}")

if __name__ == "__main__":
    process_morning_data()


