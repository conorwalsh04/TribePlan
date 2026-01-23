from flask import Flask, render_template, request, redirect, url_for, session
import json
import os
from dotenv import load_dotenv

import firebase_admin
from firebase_admin import credentials, firestore

from urllib.parse import quote_plus
import secrets
from datetime import datetime
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
    client_id = os.getenv("STRAVA_CLIENT_ID")
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


@app.route("/strava/callback")
def strava_callback():
    code = request.args.get("code")
    if not code:
        return "No code provided from Strava.", 400

    # Build the redirect URI exactly as used in authorize
    redirect_uri = url_for("strava_callback", _external=True)

    response = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": os.getenv("STRAVA_CLIENT_ID"),
            "client_secret": os.getenv("STRAVA_CLIENT_SECRET"),
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri
        }
    )

    if response.status_code != 200:
        return f"Failed to get token: {response.text}", 400

    token_data = response.json()
    access_token = token_data.get("access_token")
    athlete = token_data.get("athlete")

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

    return f"""
    <h1>✅ Strava Connected</h1>
    <p>Welcome, {athlete.get('firstname', 'User')}! Your Strava account has been connected successfully.</p>
    <a href="/fitness">Return to Fitness Page</a>
    """




@app.route('/')
def root():
    """Redirect root to login page"""
    return redirect(url_for('login'))


@app.route('/home', methods=['GET', 'POST'], endpoint='home')
def home_view():
    uid = get_current_user_id()
    is_logged_in = uid is not None
    logs = []

    if os.path.exists("data/logs.json"):
        with open("data/logs.json", "r") as f:
            logs = json.load(f)

    # Only allow POST (logging) if user is logged in
    if request.method == 'POST':
        if not is_logged_in:
            return redirect(url_for('login'))
        
        mood = request.form['mood']
        journal = request.form['journal']
        log_entry = {
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mood": mood,
            "journal": journal
        }
        logs.append(log_entry)

        with open("data/logs.json", "w") as f:
            json.dump(logs, f, indent=4)

    return render_template("home.html", logs=logs, is_logged_in=is_logged_in)


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

    return render_template("food.html", logs=logs)


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

    client_id = os.getenv("STRAVA_CLIENT_ID")
    redirect_uri = os.getenv("STRAVA_REDIRECT_URI")
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

if __name__ == '__main__':
    app.run(debug=True, port=5001)

