import os
import time
import requests
import logging
from datetime import datetime, timedelta
import firebase_admin
from firebase_admin import credentials, firestore

# =====================
# CONFIG
# =====================
API_KEY = os.getenv("API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FIREBASE_CREDENTIALS_JSON_STRING = os.getenv("FIREBASE_CREDENTIALS")

BASE_URL = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}

# =====================
# LOGGING
# =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("BetBot")

# =====================
# FIREBASE
# =====================
cred = credentials.Certificate(eval(FIREBASE_CREDENTIALS_JSON_STRING))
firebase_admin.initialize_app(cred)
db = firestore.client()

# =====================
# TELEGRAM
# =====================
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Telegram error: {e}")

# =====================
# FETCH LIVE MATCHES
# =====================
def get_live_matches():
    try:
        url = f"{BASE_URL}/fixtures?live=all"
        response = requests.get(url, headers=HEADERS, timeout=15)
        data = response.json()
        return data.get("response", [])
    except Exception as e:
        logger.error(f"Error fetching live matches: {e}")
        return []

# =====================
# 80' TRIGGER
# =====================
def check_80_minute_draws():
    matches = get_live_matches()
    for match in matches:
        try:
            fixture = match["fixture"]
            league = match["league"]
            teams = match["teams"]
            goals = match["goals"]

            elapsed = fixture["status"]["elapsed"]
            if not elapsed or not (79 <= elapsed <= 81):
                continue

            score = f"{goals['home']}-{goals['away']}"
            if score not in ["0-0", "1-1", "2-2"]:
                continue

            fixture_id = str(fixture["id"])
            home_team = teams["home"]["name"]
            away_team = teams["away"]["name"]
            match_name = f"{home_team} vs {away_team}"

            # Check if already logged
            doc_ref = db.collection("unresolved_bets").document(fixture_id)
            if doc_ref.get().exists:
                continue

            bet_data = {
                "match_name": match_name,
                "league": league["name"],
                "country": league["country"],
                "score": score,
                "alerted_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            }
            doc_ref.set(bet_data)

            msg = f"⚽ 80' Alert!\n{match_name}\nScore: {score}\nLeague: {league['name']}"
            send_telegram(msg)
            logger.info(f"Logged 80' bet: {bet_data}")

        except Exception as e:
            logger.error(f"Error processing match: {e}")

# =====================
# RESOLVE BETS
# =====================
def resolve_finished_matches():
    unresolved_ref = db.collection("unresolved_bets").stream()
    unresolved_bets = {doc.id: doc.to_dict() for doc in unresolved_ref}
    if not unresolved_bets:
        return

    matches = get_live_matches()
    for match in matches:
        fixture = match["fixture"]
        status = fixture["status"]["short"]

        if status not in ["FT", "AET", "PEN"]:
            continue

        fixture_id = str(fixture["id"])
        if fixture_id not in unresolved_bets:
            continue

        bet = unresolved_bets[fixture_id]
        home_goals = match["goals"]["home"] or 0
        away_goals = match["goals"]["away"] or 0
        ft_score = f"{home_goals}-{away_goals}"

        outcome = "WIN" if ft_score == bet["score"] else "LOSS"

        resolved_data = {
            "match_name": bet["match_name"],
            "league": bet["league"],
            "country": bet["country"],
            "score_80": bet["score"],
            "score_ft": ft_score,
            "outcome": outcome,
            "triggered_at": bet["alerted_at"],
            "resolved_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        }

        db.collection("resolved_bets").document(fixture_id).set(resolved_data)
        db.collection("unresolved_bets").document(fixture_id).delete()

        msg = (
            f"✅ RESOLVED\n{bet['match_name']}\n🏆 {bet['league']} ({bet['country']})\n"
            f"80' Score: {bet['score']}\nFT Score: {ft_score}\nResult: {outcome}"
        )
        send_telegram(msg)
        logger.info(f"Resolved bet {fixture_id}: {outcome}")

# =====================
# DAILY SUMMARY
# =====================
def send_daily_summary():
    today = datetime.utcnow().date()
    tomorrow = today + timedelta(days=1)

    bets_ref = (
        db.collection("resolved_bets")
        .where("resolved_at", ">=", f"{today} 00:00:00")
        .where("resolved_at", "<", f"{tomorrow} 00:00:00")
        .stream()
    )

    bets = [doc.to_dict() for doc in bets_ref]
    if not bets:
        return

    total = len(bets)
    wins = sum(1 for b in bets if b["outcome"] == "WIN")
    losses = total - wins
    win_rate = (wins / total) * 100 if total > 0 else 0

    msg = (
        f"📊 Daily Summary ({today} UTC)\n"
        f"Total Bets: {total}\n"
        f"✅ Wins: {wins}\n"
        f"❌ Losses: {losses}\n"
        f"📈 Win Rate: {win_rate:.2f}%"
    )
    send_telegram(msg)

# =====================
# MAIN LOOP
# =====================
if __name__ == "__main__":
    logger.info("Starting 80' Bet Bot...")
    last_summary = None

    while True:
        check_80_minute_draws()
        resolve_finished_matches()

        # Daily summary at 00:05 UTC
        now = datetime.utcnow()
        if now.hour == 0 and now.minute < 10:
            if not last_summary or last_summary.date() != now.date():
                send_daily_summary()
                last_summary = now

        time.sleep(60)
