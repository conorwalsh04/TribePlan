from flask import Flask, render_template, request, url_for
import json, os
import firebase_admin
from firebase_admin import credentials, firestore

# Path to the key file
cred = credentials.Certificate("firebase/tribeplan-service-account.json")
firebase_admin.initialize_app(cred)

# Initialize Firestore (or Realtime DB later)
db = firestore.client()

from datetime import datetime

app = Flask(__name__)

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


@app.route('/mood', methods=['GET', 'POST'])
def mood():
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

    return render_template("mood.html", logs=logs)


@app.route('/food')
def food():
    return render_template("food.html")

@app.route('/fitness')
def fitness():
    return render_template("fitness.html")

if __name__ == '__main__':
    app.run(debug=True)
