from flask import Flask, render_template, request, redirect, url_for
import json
import os
from dotenv import load_dotenv

import firebase_admin
from firebase_admin import credentials, firestore
from firebase_admin import firestore as afs  # for SERVER_TIMESTAMP

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

# Flask app MUST be created before using @app.template_filter
app = Flask(__name__)

# TEMP until real auth
USER_ID = "demo_user"

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
    doc = db.collection("users").document(USER_ID).collection("strava_tokens").document("token").get()
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
            "redirect_uri": redirect_uri   # 👈 THIS is what was missing
        }
    )

    if response.status_code != 200:
        return f"Failed to get token: {response.text}", 400

    token_data = response.json()
    access_token = token_data.get("access_token")
    athlete = token_data.get("athlete")

    user_id = "demo_user"  # placeholder until you add login

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




@app.route('/', methods=['GET', 'POST'], endpoint='home')
def home_view():
    logs = []

    if os.path.exists("data/logs.json"):
        with open("data/logs.json", "r") as f:
            logs = json.load(f)

    if request.method == 'POST':
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

    return render_template("home.html", logs=logs)


from firebase_admin import firestore as afs  # put this with your other imports, only once

@app.route('/mood', methods=['GET', 'POST'])
def mood():
    logs = []

    # CREATE (keep your existing top-level collection + 'date' field)
    if request.method == 'POST':
        mood_val = request.form['mood']
        journal = request.form.get('journal', '')
        now = datetime.now()

        log_entry = {
            "date": now,              # keep as datetime (same as before)
            "mood": mood_val,
            "journal": journal
        }

        # optional local write (unchanged)
        try:
            local = []
            if os.path.exists("data/logs.json"):
                with open("data/logs.json", "r") as f:
                    local = json.load(f)
            local.append({
                "date": now.strftime("%Y-%m-%d %H:%M:%S"),
                "mood": mood_val,
                "journal": journal
            })
            os.makedirs("data", exist_ok=True)
            with open("data/logs.json", "w") as f:
                json.dump(local, f, indent=4)
        except Exception:
            pass

        # Firestore write to the SAME place as before
        db.collection('mood_logs').add(log_entry)
        return redirect(url_for('mood'))

    # READ (same collection/path; now we include doc IDs)
    docs = db.collection('mood_logs').order_by(
        "date", direction=firestore.Query.DESCENDING
    ).stream()

    for d in docs:
        entry = d.to_dict()
        entry["id"] = d.id   # needed for edit/delete links
        # pretty date for template
        if isinstance(entry.get("date"), datetime):
            entry["date"] = entry["date"].strftime("%Y-%m-%d %H:%M:%S")
        logs.append(entry)

    return render_template("mood.html", logs=logs)


# DELETE (same top-level collection)
@app.route('/mood/delete/<log_id>', methods=['POST'])
def mood_delete(log_id):
    db.collection('mood_logs').document(log_id).delete()
    return redirect(url_for('mood'))


# UPDATE / EDIT (same top-level collection)
@app.route('/mood/edit/<log_id>', methods=['GET', 'POST'])
def mood_edit(log_id):
    ref = db.collection('mood_logs').document(log_id)

    if request.method == 'POST':
        mood_val = request.form['mood']
        journal = request.form.get('journal', '')
        # keep original 'date' as-is; only update fields you changed
        ref.update({
            "mood": mood_val,
            "journal": journal
        })
        return redirect(url_for('mood'))

    snap = ref.get()
    if not snap.exists:
        return "Mood log not found", 404

    data = snap.to_dict()
    data["id"] = log_id
    # format date for display in the form if you want to show it
    if isinstance(data.get("date"), datetime):
        data["date"] = data["date"].strftime("%Y-%m-%d %H:%M:%S")
    return render_template("mood_edit.html", log=data)




@app.route('/food', methods=['GET', 'POST'])
def food():
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

        db.collection('food_logs').add(log_entry)

    # Retrieve from Firestore
    docs = db.collection('food_logs').order_by("date", direction=firestore.Query.DESCENDING).stream()
    for doc in docs:
        logs.append(doc.to_dict())

    return render_template("food.html", logs=logs)


@app.route('/fitness', methods=['GET', 'POST'])
def fitness():
    logs = []

    # Check Strava connection state (temporary logic)
    strava_connected = is_strava_connected()

    # Prepare Strava Auth URL (only if not connected)
    client_id = os.getenv("STRAVA_CLIENT_ID")
    redirect_uri = os.getenv("STRAVA_REDIRECT_URI")
    scope = "activity:read_all"

    auth_url = (
        f"https://www.strava.com/oauth/authorize?client_id={client_id}"
        f"&redirect_uri={redirect_uri}&response_type=code&scope={scope}"
    )

    # Load local fallback logs (optional)
    if os.path.exists("data/logs.json"):
        with open("data/logs.json", "r") as f:
            local_logs = json.load(f)
            logs.extend(local_logs)

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

        # Save to Firestore
        db.collection('fitness_logs').add(log_entry)

        # Save locally (optional)
        logs.append({
            "date": now.strftime("%Y-%m-%d %H:%M:%S"),
            "exercise": exercise,
            "duration": duration,
            "notes": notes
        })
        with open("data/logs.json", "w") as f:
            json.dump(logs, f, indent=4)

    return render_template(
        "fitness.html",
        logs=logs,
        strava_connected=strava_connected
    )


if __name__ == '__main__':
    app.run(debug=True)
