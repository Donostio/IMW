


import json
import os
from datetime import datetime, timedelta, time as time_obj
import time
# Import requests for robust transport handling
import requests 
from zeep import Client, Settings
from zeep.exceptions import Fault
# Import Transport explicitly for robust connection setup
from zeep.transports import Transport 

# --- Darwin API Configuration (Official Live Data) ---
# NOTE: Your Darwin token must be set as a secret named DARWIN_TOKEN in your GitHub repository
# and passed to the action environment. The token is retrieved from the environment variable.
DARWIN_TOKEN = os.environ.get("DARWIN_TOKEN")
DARWIN_WSDL = "https://lite.realtime.nationalrail.co.uk/OpenLDBWS/ldb.asmx?WSDL"

# --- CRS Codes (National Rail Stations) ---
STREATHAM_COMMON_CRS = "SRC" # Streatham Common
CLAPHAM_JUNCTION_CRS = "CLJ" # Clapham Junction
IMPERIAL_WHARF_CRS = "IMW" # Imperial Wharf

MAX_RETRIES = 5

def create_darwin_client():
    """Initializes the Darwin SOAP client with the required token."""
    
    # Check if the token is available
    if not DARWIN_TOKEN:
        print("ERROR: DARWIN_TOKEN environment variable is missing. Please set it as a GitHub Secret.")
        return None
        
    # Use a requests Session and Transport to ensure robust connection, 
    # helping to avoid unexpected 401 errors during WSDL fetch.
    session = requests.Session()
    transport = Transport(session=session, timeout=10) # Set a 10-second timeout for safety
    
    try:
        # Zeep settings for robust parsing
        settings = Settings(strict=False, xml_huge_tree=True)
        
        # Initialize the client using the WSDL definition and the custom transport
        client = Client(DARWIN_WSDL, settings=settings, transport=transport)
        
        # Create the SOAP header structure containing the access token
        header = client.get_element('ns0:AccessToken')
        token_header = header(TokenValue=DARWIN_TOKEN)
        
        # Set the header on the client object for all future calls (this is the actual authentication)
        client.set_default_header(token_header)
        return client
    except Exception as e:
        # Catch connection and initialization errors
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
            # We use json.dumps/loads as a reliable way to serialize the Zeep object
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
    if not darwin_client: 
        print("Exiting script due to Darwin client initialization failure.")
        return

    # --- Determine the earliest time we care about today ---
    if is_weekend:
        target_start_time = "07:20"
    else:
