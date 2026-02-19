from flask import Flask, render_template, request, redirect, url_for, session, flash
import json
import os
from dotenv import load_dotenv

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from urllib.parse import quote_plus
import secrets
from datetime import datetime, timedelta, timezone
import time
import requests

# Load environment variables
load_dotenv()
print("STRAVA CLIENT ID:", os.getenv("STRAVA_CLIENT_ID"))

# Firebase init
cred = credentials.Certificate("firebase/tribeplan-service-account.json")
firebase_admin.initialize_app(cred)

# Firestore client
db = firestore.client()

# Flask app gotta be created before using @app.template_filter
app = Flask(__name__)

# Flask session secret
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-change-this")

# Firebase Web API key for Auth REST calls
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")

# External APIs
STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
STRAVA_REDIRECT_URI = os.getenv("STRAVA_REDIRECT_URI")

# Nutrition API - USDA FoodData Central (free at fdc.nal.usda.gov/api-key-signup)
USDA_FDC_API_KEY = os.getenv("USDA_FDC_API_KEY")

# OpenWeatherMap API for weather-based activity suggestions
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

# Google Maps JavaScript API (for buddies map)
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

# Activity level multipliers for TDEE (BMR * multiplier)
ACTIVITY_MULTIPLIERS = {
    "sedentary": 1.2,    # little/no exercise
    "light": 1.375,      # 1-3 days/week
    "moderate": 1.55,    # 3-5 days/week
    "active": 1.725,     # 6-7 days/week
    "very_active": 1.9,  # heavy exercise
}

# Print API config status on startup (for debugging)
print("=" * 50)
print("API Configuration Status:")
print(f"  STRAVA_CLIENT_ID: {'SET' if STRAVA_CLIENT_ID else 'MISSING'}")
print(f"  STRAVA_CLIENT_SECRET: {'SET' if STRAVA_CLIENT_SECRET else 'MISSING'}")
print(f"  OPENWEATHER_API_KEY: {'SET' if OPENWEATHER_API_KEY else 'MISSING'}")
print(f"  USDA_FDC_API_KEY: {'SET' if USDA_FDC_API_KEY else 'MISSING'} (nutrition)")
print(f"  GOOGLE_MAPS_API_KEY: {'SET' if GOOGLE_MAPS_API_KEY else 'MISSING'} (buddies map)")
print("=" * 50)


def col(user_id: str, name: str):
    """users/{uid}/{name} collection ref"""
    return db.collection("users").document(user_id).collection(name)


def _strava_token_ref(user_id: str):
    """Strava token doc ref: users/{uid}/integrations/strava (standardised path)."""
    return db.collection("users").document(user_id).collection("integrations").document("strava")


def _strava_token_ref_legacy(user_id: str):
    """Legacy path: users/{uid}/strava_tokens/token."""
    return db.collection("users").document(user_id).collection("strava_tokens").document("token")


# ===================== VALIDATION HELPERS =====================

def validate_mood(val) -> tuple:
    """Validate mood 1–5. Returns (value, error_msg)."""
    try:
        v = int(val)
        if 1 <= v <= 5:
            return v, None
        return None, "Mood must be between 1 and 5."
    except (TypeError, ValueError):
        return None, "Mood must be a number between 1 and 5."


def validate_calories(val) -> tuple:
    """Validate calories >= 0. Returns (value, error_msg)."""
    try:
        v = int(val)
        if v >= 0:
            return v, None
        return None, "Calories must be 0 or greater."
    except (TypeError, ValueError):
        return None, "Calories must be a whole number."


def validate_duration(val) -> tuple:
    """Validate fitness duration >= 0. Returns (value, error_msg)."""
    try:
        v = int(val)
        if v >= 0:
            return v, None
        return None, "Duration must be 0 or greater."
    except (TypeError, ValueError):
        return None, "Duration must be a whole number."


def validate_required(val, field_name: str) -> tuple:
    """Validate required string field. Returns (value, error_msg)."""
    s = (val or "").strip()
    if s:
        return s, None
    return None, f"{field_name} is required."


# Jinja filter (now safe, app exists)
@app.template_filter()
def fmt_dt(dt):
    try:
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return str(dt)

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        resp = requests.post(
            f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={FIREBASE_API_KEY}",
            json={
                "email": email,
                "password": password,
                "returnSecureToken": True
            }
        )
        data = resp.json()
        if resp.status_code != 200:
            error_message = data.get("error", {}).get("message", "Registration failed")
            return render_template("register.html", error=error_message)

        uid = data["localId"]
        session["user_id"] = uid
        session["user_email"] = email

        # Create user document with email and created_at
        try:
            db.collection("users").document(uid).set(
                {
                    "email": email,
                    "created_at": datetime.now()
                },
                merge=True
            )
        except Exception as e:
            print("Failed to write user doc:", e)


        return redirect(url_for("dashboard"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        resp = requests.post(
            f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_API_KEY}",
            json={
                "email": email,
                "password": password,
                "returnSecureToken": True
            }
        )
        data = resp.json()
        if resp.status_code != 200:
            error_message = data.get("error", {}).get("message", "Login failed")
            return render_template("login.html", error=error_message)

        uid = data["localId"]
        session["user_id"] = uid
        session["user_email"] = email

        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def get_current_user_id():
    return session.get("user_id")


def require_login():
    uid = get_current_user_id()
    if not uid:
        return None, redirect(url_for("login"))
    return uid, None

@app.route("/connect_strava")
def connect_strava():
    client_id = STRAVA_CLIENT_ID
    # Build http://localhost:5000/strava/callback automatically
    redirect_uri = url_for("strava_callback", _external=True)
    scope = "read,activity:read_all"
    state = secrets.token_urlsafe(16)

    encoded_redirect = quote_plus(redirect_uri)
    auth_url = (
        f"https://www.strava.com/oauth/authorize"
        f"?client_id={client_id}"
        f"&redirect_uri={encoded_redirect}"
        f"&response_type=code"
        f"&scope={quote_plus(scope)}"
        f"&approval_prompt=auto"
        f"&state={state}"
    )
    print("\n[STRAVA AUTH URL]", auth_url, "\n")  # <-- look for this in your console
    return redirect(auth_url)

def is_strava_connected():
    uid = get_current_user_id()
    if not uid:
        return False
    doc = _strava_token_ref(uid).get()
    if doc.exists:
        return True
    return _strava_token_ref_legacy(uid).get().exists


def get_strava_data_for_user(user_id: str):
    """Get Strava token doc (checks integrations/strava first, then legacy path). Migrates legacy to new path."""
    snap = _strava_token_ref(user_id).get()
    if snap.exists:
        return snap.to_dict()
    snap = _strava_token_ref_legacy(user_id).get()
    if snap.exists:
        data = snap.to_dict()
        # Migrate to standardised path
        _strava_token_ref(user_id).set(data)
        _strava_token_ref_legacy(user_id).delete()
        return data
    return None


def get_strava_access_token_for_user(user_id: str):
    """
    Return a valid Strava access token for the given user.
    Refreshes the token if it is expired, updating Firestore.
    Uses standardised path: users/{uid}/integrations/strava
    """
    data = get_strava_data_for_user(user_id)
    if not data:
        return None

    token_ref = _strava_token_ref(user_id)
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    expires_at = data.get("expires_at")  # epoch seconds from Strava

    # If we don't have expiry info, just return current token
    if not expires_at or not refresh_token:
        return access_token

    # If token is still valid, return it
    if time.time() < float(expires_at) - 60:
        return access_token

    # Otherwise refresh
    resp = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )
    if resp.status_code != 200:
        print("Strava token refresh failed:", resp.text)
        return access_token  # fall back to old token; request may still work

    new_data = resp.json()
    new_access = new_data.get("access_token")
    new_refresh = new_data.get("refresh_token", refresh_token)
    new_expires = new_data.get("expires_at", expires_at)

    athlete = data.get("athlete") or data.get("strava_athlete")
    token_ref.set(
        {
            "access_token": new_access,
            "refresh_token": new_refresh,
            "expires_at": new_expires,
            "athlete": athlete,
        },
        merge=True,
    )

    return new_access


@app.route("/strava/callback")
def strava_callback():
    # Check for error from Strava (user denied access)
    error = request.args.get("error")
    if error:
        return f"""
        <h1>Strava Connection Cancelled</h1>
        <p>Error: {error}</p>
        <a href="/fitness">Return to Fitness Page</a>
        """, 400
    
    code = request.args.get("code")
    if not code:
        return "No code provided from Strava.", 400

    # Build the redirect URI exactly as used in authorize
    redirect_uri = url_for("strava_callback", _external=True)
    
    print(f"[STRAVA DEBUG] Exchanging code for token...")
    print(f"[STRAVA DEBUG] client_id: {STRAVA_CLIENT_ID}")
    print(f"[STRAVA DEBUG] redirect_uri: {redirect_uri}")

    response = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code"
        }
    )

    if response.status_code != 200:
        error_data = response.json() if response.headers.get('content-type', '').startswith('application/json') else {}
        error_msg = error_data.get("message", "Unknown error")
        
        # Provide helpful debugging info
        return f"""
        <h1>Strava Connection Failed</h1>
        <p><strong>Error:</strong> {error_msg}</p>
        <p><strong>Details:</strong> {response.text}</p>
        <hr>
        <h3>Troubleshooting Steps:</h3>
        <ol>
            <li>Go to <a href="https://www.strava.com/settings/api" target="_blank">Strava API Settings</a></li>
            <li>Check that <strong>Authorization Callback Domain</strong> is set to: <code>127.0.0.1</code> (or <code>localhost</code>)</li>
            <li>Verify your <strong>Client Secret</strong> in your .env file matches Strava exactly</li>
            <li>Make sure your Client ID is: <code>{STRAVA_CLIENT_ID}</code></li>
        </ol>
        <a href="/fitness">Return to Fitness Page</a>
        """, 400

    token_data = response.json()
    access_token = token_data.get("access_token")
    athlete = token_data.get("athlete", {})

    user_id = get_current_user_id()
    if not user_id:
        return "Login required before connecting Strava", 401

    # Save to standardised path: users/{uid}/integrations/strava
    _strava_token_ref(user_id).set({
        "access_token": access_token,
        "refresh_token": token_data.get("refresh_token"),
        "expires_at": token_data.get("expires_at"),
        "athlete": athlete
    })
    
    # Also save athlete info to user profile for easy access
    db.collection("users").document(user_id).set({
        "strava_athlete": athlete
    }, merge=True)

    return render_template("strava_connected.html", athlete=athlete)


@app.route("/integrations")
def integrations():
    """Integrations settings page: Strava status, athlete info, sync/disconnect."""
    uid, redirect_resp = require_login()
    if redirect_resp:
        return redirect_resp

    strava_connected = is_strava_connected()
    strava_data = None
    if strava_connected:
        strava_data = get_strava_data_for_user(uid)
        if strava_data:
            # Merge with user-level strava_athlete if stored there
            user_doc = db.collection("users").document(uid).get()
            if user_doc.exists:
                ud = user_doc.to_dict() or {}
                if ud.get("strava_athlete") and not strava_data.get("athlete"):
                    strava_data = {**strava_data, "athlete": ud["strava_athlete"]}

    athlete = (strava_data or {}).get("athlete") or {}
    last_sync = strava_data.get("last_sync_at") if strava_data else None
    if hasattr(last_sync, "strftime"):
        last_sync_str = last_sync.strftime("%Y-%m-%d %H:%M")
    elif last_sync:
        last_sync_str = str(last_sync)[:16]
    else:
        last_sync_str = None

    return render_template(
        "integrations.html",
        strava_connected=strava_connected,
        athlete=athlete,
        last_sync_str=last_sync_str,
    )


@app.route("/integrations/strava/disconnect", methods=["POST"])
def strava_disconnect():
    """Disconnect Strava: delete token, keep imported workouts."""
    uid, redirect_resp = require_login()
    if redirect_resp:
        return redirect_resp

    _strava_token_ref(uid).delete()
    _strava_token_ref_legacy(uid).delete()
    flash("Strava has been disconnected. Your imported workouts are kept.", "success")
    return redirect(url_for("integrations"))


@app.route('/')
def root():
    """Redirect root to login page"""
    return redirect(url_for('login'))


@app.route('/home', methods=['GET', 'POST'], endpoint='home')
def home_view():
    uid = get_current_user_id()
    is_logged_in = uid is not None
    
    # Default stats for non-logged-in users
    stats = {
        "mood_avg": None,
        "mood_count": 0,
        "fitness_count": 0,
        "fitness_mins": 0,
        "food_count": 0,
        "total_calories": 0,
        "recent_mood": None,
        "recent_workout": None,
        "strava_connected": False
    }
    
    profile = {}
    
    if is_logged_in:
        # Get user profile
        user_doc = db.collection("users").document(uid).get()
        if user_doc.exists:
            profile = user_doc.to_dict()
        
        # Calculate mood stats (last 7 days)
        week_ago = datetime.now() - timedelta(days=7)
        
        mood_docs = list(col(uid, "mood_logs").order_by("date", direction=firestore.Query.DESCENDING).limit(20).stream())
        if mood_docs:
            mood_values = []
            for d in mood_docs:
                entry = d.to_dict()
                mood_val = entry.get("mood")
                if mood_val is not None:
                    mood_values.append(int(mood_val))
            
            if mood_values:
                stats["mood_avg"] = round(sum(mood_values) / len(mood_values), 1)
                stats["mood_count"] = len(mood_values)
                stats["recent_mood"] = mood_docs[0].to_dict() if mood_docs else None
        
        # Calculate fitness stats
        fitness_docs = list(col(uid, "fitness_logs").order_by("date", direction=firestore.Query.DESCENDING).limit(20).stream())
        if fitness_docs:
            total_mins = 0
            for d in fitness_docs:
                entry = d.to_dict()
                duration = entry.get("duration")
                if duration:
                    try:
                        total_mins += int(duration)
                    except (ValueError, TypeError):
                        pass
            
            stats["fitness_count"] = len(fitness_docs)
            stats["fitness_mins"] = total_mins
            recent = fitness_docs[0].to_dict() if fitness_docs else None
            if recent:
                stats["recent_workout"] = recent.get("exercise", "Workout")
        
        # Calculate food stats
        food_docs = list(col(uid, "food_logs").order_by("date", direction=firestore.Query.DESCENDING).limit(20).stream())
        if food_docs:
            total_cals = 0
            for d in food_docs:
                entry = d.to_dict()
                cals = entry.get("calories")
                if cals:
                    try:
                        total_cals += int(cals)
                    except (ValueError, TypeError):
                        pass
            
            stats["food_count"] = len(food_docs)
            stats["total_calories"] = total_cals
        
        # Check Strava connection
        stats["strava_connected"] = is_strava_connected()
    
    return render_template("home.html", 
                         is_logged_in=is_logged_in, 
                         stats=stats,
                         profile=profile)


# MOOD CRUD
# ===================== MOOD CRUD =====================

@app.route('/mood', methods=['GET', 'POST'])
def mood():
    uid, redirect_resp = require_login()
    if redirect_resp:
        return redirect_resp

    logs = []

    if request.method == "POST":
        mood_val, err = validate_mood(request.form.get("mood"))
        if err:
            flash(err, "error")
            return redirect(url_for("mood"))
        journal = request.form.get("journal", "").strip()
        now = datetime.now()

        log_entry = {
            "date": now,
            "mood": mood_val,
            "journal": journal,
        }

        col(uid, "mood_logs").add(log_entry)
        flash("Mood logged successfully.", "success")
        return redirect(url_for("mood"))

    docs = col(uid, "mood_logs").order_by("date", direction=firestore.Query.DESCENDING).stream()
    for d in docs:
        entry = d.to_dict()
        entry["id"] = d.id
        if isinstance(entry.get("date"), datetime):
            entry["date"] = entry["date"].strftime("%Y-%m-%d %H:%M:%S")
        logs.append(entry)

    return render_template("mood.html", logs=logs)


@app.route('/mood/delete/<log_id>', methods=['POST'])
def mood_delete(log_id):
    uid, redirect_resp = require_login()
    if redirect_resp:
        return redirect_resp

    try:
        (
            db.collection("users")
              .document(uid)
              .collection("mood_logs")
              .document(log_id)
              .delete()
        )
        flash("Mood entry deleted.", "success")
    except Exception as e:
        flash("Delete failed.", "error")
    return redirect(url_for('mood'))


@app.route('/mood/edit/<log_id>', methods=['GET', 'POST'])
def mood_edit(log_id):
    uid, redirect_resp = require_login()
    if redirect_resp:
        return redirect_resp

    ref = (
        db.collection("users")
          .document(uid)
          .collection("mood_logs")
          .document(log_id)
    )

    if request.method == 'POST':
        mood_val, err = validate_mood(request.form.get('mood'))
        if err:
            flash(err, "error")
            return redirect(url_for('mood_edit', log_id=log_id))
        journal = request.form.get('journal', '').strip()
        try:
            ref.update({"mood": mood_val, "journal": journal})
            flash("Mood entry updated.", "success")
        except Exception as e:
            flash("Update failed: " + str(e), "error")
        return redirect(url_for('mood'))

    snap = ref.get()
    if not snap.exists:
        return "Mood log not found", 404

    data = snap.to_dict()
    data["id"] = log_id
    if isinstance(data.get("date"), datetime):
        data["date"] = data["date"].strftime("%Y-%m-%d %H:%M:%S")

    return render_template("mood_edit.html", log=data)


def _food_page_data(uid):
    """Load food logs, targets, and today's totals for food page."""
    logs = []
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_cal, today_p, today_c, today_f = 0, 0, 0, 0

    docs = col(uid, "food_logs").order_by("date", direction=firestore.Query.DESCENDING).stream()
    for d in docs:
        entry = d.to_dict()
        entry["id"] = d.id
        dt = entry.get("date")
        if isinstance(dt, datetime):
            entry["date"] = dt.strftime("%Y-%m-%d %H:%M:%S")
            ds = dt.strftime("%Y-%m-%d")
        else:
            entry["date"] = str(dt)[:19] if dt else ""
            ds = str(dt)[:10] if dt else ""
        logs.append(entry)
        if ds == today_str:
            try:
                today_cal += int(entry.get("calories", 0) or 0)
            except (ValueError, TypeError):
                pass
            try:
                today_p += float(entry.get("protein_g", 0) or 0)
            except (ValueError, TypeError):
                pass
            try:
                today_c += float(entry.get("carbs_g", 0) or 0)
            except (ValueError, TypeError):
                pass
            try:
                today_f += float(entry.get("fat_g", 0) or 0)
            except (ValueError, TypeError):
                pass

    targets = None
    today_water = 0
    water_target = 2000
    user_doc = db.collection("users").document(uid).get()
    if user_doc.exists:
        profile = user_doc.to_dict()
        targets = calculate_nutrition_goals(profile)
        water_target = profile.get("water_target_ml") or 2000

    # Sum today's water intake
    water_docs = col(uid, "water_logs").where(filter=FieldFilter("date", "==", today_str)).stream()
    for wd in water_docs:
        today_water += int(wd.to_dict().get("amount_ml", 0) or 0)

    return logs, targets, {
        "calories": today_cal,
        "protein_g": round(today_p, 1),
        "carbs_g": round(today_c, 1),
        "fat_g": round(today_f, 1),
    }, today_water, water_target


# FOOD CRUD
@app.route('/food', methods=['GET', 'POST'])
def food():
    uid, redirect_resp = require_login()
    if redirect_resp:
        return redirect_resp

    if request.method == 'POST':
        meal_type, err = validate_required(request.form.get('meal_type'), "Meal type")
        if err:
            flash(err, "error")
            return redirect(url_for('food'))
        food_items, err = validate_required(request.form.get('food_items'), "Food items")
        if err:
            flash(err, "error")
            return redirect(url_for('food'))
        calories, err = validate_calories(request.form.get('calories', '0'))
        if err:
            flash(err, "error")
            return redirect(url_for('food'))
        protein = request.form.get('protein_g', '')
        carbs = request.form.get('carbs_g', '')
        fat = request.form.get('fat_g', '')
        now = datetime.now()

        log_entry = {
            "date": now,
            "meal_type": meal_type,
            "food_items": food_items,
            "calories": str(calories),
        }
        if protein:
            try:
                log_entry["protein_g"] = max(0, float(protein))
            except (ValueError, TypeError):
                pass
        if carbs:
            try:
                log_entry["carbs_g"] = max(0, float(carbs))
            except (ValueError, TypeError):
                pass
        if fat:
            try:
                log_entry["fat_g"] = max(0, float(fat))
            except (ValueError, TypeError):
                pass

        col(uid, "food_logs").add(log_entry)
        flash("Meal logged successfully.", "success")
        return redirect(url_for('food'))

    logs, targets, today_totals, today_water, water_target = _food_page_data(uid)
    return render_template(
        "food.html",
        logs=logs,
        targets=targets,
        today_totals=today_totals,
        today_water=today_water,
        water_target=water_target,
        estimated_calories=None,
        estimated_protein=None,
        estimated_carbs=None,
        estimated_fat=None,
        nutrition_error=None,
        nutrition_query="",
    )


# USDA nutrient IDs
_USDA_CAL = 1008
_USDA_PROTEIN = 1003
_USDA_CARBS = 1005
_USDA_FAT = 1004


def _get_nutrient(food_nutrients, nutrient_id):
    for n in food_nutrients or []:
        if n.get("nutrientId") == nutrient_id:
            try:
                return float(n.get("value", 0) or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def search_usda_food(query: str):
    """
    Search USDA FoodData Central. Returns (calories, protein_g, carbs_g, fat_g, error_msg).
    Get free API key at https://fdc.nal.usda.gov/api-key-signup.html
    """
    if not USDA_FDC_API_KEY:
        return None, None, None, None, "Add USDA_FDC_API_KEY to .env (free at fdc.nal.usda.gov/api-key-signup)"

    url = "https://api.nal.usda.gov/fdc/v1/foods/search"
    params = {"api_key": USDA_FDC_API_KEY}
    payload = {"query": query, "pageSize": 5}
    try:
        resp = requests.post(url, params=params, json=payload, timeout=10)
    except Exception as e:
        return None, None, None, None, str(e)
    if resp.status_code != 200:
        return None, None, None, None, resp.text or "API error"
    try:
        data = resp.json()
        foods = data.get("foods", [])
        if not foods:
            return None, None, None, None, "No results found. Try different keywords (e.g. 'bread', 'chicken breast')."
        food = foods[0]
        ntr = food.get("foodNutrients", [])
        cal = _get_nutrient(ntr, _USDA_CAL)
        p = _get_nutrient(ntr, _USDA_PROTEIN)
        c = _get_nutrient(ntr, _USDA_CARBS)
        f = _get_nutrient(ntr, _USDA_FAT)
        if cal <= 0 and (p or c or f):
            cal = (p * 4) + (c * 4) + (f * 9)
        return int(cal), round(p, 1), round(c, 1), round(f, 1), None
    except Exception as e:
        return None, None, None, None, str(e)


def calculate_nutrition_goals(profile: dict):
    """
    Calculate daily calorie and macro targets based on profile.
    Returns dict with daily_calories, protein_g, carbs_g, fat_g, goal_summary.
    """
    h = profile.get("height") or 0
    w = profile.get("weight") or 0
    age = profile.get("age") or 30
    gender = (profile.get("gender") or "male").lower()
    target_weight = profile.get("target_weight")
    goal_type = (profile.get("goal_type") or "maintain").lower()
    target_weeks = profile.get("target_weeks") or 12
    activity = profile.get("activity_level") or "moderate"

    if not h or not w or w <= 0:
        return None

    try:
        h, w, age = float(h), float(w), int(age)
    except (TypeError, ValueError):
        return None

    # BMR (Mifflin-St Jeor)
    if gender == "female":
        bmr = 10 * w + 6.25 * h - 5 * age - 161
    else:
        bmr = 10 * w + 6.25 * h - 5 * age + 5

    mult = ACTIVITY_MULTIPLIERS.get(activity, 1.55)
    tdee = bmr * mult
    daily_cal = tdee

    if goal_type == "lose" and target_weight and target_weeks > 0:
        try:
            tw = float(target_weight)
            kg_to_lose = w - tw
            if kg_to_lose > 0:
                deficit_per_day = (kg_to_lose * 7700) / (target_weeks * 7)
                daily_cal = max(1200, tdee - deficit_per_day)
        except (TypeError, ValueError):
            pass
    elif goal_type == "gain" and target_weight and target_weeks > 0:
        try:
            tw = float(target_weight)
            kg_to_gain = tw - w
            if kg_to_gain > 0:
                surplus_per_day = (kg_to_gain * 7700) / (target_weeks * 7)
                daily_cal = min(4000, tdee + min(surplus_per_day, 500))
        except (TypeError, ValueError):
            pass

    # Macros: protein 1.6g/kg (higher for muscle), rest split 30% fat / 70% carbs
    protein_g = round(w * 1.6)
    protein_cal = protein_g * 4
    remaining = max(0, daily_cal - protein_cal)
    fat_cal = remaining * 0.30
    carb_cal = remaining * 0.70
    fat_g = round(fat_cal / 9)
    carbs_g = round(carb_cal / 4)

    summary = f"TDEE ~{int(tdee)} kcal"
    weekly_rate_kg = 0.0
    projected_date = None
    if goal_type == "lose" and target_weight and target_weeks > 0:
        try:
            tw = float(target_weight)
            kg_to_lose = w - tw
            if kg_to_lose > 0:
                weekly_rate_kg = -(kg_to_lose / target_weeks)
                summary += f" → Deficit for ~{target_weeks} weeks to {target_weight} kg"
                projected_date = datetime.now() + timedelta(weeks=target_weeks)
        except (TypeError, ValueError):
            pass
    elif goal_type == "gain" and target_weight and target_weeks > 0:
        try:
            tw = float(target_weight)
            kg_to_gain = tw - w
            if kg_to_gain > 0:
                weekly_rate_kg = kg_to_gain / target_weeks
                summary += f" → Surplus for ~{target_weeks} weeks to {target_weight} kg"
                projected_date = datetime.now() + timedelta(weeks=target_weeks)
        except (TypeError, ValueError):
            pass

    return {
        "daily_calories": int(daily_cal),
        "protein_g": protein_g,
        "carbs_g": carbs_g,
        "fat_g": fat_g,
        "tdee": int(tdee),
        "bmr": int(bmr),
        "goal_summary": summary,
        "weekly_rate_kg": round(weekly_rate_kg, 2) if weekly_rate_kg else 0,
        "projected_date": projected_date,
        "current_weight": w,
        "target_weight": float(target_weight) if target_weight else None,
        "goal_type": goal_type,
        "target_weeks": target_weeks,
    }


@app.route("/food/nutrition", methods=["POST"])
@app.route("/food/nutritionix", methods=["POST"])
def food_nutrition():
    """
    Use USDA FoodData Central to estimate calories and macros for a meal.
    """
    uid, redirect_resp = require_login()
    if redirect_resp:
        return redirect_resp

    query = request.form.get("nutrition_query", "").strip()
    estimated_calories = None
    estimated_protein = None
    estimated_carbs = None
    estimated_fat = None
    error_msg = None

    if query:
        cal, p, c, f, err = search_usda_food(query)
        if err:
            error_msg = err
        else:
            estimated_calories = cal
            estimated_protein = p
            estimated_carbs = c
            estimated_fat = f

    # Load logs and targets (reuse food page logic)
    logs, targets, today_totals, today_water, water_target = _food_page_data(uid)
    return render_template(
        "food.html",
        logs=logs,
        targets=targets,
        today_totals=today_totals,
        today_water=today_water,
        water_target=water_target,
        estimated_calories=estimated_calories,
        estimated_protein=estimated_protein,
        estimated_carbs=estimated_carbs,
        estimated_fat=estimated_fat,
        nutrition_error=error_msg,
        nutrition_query=query,
    )


@app.route('/food/water', methods=['POST'])
def food_water():
    """Log water intake."""
    uid, redirect_resp = require_login()
    if redirect_resp:
        return redirect_resp
    amount = request.form.get("amount_ml", "250")
    try:
        amount_ml = max(50, min(2000, int(amount)))
    except (TypeError, ValueError):
        amount_ml = 250
    today_str = datetime.now().strftime("%Y-%m-%d")
    col(uid, "water_logs").add({"date": today_str, "amount_ml": amount_ml})
    flash("Water logged.", "success")
    return redirect(url_for('food'))


@app.route('/food/edit/<log_id>', methods=['GET', 'POST'])
def food_edit(log_id):
    uid, redirect_resp = require_login()
    if redirect_resp:
        return redirect_resp

    ref = col(uid, "food_logs").document(log_id)

    if request.method == 'POST':
        meal_type, err = validate_required(request.form.get('meal_type'), "Meal type")
        if err:
            flash(err, "error")
            return redirect(url_for('food_edit', log_id=log_id))
        food_items, err = validate_required(request.form.get('food_items'), "Food items")
        if err:
            flash(err, "error")
            return redirect(url_for('food_edit', log_id=log_id))
        calories, err = validate_calories(request.form.get('calories', '0'))
        if err:
            flash(err, "error")
            return redirect(url_for('food_edit', log_id=log_id))
        update_data = {
            "meal_type": meal_type,
            "food_items": food_items,
            "calories": str(calories),
        }
        for k in ["protein_g", "carbs_g", "fat_g"]:
            v = request.form.get(k, '')
            if v:
                try:
                    update_data[k] = max(0, float(v))
                except ValueError:
                    pass
        ref.update(update_data)
        flash("Meal entry updated.", "success")
        return redirect(url_for('food'))

    snap = ref.get()
    if not snap.exists:
        return "Food log not found", 404

    data = snap.to_dict()
    data["id"] = log_id
    if isinstance(data.get("date"), datetime):
        data["date"] = data["date"].strftime("%Y-%m-%d %H:%M:%S")
    return render_template("food_edit.html", log=data)


@app.route('/food/delete/<log_id>', methods=['POST'])
def food_delete(log_id):
    uid, redirect_resp = require_login()
    if redirect_resp:
        return redirect_resp

    col(uid, "food_logs").document(log_id).delete()
    flash("Meal entry deleted.", "success")
    return redirect(url_for('food'))

# FITNESS CRUD
@app.route('/fitness', methods=['GET', 'POST'])
def fitness():
    uid, redirect_resp = require_login()
    if redirect_resp:
        return redirect_resp

    logs = []

    strava_connected = is_strava_connected()

    client_id = STRAVA_CLIENT_ID
    redirect_uri = STRAVA_REDIRECT_URI
    scope = "activity:read_all"

    auth_url = (
        f"https://www.strava.com/oauth/authorize?client_id={client_id}"
        f"&redirect_uri={redirect_uri}&response_type=code&scope={scope}"
    )

    if request.method == 'POST':
        exercise, err = validate_required(request.form.get('exercise'), "Exercise")
        if err:
            flash(err, "error")
            return redirect(url_for('fitness'))
        duration, err = validate_duration(request.form.get('duration', '0'))
        if err:
            flash(err, "error")
            return redirect(url_for('fitness'))
        notes = request.form.get('notes', '').strip()
        now = datetime.now()

        log_entry = {
            "date": now,
            "exercise": exercise,
            "duration": duration,
            "notes": notes
        }

        col(uid, "fitness_logs").add(log_entry)
        flash("Workout logged successfully.", "success")
        return redirect(url_for('fitness'))

    docs = col(uid, "fitness_logs").order_by("date", direction=firestore.Query.DESCENDING).stream()
    for d in docs:
        entry = d.to_dict()
        entry["id"] = d.id
        if isinstance(entry.get("date"), datetime):
            entry["date"] = entry["date"].strftime("%Y-%m-%d %H:%M:%S")
        logs.append(entry)

    return render_template("fitness.html", logs=logs, strava_connected=strava_connected)


@app.route("/fitness/strava_sync")
def fitness_strava_sync():
    """
    Fetch recent Strava activities for the logged-in user and
    add them to the fitness_logs subcollection (avoiding duplicates).
    """
    uid, redirect_resp = require_login()
    if redirect_resp:
        return redirect_resp

    access_token = get_strava_access_token_for_user(uid)
    if not access_token:
        return "Strava is not connected for this user.", 400

    # Fetch the user's recent activities from Strava
    resp = requests.get(
        "https://www.strava.com/api/v3/athlete/activities",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"per_page": 30},
        timeout=10,
    )
    if resp.status_code != 200:
        print("Strava activities fetch failed:", resp.text)
        flash("Strava sync failed. Please try again.", "error")
        return redirect(url_for("integrations" if request.args.get("redirect") == "integrations" else "fitness"))

    activities = resp.json()
    
    # Get existing Strava activity IDs to avoid duplicates
    existing_docs = col(uid, "fitness_logs").where(filter=FieldFilter("strava_id", "!=", "")).stream()
    existing_strava_ids = set()
    for doc in existing_docs:
        data = doc.to_dict()
        if data.get("strava_id"):
            existing_strava_ids.add(str(data["strava_id"]))
    
    imported_count = 0
    for act in activities:
        strava_id = str(act.get("id", ""))
        
        # Skip if already imported
        if strava_id in existing_strava_ids:
            continue
            
        name = act.get("name", "Strava activity")
        sport_type = act.get("sport_type") or act.get("type", "Workout")
        duration_mins = int(act.get("moving_time", 0) / 60)
        distance_m = act.get("distance", 0)
        distance_km = round(distance_m / 1000, 2)
        calories = act.get("calories", 0)
        avg_hr = act.get("average_heartrate")
        max_hr = act.get("max_heartrate")
        
        # Parse the start date
        start_date_str = act.get("start_date_local", "")
        try:
            activity_date = datetime.fromisoformat(start_date_str.replace("Z", "+00:00"))
        except:
            activity_date = datetime.now(timezone.utc)

        log_entry = {
            "date": activity_date,
            "exercise": name,
            "duration": duration_mins,
            "notes": f"{sport_type}",
            "source": "strava",
            "strava_id": strava_id,
            "sport_type": sport_type,
            "distance_km": distance_km,
            "calories": calories,
            "avg_heartrate": avg_hr,
            "max_heartrate": max_hr,
        }

        col(uid, "fitness_logs").add(log_entry)
        imported_count += 1
    
    # Update last_sync_at in integrations/strava
    _strava_token_ref(uid).set({"last_sync_at": datetime.now(timezone.utc)}, merge=True)

    if imported_count > 0:
        flash(f"Synced {imported_count} new activities from Strava.", "success")
    else:
        flash("No new activities to sync. You're up to date!", "info")
    return redirect(url_for("integrations" if request.args.get("redirect") == "integrations" else "fitness"))


@app.route('/fitness/edit/<log_id>', methods=['GET', 'POST'])
def fitness_edit(log_id):
    uid, redirect_resp = require_login()
    if redirect_resp:
        return redirect_resp

    ref = col(uid, "fitness_logs").document(log_id)

    if request.method == 'POST':
        exercise, err = validate_required(request.form.get('exercise'), "Exercise")
        if err:
            flash(err, "error")
            return redirect(url_for('fitness_edit', log_id=log_id))
        duration, err = validate_duration(request.form.get('duration', '0'))
        if err:
            flash(err, "error")
            return redirect(url_for('fitness_edit', log_id=log_id))
        notes = request.form.get('notes', '').strip()

        ref.update({"exercise": exercise, "duration": duration, "notes": notes})
        flash("Workout updated.", "success")
        return redirect(url_for('fitness'))

    snap = ref.get()
    if not snap.exists:
        return "Fitness log not found", 404

    data = snap.to_dict()
    data["id"] = log_id
    if isinstance(data.get("date"), datetime):
        data["date"] = data["date"].strftime("%Y-%m-%d %H:%M:%S")

    return render_template("fitness_edit.html", log=data)


@app.route('/fitness/delete/<log_id>', methods=['POST'])
def fitness_delete(log_id):
    uid, redirect_resp = require_login()
    if redirect_resp:
        return redirect_resp

    col(uid, "fitness_logs").document(log_id).delete()
    flash("Workout deleted.", "success")
    return redirect(url_for('fitness'))


@app.route("/profile", methods=["GET", "POST"])
def profile():
    uid, redirect_resp = require_login()
    if redirect_resp:
        return redirect_resp

    user_ref = db.collection("users").document(uid)
    
    if request.method == "POST":
        profile_data = {
            "name": request.form.get("name", ""),
            "height": request.form.get("height", ""),
            "weight": request.form.get("weight", ""),
            "age": request.form.get("age", ""),
            "gender": request.form.get("gender", "male"),
            "target_weight": request.form.get("target_weight", ""),
            "goal_type": request.form.get("goal_type", "maintain"),
            "target_weeks": request.form.get("target_weeks", ""),
            "activity_level": request.form.get("activity_level", "moderate"),
            "water_target_ml": int(request.form.get("water_target_ml") or 2000),
            "city": request.form.get("city", ""),
            "country": request.form.get("country", "").upper(),
            "goals": request.form.get("goals", ""),
            "email": session.get("user_email", ""),
            "updated_at": datetime.now()
        }
        
        # Convert height and weight to numbers if provided
        if profile_data["height"]:
            try:
                profile_data["height"] = float(profile_data["height"])
            except ValueError:
                profile_data["height"] = None
        else:
            profile_data["height"] = None
            
        if profile_data["weight"]:
            try:
                profile_data["weight"] = float(profile_data["weight"])
            except ValueError:
                profile_data["weight"] = None
        else:
            profile_data["weight"] = None
        if profile_data["age"]:
            try:
                profile_data["age"] = int(profile_data["age"])
            except ValueError:
                profile_data["age"] = None
        else:
            profile_data["age"] = None
        if profile_data["target_weight"]:
            try:
                profile_data["target_weight"] = float(profile_data["target_weight"])
            except ValueError:
                profile_data["target_weight"] = None
        else:
            profile_data["target_weight"] = None
        if profile_data["target_weeks"]:
            try:
                profile_data["target_weeks"] = int(profile_data["target_weeks"])
            except ValueError:
                profile_data["target_weeks"] = None
        else:
            profile_data["target_weeks"] = None

        user_ref.set(profile_data, merge=True)
        return redirect(url_for("profile"))
    
    # GET: Load current profile
    user_doc = user_ref.get()
    profile_data = {
        "name": "", "height": "", "weight": "", "age": "",
        "gender": "male", "target_weight": "", "goal_type": "maintain",
        "target_weeks": "", "activity_level": "moderate",
        "water_target_ml": 2000,
        "city": "", "country": "", "goals": "",
        "email": session.get("user_email", "")
    }
    
    if user_doc.exists:
        data = user_doc.to_dict()
        for k in ["height", "weight", "target_weight"]:
            v = data.get(k)
            profile_data[k] = str(v) if v is not None else ""
        for k in ["age", "target_weeks"]:
            v = data.get(k)
            profile_data[k] = str(v) if v is not None else ""
        profile_data.update({
            "name": data.get("name", ""),
            "gender": data.get("gender", "male"),
            "goal_type": data.get("goal_type", "maintain"),
            "activity_level": data.get("activity_level", "moderate"),
            "water_target_ml": data.get("water_target_ml") or 2000,
            "city": data.get("city", ""),
            "country": data.get("country", ""),
            "goals": data.get("goals", ""),
            "email": data.get("email", session.get("user_email", ""))
        })
    
    return render_template("profile.html", profile=profile_data)


@app.route("/dashboard")
def dashboard():
    uid, redirect_resp = require_login()
    if redirect_resp:
        return redirect_resp

    mood_count = sum(
        1
        for _ in db.collection("users")
                    .document(uid)
                    .collection("mood_logs")
                    .stream()
    )
    food_count = sum(1 for _ in col(uid, "food_logs").stream())
    fitness_count = sum(1 for _ in col(uid, "fitness_logs").stream())
    
    # Get user profile info
    user_ref = db.collection("users").document(uid)
    user_doc = user_ref.get()
    profile = {}
    if user_doc.exists:
        profile = user_doc.to_dict()

    quote = get_daily_quote()
    city = (profile or {}).get("city") or "Dublin"
    country = (profile or {}).get("country") or "IE"
    weather = get_weather(city, country)
    suggestion = get_activity_suggestion(weather) if weather else None

    days_param = request.args.get("days", "14")
    chart_days = int(days_param) if days_param in ("7", "14", "30") else 14
    chart_data = _get_chart_data(uid, chart_days)

    return render_template(
        "dashboard.html",
        mood_count=mood_count,
        food_count=food_count,
        fitness_count=fitness_count,
        profile=profile,
        quote=quote,
        weather=weather,
        suggestion=suggestion,
        chart_data=chart_data,
        chart_days=chart_days,
    )


# ===================== WEATHER API (OpenWeatherMap) =====================

def get_weather(city="Dublin", country_code="IE"):
    """
    Get current weather for a city using OpenWeatherMap API.
    Returns dict with temp, description, icon, etc. or None on error.
    """
    if not OPENWEATHER_API_KEY:
        return None
    
    try:
        url = f"https://api.openweathermap.org/data/2.5/weather"
        params = {
            "q": f"{city},{country_code}",
            "appid": OPENWEATHER_API_KEY,
            "units": "metric"  # Celsius
        }
        resp = requests.get(url, params=params, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            return {
                "temp": round(data["main"]["temp"]),
                "feels_like": round(data["main"]["feels_like"]),
                "description": data["weather"][0]["description"].capitalize(),
                "icon": data["weather"][0]["icon"],
                "humidity": data["main"]["humidity"],
                "wind_speed": round(data["wind"]["speed"] * 3.6, 1),  # m/s to km/h
                "city": data["name"]
            }
    except Exception as e:
        print(f"Weather API error: {e}")
    return None


def get_activity_suggestion(weather):
    """
    Suggest an activity based on weather conditions.
    Great for the buddy system to suggest outdoor activities.
    """
    if not weather:
        return "Log a workout to stay active!"
    
    temp = weather["temp"]
    desc = weather["description"].lower()
    
    # Rain or bad weather
    if any(word in desc for word in ["rain", "drizzle", "storm", "thunder", "snow"]):
        return "Indoor day! Great for gym workouts, yoga, or home exercises."
    
    # Cold weather
    if temp < 5:
        return "Bundle up! Good for a brisk run or indoor gym session."
    
    # Cool weather (perfect for running)
    if 5 <= temp < 15:
        return "Perfect running weather! Great day for outdoor cardio."
    
    # Mild weather
    if 15 <= temp < 22:
        return "Ideal conditions! Perfect for cycling, hiking, or outdoor sports."
    
    # Warm weather
    if 22 <= temp < 30:
        return "Warm day! Try swimming, early morning runs, or evening workouts."
    
    # Hot weather
    return "Hot day! Stay hydrated. Best for swimming or indoor workouts."


@app.route("/api/weather")
def api_weather():
    """API endpoint to get weather and activity suggestion."""
    city = request.args.get("city", "Dublin")
    country = request.args.get("country", "IE")
    
    weather = get_weather(city, country)
    suggestion = get_activity_suggestion(weather)
    
    return {
        "weather": weather,
        "suggestion": suggestion,
        "configured": OPENWEATHER_API_KEY is not None
    }


# ===================== MOTIVATIONAL QUOTES =====================

# Built-in fitness/wellness quotes (no API needed)
MOTIVATION_QUOTES = [
    {"quote": "The only bad workout is the one that didn't happen.", "author": "Jessica Fox"},
    {"quote": "Take care of your body. It's the only place you have to live.", "author": "Jim Rohn"},
    {"quote": "The body achieves what the mind believes.", "author": "Napoleon Hill"},
    {"quote": "Fitness is not about being better than someone else. It's about being better than you used to be.", "author": "Khloe Kardashian"},
    {"quote": "The pain you feel today will be the strength you feel tomorrow.", "author": "Arnold Schwarzenegger"},
    {"quote": "Your health is an investment, not an expense.", "author": "Unknown"},
    {"quote": "The secret of getting ahead is getting started.", "author": "Mark Twain"},
    {"quote": "Don't limit your challenges. Challenge your limits.", "author": "Unknown"},
    {"quote": "A one-hour workout is 4% of your day. No excuses.", "author": "Unknown"},
    {"quote": "Success is walking from failure to failure with no loss of enthusiasm.", "author": "Winston Churchill"},
    {"quote": "The difference between try and triumph is a little umph.", "author": "Marvin Phillips"},
    {"quote": "Strength does not come from physical capacity. It comes from an indomitable will.", "author": "Mahatma Gandhi"},
    {"quote": "You don't have to be great to start, but you have to start to be great.", "author": "Zig Ziglar"},
    {"quote": "Exercise is king. Nutrition is queen. Put them together and you've got a kingdom.", "author": "Jack LaLanne"},
    {"quote": "The groundwork for all happiness is good health.", "author": "Leigh Hunt"},
]

import random

def get_daily_quote():
    """Get a motivational quote (changes daily based on date)."""
    # Use date as seed for consistent daily quote
    today = datetime.now().strftime("%Y-%m-%d")
    random.seed(today)
    quote = random.choice(MOTIVATION_QUOTES)
    random.seed()  # Reset seed
    return quote


@app.route("/api/quote")
def api_quote():
    """API endpoint to get a motivational quote."""
    daily = request.args.get("daily", "true").lower() == "true"
    
    if daily:
        quote = get_daily_quote()
    else:
        quote = random.choice(MOTIVATION_QUOTES)
    
    return quote


# ===================== API STATUS PAGE =====================

@app.route("/api/status")
def api_status():
    """Check status of all configured APIs."""
    uid = get_current_user_id()
    
    status = {
        "strava": {
            "configured": bool(STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET),
            "connected": is_strava_connected() if uid else False
        },
        "openweather": {
            "configured": bool(OPENWEATHER_API_KEY),
            "working": False
        },
        "nutrition": {
            "configured": bool(USDA_FDC_API_KEY),
            "note": "USDA FDC - get free key at api.data.gov" if not USDA_FDC_API_KEY else "Ready"
        },
        "firebase": {
            "configured": True,
            "working": True
        }
    }
    
    # Test OpenWeather if configured
    if OPENWEATHER_API_KEY:
        weather = get_weather()
        status["openweather"]["working"] = weather is not None
    
    return status


# ===================== CHART DATA API =====================

def _to_date_str(dt):
    """Convert Firestore timestamp or datetime to YYYY-MM-DD."""
    if dt is None:
        return None
    if hasattr(dt, "strftime"):
        return dt.strftime("%Y-%m-%d")
    if hasattr(dt, "seconds"):
        return datetime.utcfromtimestamp(dt.seconds).strftime("%Y-%m-%d")
    return str(dt)[:10] if dt else None


def _get_chart_data(uid, days_back=14):
    """Return chart data for mood, food, fitness. Used by dashboard and API."""
    from collections import defaultdict

    today = datetime.now().date()
    date_keys = [(today - timedelta(days=days_back - 1 - i)).strftime("%Y-%m-%d") for i in range(days_back)]
    date_labels = [(today - timedelta(days=days_back - 1 - i)).strftime("%m/%d") for i in range(days_back)]
    limit = max(100, days_back * 5)

    mood_by_day = defaultdict(list)
    for d in col(uid, "mood_logs").order_by("date", direction=firestore.Query.DESCENDING).limit(limit).stream():
        entry = d.to_dict()
        ds = _to_date_str(entry.get("date"))
        if ds:
            mood_val = entry.get("mood")
            if mood_val is not None:
                mood_by_day[ds].append(int(mood_val))

    mood_data = []
    for dk in date_keys:
        vals = mood_by_day.get(dk, [])
        mood_data.append(round(sum(vals) / len(vals), 1) if vals else None)

    food_by_day = defaultdict(int)
    for d in col(uid, "food_logs").order_by("date", direction=firestore.Query.DESCENDING).limit(limit).stream():
        entry = d.to_dict()
        ds = _to_date_str(entry.get("date"))
        if ds:
            try:
                food_by_day[ds] += int(entry.get("calories", 0) or 0)
            except (ValueError, TypeError):
                pass

    food_data = [food_by_day.get(dk, 0) for dk in date_keys]

    fitness_by_day = defaultdict(int)
    for d in col(uid, "fitness_logs").order_by("date", direction=firestore.Query.DESCENDING).limit(limit).stream():
        entry = d.to_dict()
        ds = _to_date_str(entry.get("date"))
        if ds:
            try:
                fitness_by_day[ds] += int(entry.get("duration", 0) or 0)
            except (ValueError, TypeError):
                pass

    fitness_data = [fitness_by_day.get(dk, 0) for dk in date_keys]
    has_any = any(mood_data) or any(food_data) or any(fitness_data)
    return {
        "labels": date_labels,
        "mood": mood_data,
        "food": food_data,
        "fitness": fitness_data,
        "days": days_back,
        "empty_message": None if has_any else "No data for this period. Log some entries to see your trends."
    }


@app.route("/api/charts")
def api_charts():
    """Return chart data for mood, food, and fitness. Query param: days=7|14|30 (default 14)."""
    uid, redirect_resp = require_login()
    if redirect_resp:
        return {"error": "Not logged in"}, 401

    days_param = request.args.get("days", "14")
    days_back = int(days_param) if days_param in ("7", "14", "30") else 14
    return _get_chart_data(uid, days_back)


# ===================== ENHANCED DASHBOARD WITH WEATHER =====================

@app.route("/dashboard/widgets")
def dashboard_widgets():
    """Get dashboard widget data (weather, quote, etc.)."""
    uid, redirect_resp = require_login()
    if redirect_resp:
        return {"error": "Not logged in"}, 401
    
    weather = get_weather()
    suggestion = get_activity_suggestion(weather)
    quote = get_daily_quote()
    
    return {
        "weather": weather,
        "activity_suggestion": suggestion,
        "quote": quote
    }


@app.route("/aura")
def aura():
    """Aura AI scaffolding page (coming next iteration)."""
    uid, redirect_resp = require_login()
    if redirect_resp:
        return redirect_resp
    # In future: use recent mood/food/fitness data to generate AI insights.
    return render_template("aura.html")


@app.route("/buddies")
def buddies():
    """Buddy system page: suggestions, requests, and current buddies."""
    uid, redirect_resp = require_login()
    if redirect_resp:
        return redirect_resp

    user_doc = db.collection("users").document(uid).get()
    profile = user_doc.to_dict() if user_doc.exists else {}

    # Map location string from profile (city / country)
    map_location = None
    if profile:
        city = profile.get("city")
        country = profile.get("country")
        if city and country:
            map_location = f"{city}, {country}"
        elif city:
            map_location = city

    # Load buddy links for current user
    links = []
    for d in col(uid, "buddy_links").stream():
        data = d.to_dict() or {}
        data["other_uid"] = data.get("other_uid") or d.id
        links.append(data)

    accepted_uids = {l["other_uid"] for l in links if l.get("status") == "accepted"}
    pending_out_uids = {l["other_uid"] for l in links if l.get("status") == "pending_out"}
    pending_in_uids = {l["other_uid"] for l in links if l.get("status") == "pending_in"}

    all_linked_uids = accepted_uids | pending_out_uids | pending_in_uids

    # Fetch profiles for linked users
    user_profiles = {}
    for other_uid in all_linked_uids:
        doc = db.collection("users").document(other_uid).get()
        if doc.exists:
            user_profiles[other_uid] = doc.to_dict()

    def _build_items(uids):
        items = []
        for other_uid in uids:
            prof = user_profiles.get(other_uid, {})
            items.append({"uid": other_uid, "profile": prof})
        return items

    current_buddies = _build_items(accepted_uids)
    incoming_requests = _build_items(pending_in_uids)
    outgoing_requests = _build_items(pending_out_uids)

    # Suggested buddies: same country (if set), excluding self and already linked users
    suggestions = []
    seen = {uid} | all_linked_uids
    users_ref = db.collection("users")
    country = profile.get("country")
    if country:
        users_ref = users_ref.where("country", "==", country)
    try:
        for d in users_ref.limit(30).stream():
            other_uid = d.id
            if other_uid in seen:
                continue
            prof = d.to_dict() or {}
            suggestions.append({"uid": other_uid, "profile": prof})
    except Exception:
        suggestions = []

    return render_template(
        "buddies.html",
        profile=profile,
        google_maps_api_key=GOOGLE_MAPS_API_KEY,
        map_location=map_location,
        current_buddies=current_buddies,
        incoming_requests=incoming_requests,
        outgoing_requests=outgoing_requests,
        suggested_buddies=suggestions,
    )


@app.route("/buddies/request/<other_uid>", methods=["POST"])
def buddies_request(other_uid):
    """Send a buddy request to another user."""
    uid, redirect_resp = require_login()
    if redirect_resp:
        return redirect_resp
    if uid == other_uid:
        flash("You cannot send a buddy request to yourself.", "error")
        return redirect(url_for("buddies"))

    target_doc = db.collection("users").document(other_uid).get()
    if not target_doc.exists:
        flash("User not found.", "error")
        return redirect(url_for("buddies"))

    now = datetime.now(timezone.utc)

    # Current user's view
    my_ref = col(uid, "buddy_links").document(other_uid)
    my_data = my_ref.get().to_dict() if my_ref.get().exists else {}
    if my_data.get("status") == "accepted":
        flash("You are already buddies.", "info")
        return redirect(url_for("buddies"))
    if my_data.get("status") == "pending_out":
        flash("Buddy request already sent.", "info")
        return redirect(url_for("buddies"))

    my_ref.set(
        {"other_uid": other_uid, "status": "pending_out", "created_at": now},
        merge=True,
    )

    # Target user's view
    other_ref = col(other_uid, "buddy_links").document(uid)
    other_ref.set(
        {"other_uid": uid, "status": "pending_in", "created_at": now},
        merge=True,
    )

    flash("Buddy request sent.", "success")
    return redirect(url_for("buddies"))


@app.route("/buddies/accept/<other_uid>", methods=["POST"])
def buddies_accept(other_uid):
    """Accept an incoming buddy request."""
    uid, redirect_resp = require_login()
    if redirect_resp:
        return redirect_resp

    my_ref = col(uid, "buddy_links").document(other_uid)
    snap = my_ref.get()
    if not snap.exists or snap.to_dict().get("status") != "pending_in":
        flash("No incoming request to accept.", "error")
        return redirect(url_for("buddies"))

    my_ref.set({"status": "accepted"}, merge=True)
    other_ref = col(other_uid, "buddy_links").document(uid)
    other_ref.set({"status": "accepted"}, merge=True)

    flash("Buddy request accepted.", "success")
    return redirect(url_for("buddies"))


@app.route("/buddies/decline/<other_uid>", methods=["POST"])
def buddies_decline(other_uid):
    """Decline or cancel a buddy request."""
    uid, redirect_resp = require_login()
    if redirect_resp:
        return redirect_resp

    # Remove from both sides if present
    col(uid, "buddy_links").document(other_uid).delete()
    col(other_uid, "buddy_links").document(uid).delete()

    flash("Buddy request updated.", "info")
    return redirect(url_for("buddies"))


@app.route("/buddies/remove/<other_uid>", methods=["POST"])
def buddies_remove(other_uid):
    """Remove an existing buddy connection."""
    uid, redirect_resp = require_login()
    if redirect_resp:
        return redirect_resp

    col(uid, "buddy_links").document(other_uid).delete()
    col(other_uid, "buddy_links").document(uid).delete()

    flash("Buddy removed.", "info")
    return redirect(url_for("buddies"))


if __name__ == '__main__':
    app.run(debug=True, port=5001)

