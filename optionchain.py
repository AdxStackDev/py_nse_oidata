import requests
import logging
import re
import json
from storage import storage

NSE_OPTION_CHAIN_URL = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"
NSE_OPTION_CHAIN_PAGE = "https://www.nseindia.com/option-chain"
NSE_HOME_URL = "https://www.nseindia.com"
STORAGE_KEY = "option_chain_state"

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Referer": "https://www.nseindia.com/option-chain",
    "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin"
}

def load_previous_data():
    """Load previous option chain data from storage (Redis or in-memory)."""
    data = storage.get_json(STORAGE_KEY)
    return data if data else {}

def save_current_data(data):
    """Save current option chain data to storage (Redis or in-memory)."""
    # Store with 24 hour expiration
    storage.set_json(STORAGE_KEY, data, ex=86400)

def get_browser_session():
    """Create a properly configured session that mimics a real browser."""
    session = requests.Session()
    
    # Enhanced headers to closely mimic Chrome browser
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0"
    }
    
    session.headers.update(headers)
    return session


def extract_data_from_page(html_content):
    """Extract option chain data from the HTML page if API fails."""
    try:
        # Look for embedded JSON data in script tags
        # NSE often embeds data in window.__NUXT__ or similar variables
        script_pattern = r'window\.__NUXT__\s*=\s*({.*?});'
        match = re.search(script_pattern, html_content, re.DOTALL)
        
        if match:
            try:
                data_str = match.group(1)
                data = json.loads(data_str)
                logging.info("Found embedded data in __NUXT__ variable")
                return data
            except json.JSONDecodeError:
                pass
        
        # Alternative pattern for option chain data
        option_data_pattern = r'optionChainData\s*[:=]\s*({.*?});'
        match = re.search(option_data_pattern, html_content, re.DOTALL)
        
        if match:
            try:
                data_str = match.group(1)
                data = json.loads(data_str)
                logging.info("Found option chain data in embedded script")
                return data
            except json.JSONDecodeError:
                pass
                
        logging.warning("Could not extract data from HTML page")
        return None
        
    except Exception as e:
        logging.error(f"Error extracting data from page: {e}")
        return None


def fetch_raw_data():
    """Fetch raw option chain data from NSE with multiple fallback methods."""
    session = get_browser_session()
    
    try:
        # Method 1: Visit home page first to establish session
        logging.info("Step 1: Visiting NSE home page...")
        home_resp = session.get(NSE_HOME_URL, timeout=20)
        home_resp.raise_for_status()
        logging.info(f"Home page status: {home_resp.status_code}")
        
        # Small delay to mimic human behavior
        import time
        time.sleep(1)
        
        # Method 2: Visit option chain page to set proper context
        logging.info("Step 2: Visiting option chain page...")
        session.headers.update({
            "Referer": NSE_HOME_URL,
            "Sec-Fetch-Site": "same-origin"
        })
        
        page_resp = session.get(NSE_OPTION_CHAIN_PAGE, timeout=20)
        page_resp.raise_for_status()
        logging.info(f"Option chain page status: {page_resp.status_code}")
        
        # Try to extract data from the page HTML first
        page_data = extract_data_from_page(page_resp.text)
        if page_data:
            logging.info("✅ Successfully extracted data from HTML page")
            return page_data
        
        # Method 3: Try API call with proper session
        time.sleep(2)  # Another delay
        
        logging.info("Step 3: Attempting API call...")
        session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Referer": NSE_OPTION_CHAIN_PAGE,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "X-Requested-With": "XMLHttpRequest"
        })
        
        api_resp = session.get(NSE_OPTION_CHAIN_URL, timeout=20)
        
        if api_resp.status_code == 200:
            try:
                data = api_resp.json()
                logging.info("✅ Successfully fetched data via API")
                return data
            except json.JSONDecodeError as e:
                logging.error(f"Failed to parse API JSON response: {e}")
                logging.error(f"Response content: {api_resp.text[:500]}...")
        else:
            logging.warning(f"API request failed with status {api_resp.status_code}")
            logging.warning(f"Response: {api_resp.text[:200]}...")
        
        # Method 4: Try alternative API endpoints
        logging.info("Step 4: Trying alternative endpoints...")
        alt_urls = [
            "https://www.nseindia.com/api/option-chain-equities?symbol=NIFTY",
            "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY50"
        ]
        
        for alt_url in alt_urls:
            try:
                alt_resp = session.get(alt_url, timeout=15)
                if alt_resp.status_code == 200:
                    data = alt_resp.json()
                    logging.info(f"✅ Success with alternative URL: {alt_url}")
                    return data
            except:
                continue
        
        logging.error("All methods failed to fetch NSE data")
        return None
        
    except requests.exceptions.Timeout:
        logging.error("Request timeout while fetching option chain data")
        return None
    except requests.exceptions.ConnectionError:
        logging.error("Connection error while fetching option chain data")
        return None
    except requests.exceptions.RequestException as e:
        logging.error(f"Request error: {e}")
        return None
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
        return None
    finally:
        session.close()

def get_option_chain_data():
    """
    Fetch and process option chain data with difference tracking.
    Falls back to mock data if NSE API is not accessible.
    Returns: (processed_rows, spot_price, atm_strike)
    """
    # Try to fetch real data first
    raw_data = fetch_raw_data()
    previous_data = load_previous_data()
    
    if not raw_data:
        logging.warning("NSE API not accessible, using mock data for development")
        return get_mock_option_chain_data()

    # Extract records from NSE API response
    records = raw_data.get("records", {})
    if not records:
        logging.warning("No 'records' key found in NSE response, using mock data")
        return get_mock_option_chain_data()
    
    spot_price = records.get("underlyingValue", 0)
    if spot_price:
        logging.info(f"Current NIFTY spot price: {spot_price}")
    else:
        logging.warning("No spot price found in response")
    
    # Get all option chain data
    all_data = records.get("data", [])
    if not all_data:
        logging.warning("No option chain data found in 'data' key, using mock data")
        return get_mock_option_chain_data()

    # Get expiry dates and filter for nearest 2 expiries
    expiry_dates = records.get("expiryDates", [])
    if not expiry_dates:
        logging.warning("No expiry dates found, using mock data")
        return get_mock_option_chain_data()
    
    # Take only first 2 expiries (nearest)
    target_expiries = expiry_dates[:2]
    logging.info(f"Processing {len(target_expiries)} expiries: {target_expiries}")
    
    # Filter data for target expiries only
    filtered_data = [item for item in all_data if item.get("expiryDate") in target_expiries]
    
    if not filtered_data:
        logging.warning(f"No data found for target expiries: {target_expiries}, using mock data")
        return get_mock_option_chain_data()

    logging.info(f"Found {len(filtered_data)} option chain entries for target expiries")

    # Calculate ATM strike (nearest to spot price, rounded to 50)
    atm_strike = int(round(spot_price / 50) * 50) if spot_price else None
    if atm_strike:
        logging.info(f"Calculated ATM Strike: {atm_strike}")

    processed_rows = []
    new_session_data = previous_data.copy()
    errors_count = 0

    for item in filtered_data:
        try:
            strike = item.get("strikePrice")
            expiry = item.get("expiryDate")
            
            if not strike or not expiry:
                errors_count += 1
                continue
            
            # Extract CE (Call) and PE (Put) data
            ce = item.get("CE", {})
            pe = item.get("PE", {})
            
            # Current CE values with safe defaults
            curr_ce = {
                "OI": int(ce.get("openInterest", 0) or 0),
                "ChangeInOI": int(ce.get("changeinOpenInterest", 0) or 0),
                "Volume": int(ce.get("totalTradedVolume", 0) or 0),
                "IV": round(float(ce.get("impliedVolatility", 0) or 0), 2),
                "LTP": round(float(ce.get("lastPrice", 0) or 0), 2)
            }
            
            # Current PE values with safe defaults
            curr_pe = {
                "OI": int(pe.get("openInterest", 0) or 0),
                "ChangeInOI": int(pe.get("changeinOpenInterest", 0) or 0),
                "Volume": int(pe.get("totalTradedVolume", 0) or 0),
                "IV": round(float(pe.get("impliedVolatility", 0) or 0), 2),
                "LTP": round(float(pe.get("lastPrice", 0) or 0), 2)
            }

            # Get previous values for difference calculation
            # Use strike + expiry as key for uniqueness across expiries
            strike_key = f"{strike}_{expiry}"
            prev_item = previous_data.get(strike_key, {})
            prev_ce = prev_item.get("CE", {"OI": 0, "ChangeInOI": 0, "Volume": 0})
            prev_pe = prev_item.get("PE", {"OI": 0, "ChangeInOI": 0, "Volume": 0})

            # Calculate differences from previous session
            diff_ce = {
                "OI": curr_ce["OI"] - prev_ce.get("OI", 0),
                "ChangeInOI": curr_ce["ChangeInOI"] - prev_ce.get("ChangeInOI", 0),
                "Volume": curr_ce["Volume"] - prev_ce.get("Volume", 0)
            }
            
            diff_pe = {
                "OI": curr_pe["OI"] - prev_pe.get("OI", 0),
                "ChangeInOI": curr_pe["ChangeInOI"] - prev_pe.get("ChangeInOI", 0),
                "Volume": curr_pe["Volume"] - prev_pe.get("Volume", 0)
            }

            row_data = {
                "strikePrice": int(strike),
                "expiryDate": expiry,
                "CE": curr_ce,
                "PE": curr_pe,
                "diffCE": diff_ce,
                "diffPE": diff_pe
            }
            
            processed_rows.append(row_data)
            
            # Store current data for next comparison
            new_session_data[strike_key] = {"CE": curr_ce, "PE": curr_pe}
            
        except (ValueError, TypeError) as e:
            logging.error(f"Data conversion error for strike {strike}: {e}")
            errors_count += 1
            continue
        except Exception as e:
            logging.error(f"Unexpected error processing strike {strike}: {e}")
            errors_count += 1
            continue

    if errors_count > 0:
        logging.warning(f"Encountered {errors_count} errors while processing option chain data")

    # Sort by expiry date first, then by strike price
    processed_rows.sort(key=lambda x: (x["expiryDate"], x["strikePrice"]))
    
    # Save current data as previous data for next session
    if processed_rows:
        save_current_data(new_session_data)
        logging.info(f"✅ Successfully processed {len(processed_rows)} option chain rows")
    else:
        logging.warning("No rows processed successfully, using mock data")
        return get_mock_option_chain_data()

    return processed_rows, spot_price, atm_strike


def test_nse_website_access():
    """Test direct access to NSE option chain website."""
    print("🌐 Testing NSE Website Access...")
    print("=" * 60)
    
    session = get_browser_session()
    
    try:
        # Test 1: Home page access
        print("1. Testing NSE home page access...")
        home_resp = session.get(NSE_HOME_URL, timeout=15)
        print(f"   Status: {home_resp.status_code}")
        print(f"   Response size: {len(home_resp.content)} bytes")
        
        if "NSE" in home_resp.text or "National Stock Exchange" in home_resp.text:
            print("   ✅ Home page content looks valid")
        else:
            print("   ⚠️ Home page content may be blocked")
        
        # Test 2: Option chain page access
        print("\n2. Testing option chain page access...")
        page_resp = session.get(NSE_OPTION_CHAIN_PAGE, timeout=15)
        print(f"   Status: {page_resp.status_code}")
        print(f"   Response size: {len(page_resp.content)} bytes")
        
        if "option" in page_resp.text.lower() or "nifty" in page_resp.text.lower():
            print("   ✅ Option chain page content looks valid")
        else:
            print("   ⚠️ Option chain page content may be blocked")
        
        # Test 3: Look for embedded data
        print("\n3. Checking for embedded data...")
        if "window.__NUXT__" in page_resp.text:
            print("   ✅ Found __NUXT__ data structure")
        elif "optionChainData" in page_resp.text:
            print("   ✅ Found optionChainData structure")
        else:
            print("   ⚠️ No obvious data structures found")
        
        # Test 4: API endpoint
        print("\n4. Testing API endpoint...")
        session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Referer": NSE_OPTION_CHAIN_PAGE,
            "X-Requested-With": "XMLHttpRequest"
        })
        
        api_resp = session.get(NSE_OPTION_CHAIN_URL, timeout=15)
        print(f"   Status: {api_resp.status_code}")
        
        if api_resp.status_code == 200:
            try:
                data = api_resp.json()
                print("   ✅ API returned valid JSON")
                if "records" in data:
                    records = data["records"]
                    print(f"   Spot price: {records.get('underlyingValue', 'Not found')}")
                    print(f"   Expiry dates: {len(records.get('expiryDates', []))}")
                    print(f"   Data entries: {len(records.get('data', []))}")
                else:
                    print("   ⚠️ No 'records' key in API response")
            except json.JSONDecodeError:
                print("   ❌ API returned invalid JSON")
                print(f"   Response preview: {api_resp.text[:200]}...")
        else:
            print(f"   ❌ API failed with status {api_resp.status_code}")
        
    except Exception as e:
        print(f"   ❌ Error: {e}")
    finally:
        session.close()
    
    print("=" * 60)


def get_mock_option_chain_data():
    """
    Generate mock option chain data for testing when NSE API is not accessible.
    Returns the same structure as real data.
    """
    import datetime
    
    # Mock spot price around current market levels
    spot_price = 24500.75
    atm_strike = int(round(spot_price / 50) * 50)
    
    # Generate mock expiry dates
    today = datetime.date.today()
    expiry1 = today + datetime.timedelta(days=7)  # Weekly expiry
    expiry2 = today + datetime.timedelta(days=28)  # Monthly expiry
    
    expiry1_str = expiry1.strftime("%d-%b-%Y")
    expiry2_str = expiry2.strftime("%d-%b-%Y")
    
    processed_rows = []
    
    # Generate strikes around ATM (±500 points, 50 point intervals)
    strikes = range(atm_strike - 500, atm_strike + 550, 50)
    
    for i, strike in enumerate(strikes):
        for expiry in [expiry1_str, expiry2_str]:
            # Mock CE data
            ce_oi = max(0, 50000 - abs(strike - atm_strike) * 100 + (i * 1000))
            ce_volume = max(0, 10000 - abs(strike - atm_strike) * 20)
            ce_ltp = max(0.05, 200 - abs(strike - atm_strike) * 0.5)
            
            # Mock PE data  
            pe_oi = max(0, 45000 - abs(strike - atm_strike) * 90 + (i * 800))
            pe_volume = max(0, 8000 - abs(strike - atm_strike) * 15)
            pe_ltp = max(0.05, 180 - abs(strike - atm_strike) * 0.4)
            
            row_data = {
                "strikePrice": strike,
                "expiryDate": expiry,
                "CE": {
                    "OI": int(ce_oi),
                    "ChangeInOI": int((ce_oi * 0.1) - 1000),
                    "Volume": int(ce_volume),
                    "IV": round(15.5 + abs(strike - atm_strike) * 0.01, 2),
                    "LTP": round(ce_ltp, 2)
                },
                "PE": {
                    "OI": int(pe_oi),
                    "ChangeInOI": int((pe_oi * 0.08) - 800),
                    "Volume": int(pe_volume),
                    "IV": round(16.2 + abs(strike - atm_strike) * 0.01, 2),
                    "LTP": round(pe_ltp, 2)
                },
                "diffCE": {
                    "OI": int(ce_oi * 0.05),
                    "ChangeInOI": int(ce_oi * 0.02),
                    "Volume": int(ce_volume * 0.1)
                },
                "diffPE": {
                    "OI": int(pe_oi * 0.04),
                    "ChangeInOI": int(pe_oi * 0.03),
                    "Volume": int(pe_volume * 0.12)
                }
            }
            processed_rows.append(row_data)
    
    # Sort by expiry then strike
    processed_rows.sort(key=lambda x: (x["expiryDate"], x["strikePrice"]))
    
    logging.info(f"📊 Generated {len(processed_rows)} mock option chain rows")
    return processed_rows, spot_price, atm_strike


def test_option_chain():
    """Test function to debug option chain data fetching."""
    import json
    
    print("🧪 Testing Option Chain Data Fetch...")
    print("=" * 50)
    
    # First test website access
    test_nse_website_access()
    
    # Test raw data fetch
    print("\n📡 Testing Raw Data Fetch...")
    print("=" * 50)
    raw_data = fetch_raw_data()
    
    if raw_data:
        print("✅ Raw data fetch successful")
        print(f"   Keys in response: {list(raw_data.keys())}")
        
        records = raw_data.get("records", {})
        if records:
            print(f"   Keys in records: {list(records.keys())}")
            print(f"   Spot price: {records.get('underlyingValue', 'Not found')}")
            print(f"   Number of expiry dates: {len(records.get('expiryDates', []))}")
            print(f"   Number of data entries: {len(records.get('data', []))}")
            
            # Show first few expiry dates
            expiries = records.get('expiryDates', [])[:3]
            print(f"   First 3 expiries: {expiries}")
        else:
            print("❌ No 'records' key in response")
    else:
        print("❌ Raw data fetch failed")
    
    print("\n📊 Testing Processed Data...")
    print("=" * 50)
    processed_data, spot_price, atm_strike = get_option_chain_data()
    
    print(f"   Processed rows: {len(processed_data)}")
    print(f"   Spot price: {spot_price}")
    print(f"   ATM strike: {atm_strike}")
    
    if processed_data:
        print(f"   Sample row structure: {list(processed_data[0].keys())}")
        print(f"   First strike: {processed_data[0]['strikePrice']}")
        print(f"   First expiry: {processed_data[0]['expiryDate']}")
        
        # Show some sample data
        print(f"   Sample CE data: OI={processed_data[0]['CE']['OI']}, LTP={processed_data[0]['CE']['LTP']}")
        print(f"   Sample PE data: OI={processed_data[0]['PE']['OI']}, LTP={processed_data[0]['PE']['LTP']}")
    
    print("=" * 50)
    print("🧪 Test completed!")


if __name__ == "__main__":
    # Set up logging for testing
    import logging
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    
    test_option_chain()