from flask import Flask, render_template, request, redirect, url_for, session
import json
import os
from dotenv import load_dotenv

import firebase_admin
from firebase_admin import credentials, firestore

from urllib.parse import quote_plus
import secrets
from datetime import datetime, timedelta
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

NUTRITIONIX_APP_ID = os.getenv("NUTRITIONIX_APP_ID")
NUTRITIONIX_API_KEY = os.getenv("NUTRITIONIX_API_KEY")

# OpenWeatherMap API for weather-based activity suggestions
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

# Print API config status on startup (for debugging)
print("=" * 50)
print("API Configuration Status:")
print(f"  STRAVA_CLIENT_ID: {'SET' if STRAVA_CLIENT_ID else 'MISSING'}")
print(f"  STRAVA_CLIENT_SECRET: {'SET' if STRAVA_CLIENT_SECRET else 'MISSING'}")
print(f"  OPENWEATHER_API_KEY: {'SET' if OPENWEATHER_API_KEY else 'MISSING'}")
print(f"  NUTRITIONIX_APP_ID: {'SET' if NUTRITIONIX_APP_ID else 'MISSING'}")
print("=" * 50)


def col(user_id: str, name: str):
    """users/{uid}/{name} collection ref"""
    return db.collection("users").document(user_id).collection(name)

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
    doc = db.collection("users").document(uid).collection("strava_tokens").document("token").get()
    return doc.exists


def get_strava_access_token_for_user(user_id: str):
    """
    Return a valid Strava access token for the given user.
    Refreshes the token if it is expired, updating Firestore.
    """
    token_ref = (
        db.collection("users")
          .document(user_id)
          .collection("strava_tokens")
          .document("token")
    )
    snap = token_ref.get()
    if not snap.exists:
        return None

    data = snap.to_dict() or {}
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

    token_ref.set(
        {
            "access_token": new_access,
            "refresh_token": new_refresh,
            "expires_at": new_expires,
            "athlete": data.get("athlete"),
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

    # Save tokens + athlete profile
    db.collection("users").document(user_id).collection("strava_tokens").document("token").set({
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
        mood_val = int(request.form["mood"])
        journal = request.form.get("journal", "")
        now = datetime.now()

        log_entry = {
            "date": now,
            "mood": mood_val,
            "journal": journal,
        }

        col(uid, "mood_logs").add(log_entry)
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
        print("Mood delete OK. Doc id:", log_id)
    except Exception as e:
        print("Mood delete failed:", e)

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
        mood_val = int(request.form['mood'])
        journal = request.form.get('journal', '')

        try:
            ref.update({
                "mood": mood_val,
                "journal": journal
            })
            print("Mood update OK. Doc id:", log_id)
        except Exception as e:
            print("Mood update failed:", e)

        return redirect(url_for('mood'))

    snap = ref.get()
    if not snap.exists:
        return "Mood log not found", 404

    data = snap.to_dict()
    data["id"] = log_id
    if isinstance(data.get("date"), datetime):
        data["date"] = data["date"].strftime("%Y-%m-%d %H:%M:%S")

    return render_template("mood_edit.html", log=data)


# FOOD CRUD
@app.route('/food', methods=['GET', 'POST'])
def food():
    uid, redirect_resp = require_login()
    if redirect_resp:
        return redirect_resp

    logs = []

    if request.method == 'POST':
        meal_type = request.form['meal_type']
        food_items = request.form['food_items']
        calories = request.form['calories']
        now = datetime.now()

        log_entry = {
            "date": now,
            "meal_type": meal_type,
            "food_items": food_items,
            "calories": calories
        }

        col(uid, "food_logs").add(log_entry)
        return redirect(url_for('food'))

    docs = col(uid, "food_logs").order_by("date", direction=firestore.Query.DESCENDING).stream()
    for d in docs:
        entry = d.to_dict()
        entry["id"] = d.id
        if isinstance(entry.get("date"), datetime):
            entry["date"] = entry["date"].strftime("%Y-%m-%d %H:%M:%S")
        logs.append(entry)

    return render_template("food.html", logs=logs, estimated_calories=None, nutrition_error=None)


def estimate_calories_with_nutritionix(query: str):
    """
    Call Nutritionix natural language endpoint to estimate total calories
    for a described meal.
    """
    if not NUTRITIONIX_APP_ID or not NUTRITIONIX_API_KEY:
        return None, "Nutritionix API keys are not configured."

    url = "https://trackapi.nutritionix.com/v2/natural/nutrients"
    headers = {
        "x-app-id": NUTRITIONIX_APP_ID,
        "x-app-key": NUTRITIONIX_API_KEY,
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(url, headers=headers, json={"query": query}, timeout=10)
    except Exception as e:
        return None, f"Nutritionix request failed: {e}"

    if resp.status_code != 200:
        return None, f"Nutritionix error: {resp.text}"

    data = resp.json()
    foods = data.get("foods", [])
    total_calories = sum(f.get("nf_calories", 0) for f in foods)
    return int(total_calories), None


@app.route("/food/nutritionix", methods=["POST"])
def food_nutritionix():
    """
    Use Nutritionix to estimate calories for a free-text meal description,
    then render the food page with the suggestion.
    """
    uid, redirect_resp = require_login()
    if redirect_resp:
        return redirect_resp

    query = request.form.get("nutrition_query", "").strip()
    estimated_calories = None
    error_msg = None

    if query:
        estimated_calories, error_msg = estimate_calories_with_nutritionix(query)

    # Load existing food logs
    logs = []
    docs = col(uid, "food_logs").order_by("date", direction=firestore.Query.DESCENDING).stream()
    for d in docs:
        entry = d.to_dict()
        entry["id"] = d.id
        if isinstance(entry.get("date"), datetime):
            entry["date"] = entry["date"].strftime("%Y-%m-%d %H:%M:%S")
        logs.append(entry)

    return render_template(
        "food.html",
        logs=logs,
        estimated_calories=estimated_calories,
        nutrition_error=error_msg,
        nutrition_query=query,
    )


@app.route('/food/edit/<log_id>', methods=['GET', 'POST'])
def food_edit(log_id):
    uid, redirect_resp = require_login()
    if redirect_resp:
        return redirect_resp

    ref = col(uid, "food_logs").document(log_id)

    if request.method == 'POST':
        meal_type = request.form['meal_type']
        food_items = request.form['food_items']
        calories = request.form['calories']

        ref.update({
            "meal_type": meal_type,
            "food_items": food_items,
            "calories": calories
        })
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
        exercise = request.form['exercise']
        duration = request.form['duration']
        notes = request.form['notes']
        now = datetime.now()

        log_entry = {
            "date": now,
            "exercise": exercise,
            "duration": duration,
            "notes": notes
        }

        col(uid, "fitness_logs").add(log_entry)
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
        return redirect(url_for("fitness"))

    activities = resp.json()
    
    # Get existing Strava activity IDs to avoid duplicates
    existing_docs = col(uid, "fitness_logs").where("strava_id", "!=", "").stream()
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
            activity_date = datetime.utcnow()

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
    
    print(f"Imported {imported_count} new Strava activities for user {uid}")
    return redirect(url_for("fitness"))


@app.route('/fitness/edit/<log_id>', methods=['GET', 'POST'])
def fitness_edit(log_id):
    uid, redirect_resp = require_login()
    if redirect_resp:
        return redirect_resp

    ref = col(uid, "fitness_logs").document(log_id)

    if request.method == 'POST':
        exercise = request.form['exercise']
        duration = request.form['duration']
        notes = request.form['notes']

        ref.update({
            "exercise": exercise,
            "duration": duration,
            "notes": notes
        })
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
    return redirect(url_for('fitness'))

@app.route("/profile", methods=["GET", "POST"])
def profile():
    uid, redirect_resp = require_login()
    if redirect_resp:
        return redirect_resp

    user_ref = db.collection("users").document(uid)
    
    if request.method == "POST":
        # Update profile data
        profile_data = {
            "name": request.form.get("name", ""),
            "height": request.form.get("height", ""),
            "weight": request.form.get("weight", ""),
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
        
        user_ref.set(profile_data, merge=True)
        return redirect(url_for("profile"))
    
    # GET: Load current profile
    user_doc = user_ref.get()
    profile_data = {
        "name": "",
        "height": "",
        "weight": "",
        "city": "",
        "country": "",
        "goals": "",
        "email": session.get("user_email", "")
    }
    
    if user_doc.exists:
        data = user_doc.to_dict()
        # Convert height and weight to strings for form display, or empty string if None
        height_val = data.get("height")
        weight_val = data.get("weight")
        profile_data.update({
            "name": data.get("name", ""),
            "height": str(height_val) if height_val is not None else "",
            "weight": str(weight_val) if weight_val is not None else "",
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

    return render_template(
        "dashboard.html",
        mood_count=mood_count,
        food_count=food_count,
        fitness_count=fitness_count,
        profile=profile
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
        "nutritionix": {
            "configured": bool(NUTRITIONIX_APP_ID and NUTRITIONIX_API_KEY),
            "note": "Waiting for API access" if not NUTRITIONIX_APP_ID else "Ready"
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


@app.route("/api/charts")
def api_charts():
    """Return chart data for mood, food, and fitness (last 14 days)."""
    uid, redirect_resp = require_login()
    if redirect_resp:
        return {"error": "Not logged in"}, 401

    from collections import defaultdict

    # Last 14 days
    days_back = 14
    today = datetime.now().date()
    date_labels = [(today - timedelta(days=i)).strftime("%m/%d") for i in range(days_back - 1, -1, -1)]
    date_keys = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days_back - 1, -1, -1)]

    # Mood: average per day (1-5 scale)
    mood_by_day = defaultdict(list)
    for d in col(uid, "mood_logs").order_by("date", direction=firestore.Query.DESCENDING).limit(50).stream():
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

    # Food: total calories per day
    food_by_day = defaultdict(int)
    for d in col(uid, "food_logs").order_by("date", direction=firestore.Query.DESCENDING).limit(50).stream():
        entry = d.to_dict()
        ds = _to_date_str(entry.get("date"))
        if ds:
            try:
                food_by_day[ds] += int(entry.get("calories", 0) or 0)
            except (ValueError, TypeError):
                pass

    food_data = [food_by_day.get(dk, 0) for dk in date_keys]

    # Fitness: total minutes per day
    fitness_by_day = defaultdict(int)
    for d in col(uid, "fitness_logs").order_by("date", direction=firestore.Query.DESCENDING).limit(80).stream():
        entry = d.to_dict()
        ds = _to_date_str(entry.get("date"))
        if ds:
            try:
                fitness_by_day[ds] += int(entry.get("duration", 0) or 0)
            except (ValueError, TypeError):
                pass

    fitness_data = [fitness_by_day.get(dk, 0) for dk in date_keys]

    return {
        "labels": date_labels,
        "mood": mood_data,
        "food": food_data,
        "fitness": fitness_data,
    }


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


if __name__ == '__main__':
    app.run(debug=True, port=5001)

