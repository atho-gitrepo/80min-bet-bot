import requests
import os
import json
import time
import logging
from datetime import datetime, timedelta
import firebase_admin
from firebase_admin import credentials, firestore

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("FootballBettingBot")

# Load environment variables
# NOTE: In a real-world scenario, you must ensure these environment variables are set.
API_KEY = os.getenv("API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FIREBASE_CREDENTIALS_JSON_STRING = os.getenv("FIREBASE_CREDENTIALS_JSON")

HEADERS = {
    'x-rapidapi-key': API_KEY,
    'x-rapidapi-host': 'v3.football.api-sports.io'
}
BASE_URL = 'https://v3.football.api-sports.io'

# --- CONSTANTS ---
SLEEP_TIME = 90
FIXTURE_API_INTERVAL = 900 # 900 seconds (15 minutes) interval for fixture result API call
# REMOVED: MINUTES_REGULAR_BET = [35, 36, 37]
MINUTES_32_MINUTE_BET = [31, 32, 33]
MINUTES_80_MINUTE_BET = [79, 80, 81]
# REMOVED: BET_TYPE_REGULAR = 'regular'
BET_TYPE_32_OVER = '32_over' 
BET_TYPE_80_MINUTE = '80_minute'
STATUS_LIVE = ['LIVE', '1H', '2H', 'ET', 'P']
STATUS_HALFTIME = 'HT'
STATUS_FINISHED = ['FT', 'AET', 'PEN']
BET_SCORES_80_MINUTE = ['3-1','2-0']

class FirebaseManager:
    """Manages all interactions with the Firebase Firestore database."""
    def __init__(self, credentials_json_string):
        try:
            logger.info("Initializing Firebase...")
            if not credentials_json_string:
                # This should ideally be handled by the environment variable setup
                logger.warning("FIREBASE_CREDENTIALS_JSON is empty. Skipping Firebase initialization.")
                self.db = None
                return

            cred_dict = json.loads(credentials_json_string)
            cred = credentials.Certificate(cred_dict)
            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred)
            self.db = firestore.client()
            logger.info("Firebase initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Firebase: {e}")
            self.db = None # Set to None on failure
            raise

    # Note: All Firebase methods should check if self.db is not None
    def get_tracked_match(self, match_id):
        if not self.db: return None
        try:
            doc = self.db.collection('tracked_matches').document(str(match_id)).get()
            return doc.to_dict() if doc.exists else None
        except Exception as e:
            logger.error(f"Firestore Error during get_tracked_match: {e}")
            return None

    def update_tracked_match(self, match_id, data):
        if not self.db: return
        try:
            self.db.collection('tracked_matches').document(str(match_id)).set(data, merge=True)
        except Exception as e:
            logger.error(f"Firestore Error during update_tracked_match: {e}")
            
    def delete_tracked_match(self, match_id):
        if not self.db: return
        try:
            self.db.collection('tracked_matches').document(str(match_id)).delete()
        except Exception as e:
            logger.error(f"Firestore Error during delete_tracked_match: {e}")

    def get_unresolved_bets(self):
        if not self.db: return {}
        try:
            bets = self.db.collection('unresolved_bets').stream()
            result = {doc.id: doc.to_dict() for doc in bets}
            return result
        except Exception as e:
            logger.error(f"Firestore Error during get_unresolved_bets: {e}")
            return {}
    
    def get_stale_unresolved_bets(self, minutes_to_wait=20):
        if not self.db: return {}
        """
        Retrieves unresolved bets from Firestore that were placed more than `minutes_to_wait` ago.
        This is primarily used to ensure FT resolution for 80' bets and 32' Over bets.
        """
        try:
            bets = self.db.collection('unresolved_bets').stream()
            stale_bets = {}
            time_threshold = datetime.utcnow() - timedelta(minutes=minutes_to_wait)
            
            for doc in bets:
                bet_info = doc.to_dict()
                # Only process bets requiring FT resolution here
                if bet_info.get('bet_type') in [BET_TYPE_80_MINUTE, BET_TYPE_32_OVER]: 
                    placed_at_str = bet_info.get('placed_at')
                    if placed_at_str:
                        try:
                            placed_at_dt = datetime.strptime(placed_at_str, '%Y-%m-%d %H:%M:%S')
                            if placed_at_dt < time_threshold:
                                stale_bets[doc.id] = bet_info
                        except ValueError:
                            logger.warning(f"Could not parse placed_at timestamp for bet {doc.id}")
                            continue
            return stale_bets
        except Exception as e:
            logger.error(f"Firestore Error during get_stale_unresolved_bets: {e}")
            return {}

    def add_unresolved_bet(self, match_id, data):
        if not self.db: return
        try:
            # Add a timestamp when the bet was placed
            data['placed_at'] = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            self.db.collection('unresolved_bets').document(str(match_id)).set(data)
        except Exception as e:
            logger.error(f"Firestore Error during add_unresolved_bet: {e}")

    def move_to_resolved(self, match_id, bet_info, outcome):
        if not self.db: return False
        try:
            resolved_data = {
                **bet_info,
                'outcome': outcome,
                'resolved_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
                'resolution_timestamp': firestore.SERVER_TIMESTAMP
            } 
            self.db.collection('resolved_bets').document(str(match_id)).set(resolved_data)
            self.db.collection('unresolved_bets').document(str(match_id)).delete()
            return True
        except Exception as e:
            logger.error(f"Firestore Error during move_to_resolved: {e}")
            return False

    def add_to_resolved_bets(self, match_id, bet_info, outcome):
        if not self.db: return False
        try:
            resolved_data = {
                **bet_info,
                'outcome': outcome,
                'resolved_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
                'resolution_timestamp': firestore.SERVER_TIMESTAMP
            }
            # Use a unique ID based on match and timestamp since this is an append operation
            doc_id = f"{match_id}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
            self.db.collection('resolved_bets').document(doc_id).set(resolved_data)
            return True
        except Exception as e:
            logger.error(f"Firestore Error during add_to_resolved_bets: {e}")
            return False

    # Methods to track the last successful resolution API call time
    def get_last_api_call(self):
        """Retrieves the last successful resolution API call time."""
        if not self.db: return None
        try:
            doc = self.db.collection('config').document('api_tracker').get()
            data = doc.to_dict()
            if data and 'last_resolution_api_call' in data:
                return data['last_resolution_api_call']
            return None
        except Exception as e:
            logger.error(f"Firestore Error during get_last_api_call: {e}")
            return None

    def update_last_api_call(self):
        """Updates the last successful resolution API call time to now."""
        if not self.db: return
        try:
            timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            self.db.collection('config').document('api_tracker').set({
                'last_resolution_api_call': timestamp
            }, merge=True)
        except Exception as e:
            logger.error(f"Firestore Error during update_last_api_call: {e}")

# Initialize Firebase
try:
    firebase_manager = FirebaseManager(FIREBASE_CREDENTIALS_JSON_STRING)
except Exception as e:
    logger.critical(f"Critical Firebase initialization error: {e}")
    if not firebase_manager.db:
        logger.warning("Continuing bot execution with disabled Firebase functionality.")

def send_telegram(msg, max_retries=3):
    """Send Telegram message with retry mechanism"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning(f"Telegram credentials missing. Message not sent: {msg}")
        return False
        
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {'chat_id': TELEGRAM_CHAT_ID, 'text': msg}
    
    for attempt in range(max_retries):
        try:
            response = requests.post(url, data=data, timeout=10)
            if response.status_code == 200:
                return True
            else:
                logger.error(f"Telegram error (attempt {attempt + 1}): {response.status_code} - {response.text}")
        except requests.exceptions.RequestException as e:
            logger.error(f"Network Error sending Telegram message (attempt {attempt + 1}): {e}")
        
        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)
    
    return False

def handle_api_rate_limit(response):
    """Handle API rate limiting by adjusting sleep time"""
    if response.status_code == 429:
        retry_after = int(response.headers.get('Retry-After', 60))
        logger.warning(f"Rate limited. Sleeping for {retry_after} seconds")
        time.sleep(retry_after)
        return True
    return False

def get_live_matches():
    """Fetch ONLY live matches from API"""
    if not API_KEY:
        logger.error("API_KEY is not set. Cannot fetch live matches.")
        return []
        
    url = f"{BASE_URL}/fixtures?live=all"
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        if handle_api_rate_limit(response):
            return get_live_matches()
        if response.status_code != 200:
            logger.error(f"API ERROR: {response.status_code} - {response.text}")
            return []
        data = response.json()
        return data.get('response', [])
    except Exception as e:
        logger.error(f"API Error: {e}")
        return []

def get_fixture_by_id(fixture_id):
    """Fetch details for a single fixture by its ID."""
    if not API_KEY: return None
    
    url = f"{BASE_URL}/fixtures"
    params = {'id': fixture_id}
    try:
        response = requests.get(url, headers=HEADERS, params=params, timeout=15)
        if handle_api_rate_limit(response):
            return get_fixture_by_id(fixture_id)
        if response.status_code != 200:
            logger.error(f"API ERROR for fixture {fixture_id}: {response.status_code} - {response.text}")
            return None
        data = response.json()
        return data['response'][0] if data.get('response') else None
    except Exception as e:
        logger.error(f"Error fetching fixture {fixture_id}: {e}")
        return None

# REMOVED: place_regular_bet function

def place_32_over_bet(state, fixture_id, score, match_info):
    """Handles placing the 32' over bet if score is 0-1 or 1-0."""
    
    # Check for qualifying scores: 0-1, or 1-0
    qualifying_scores = ['0-1', '1-0']
    
    if score in qualifying_scores:
        # The bet is always "Over 2.5"
        over_line = 2.5 
        
        # Update the state to indicate a 32' bet has been placed
        state['32_bet_placed'] = True
        firebase_manager.update_tracked_match(fixture_id, state)

        # Prepare and add unresolved bet data to Firebase
        unresolved_data = {
            'match_name': match_info['match_name'],
            'placed_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
            'league': match_info['league_name'],
            'country': match_info['country'],
            'league_id': match_info['league_id'],
            'bet_type': BET_TYPE_32_OVER, 
            '32_score': score,
            'over_line': over_line,
            'fixture_id': fixture_id
        }
        firebase_manager.add_unresolved_bet(fixture_id, unresolved_data)
        
        # Send Telegram notification
        send_telegram(f"⏱️ 32' - {match_info['match_name']}\n🏆 {match_info['league_name']} ({match_info['country']})\n🔢 Score: {score}\n🎯 Bet Placed: Total Goals **Over {over_line}**for Full Time")
    else:
        # Also mark as placed to avoid re-checking on every loop
        state['32_bet_placed'] = True
        firebase_manager.update_tracked_match(fixture_id, state)

# REMOVED: check_ht_result function

def place_80_minute_bet(state, fixture_id, score, match_info):
    """Handles placing the new 80' bet."""
    if score in BET_SCORES_80_MINUTE:
        state['80_bet_placed'] = True
        state['80_score'] = score
        firebase_manager.update_tracked_match(fixture_id, state)
        unresolved_data = {
            'match_name': match_info['match_name'],
            'placed_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
            'league': match_info['league_name'],
            'country': match_info['country'],
            'league_id': match_info['league_id'],
            'bet_type': BET_TYPE_80_MINUTE,
            '80_score': score,
            'fixture_id': fixture_id
        }
        firebase_manager.add_unresolved_bet(fixture_id, unresolved_data)
        send_telegram(f"⏱️ 80' - {match_info['match_name']}\n🏆 {match_info['league_name']} ({match_info['country']})\n🔢 Score: {score}\n🎯 80' Correct Score Bet Placed for Full Time")
    else:
        state['80_bet_placed'] = True
        firebase_manager.update_tracked_match(fixture_id, state)

def process_live_match(match):
    """
    Processes a single live match.
    No API calls are made here.
    """
    fixture = match['fixture']
    teams = match['teams']
    goals = match['goals']
    fixture_id = fixture['id']
    match_name = f"{teams['home']['name']} vs {teams['away']['name']}"
    minute = fixture['status']['elapsed']
    status = fixture['status']['short'] 
    home_goals = goals['home'] if goals['home'] is not None else 0
    away_goals = goals['away'] if goals['away'] is not None else 0
    score = f"{home_goals}-{away_goals}"
    
    if status.upper() not in STATUS_LIVE and status.upper() != STATUS_HALFTIME:
        return
    if minute is None and status.upper() not in [STATUS_HALFTIME]:
        return
    
    state = firebase_manager.get_tracked_match(fixture_id) or {
        # REMOVED: '36_bet_placed': False,
        '32_bet_placed': False, 
        '80_bet_placed': False,
        # REMOVED: '36_score': None,
        '80_score': None,
    }
    
    match_info = {
        'match_name': match_name,
        'league_name': match['league']['name'],
        'country': match['league']['country'],
        'league_id': match['league']['id']
    }
        
    # 1. 32' Over 2.5 Bet - Only checked in the first half
    if status.upper() == '1H' and minute in MINUTES_32_MINUTE_BET and not state.get('32_bet_placed'):
        place_32_over_bet(state, fixture_id, score, match_info) 
        
    # REMOVED: 36' Regular Bet logic
    # REMOVED: Halftime Resolution (for 36' bets) logic
    # Now that 36' is removed, 32' Over bet waits until FT, so HT check is unnecessary.
        
    # 2. 80' Bet - Only checked in the second half
    elif status.upper() == '2H' and minute in MINUTES_80_MINUTE_BET and not state.get('80_bet_placed'):
        place_80_minute_bet(state, fixture_id, score, match_info)
    
    # If the match is finished and there are no unresolved bets, delete the tracked match state
    # This remains for cleanup after FT resolution by check_and_resolve_stale_bets
    if status in STATUS_FINISHED and not firebase_manager.get_unresolved_bets().get(str(fixture_id)):
        firebase_manager.delete_tracked_match(fixture_id)


def check_and_resolve_stale_bets():
    """
    Checks and resolves old, unresolved bets by fetching their final status.
    This function handles 80' bets and 32' Over bets that require FT resolution.
    It now uses a time-gate to control the fixture API call frequency.
    """
    stale_bets = firebase_manager.get_stale_unresolved_bets()
    if not stale_bets:
        return
    
    # --- TIME-GATE LOGIC ---
    last_call_str = firebase_manager.get_last_api_call()
    last_call_dt = None
    if last_call_str:
        try:
            last_call_dt = datetime.strptime(last_call_str, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            logger.warning("Could not parse last_resolution_api_call timestamp. Proceeding with API call.")

    time_since_last_call = (datetime.utcnow() - last_call_dt).total_seconds() if last_call_dt else FIXTURE_API_INTERVAL + 1
    
    if time_since_last_call < FIXTURE_API_INTERVAL:
        logger.info(f"Skipping FT resolution API call. Last call was {int(time_since_last_call)}s ago. Next in {int(FIXTURE_API_INTERVAL - time_since_last_call)}s.")
        return

    logger.info(f"Initiating FT resolution API calls for {len(stale_bets)} stale bets.")
    successful_api_call = False
    
    for match_id, bet_info in stale_bets.items():
        match_data = get_fixture_by_id(match_id)
        
        if not match_data:
            logger.warning(f"Failed to fetch final data for fixture {match_id}. Will retry on next interval.")
            # If API call failed, don't update the last call time.
            continue
        
        # Mark successful API call to update timestamp after the loop
        successful_api_call = True

        status = match_data['fixture']['status']['short']
        
        if status in STATUS_FINISHED:
            final_score = f"{match_data['goals']['home'] or 0}-{match_data['goals']['away'] or 0}"
            match_name = bet_info.get('match_name', f"Match {match_id}")
            bet_type = bet_info.get('bet_type', 'unknown')
            outcome = None
            message = ""

            if bet_type == BET_TYPE_80_MINUTE:
                # Logic for 80' correct score bet
                bet_score = bet_info.get('80_score')
                outcome = 'win' if final_score == bet_score else 'loss'
                message = f"🏁 FINAL RESULT - 80' Bet\n⚽ {match_name}\n🔢 Final Score: **{final_score}**\n🎯 Bet on 80' Score: **{bet_score}**\n📊 Outcome: {'✅ WON' if outcome == 'win' else '❌ LOST'}"

            elif bet_type == BET_TYPE_32_OVER:
                # Logic for 32' Over 2.5 bet
                over_line = bet_info.get('over_line') # Should be 2.5
                try:
                    home_goals, away_goals = map(int, final_score.split('-'))
                    total_goals = home_goals + away_goals
                    
                    if total_goals > over_line:
                        outcome = 'win'
                    elif total_goals < over_line:
                        outcome = 'loss'
                    else: # total_goals == over_line (Push/Void)
                        outcome = 'push'
                        
                    message = f"🏁 FINAL RESULT - 32' Over Bet\n⚽ {match_name}\n🔢 Final Score: **{final_score}**\n🎯 Bet: Over {over_line}\n📊 Outcome: {'✅ WON' if outcome == 'win' else '❌ LOST' if outcome == 'loss' else '➖ PUSH'}"
                except ValueError:
                    outcome = 'error'
                    message = f"⚠️ FINAL RESULT: {match_name}\n❌ Bet could not be resolved due to score format issue."

            if outcome and outcome != 'error':
                if send_telegram(message):
                    # Move to resolved collection and delete from unresolved
                    firebase_manager.move_to_resolved(match_id, bet_info, outcome)
                    # Also delete the match from tracked_matches state as it's finished
                    firebase_manager.delete_tracked_match(match_id) 
                time.sleep(1)
    
    # Update API call time only if at least one fixture was successfully fetched.
    if successful_api_call:
        firebase_manager.update_last_api_call()

def run_bot_once():
    """Run one complete cycle of the bot"""
    logger.info("Starting bot cycle...")
    
    live_matches = get_live_matches()
    for match in live_matches:
        process_live_match(match)
    
    check_and_resolve_stale_bets()
    
    logger.info("Bot cycle completed.")

if __name__ == "__main__":
    logger.info("Starting Football Betting Bot")
    # Initial startup message
    send_telegram("🚀 Football Betting Bot Started Successfully! Monitoring live games.")
    
    while True:
        try:
            run_bot_once()
        except Exception as e:
            error_msg = f"❌ CRITICAL ERROR: {str(e)}"
            logger.critical(error_msg, exc_info=True)
            send_telegram(error_msg[:300])
        finally:
            time.sleep(SLEEP_TIME)
