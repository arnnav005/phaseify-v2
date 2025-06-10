import os
import requests
from flask import Flask, redirect, request, session, jsonify, url_for, render_template
from dotenv import load_dotenv
from urllib.parse import urlencode
import logging
from collections import defaultdict
import json
import time

# --- Setup ---
logging.basicConfig(level=logging.INFO)
load_dotenv()
# Vercel requires the templates folder to be relative to the app's location
app = Flask(__name__, template_folder='../templates') 
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(24))

# --- Spotify Credentials and API Configuration ---
CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE_URL = "https://api.spotify.com/v1/"
SCOPE = "user-top-read" # We only need the top tracks now

# ===================================================================
# INTERNAL HELPER FUNCTIONS
# ===================================================================

def _get_api_data(endpoint, access_token, params=None):
    headers = {'Authorization': f'Bearer {access_token}'}
    res = requests.get(API_BASE_URL + endpoint, headers=headers, params=params)
    res.raise_for_status()
    return res.json()

def _get_artist_genres(artist_ids, access_token):
    genres_map = {}
    if not artist_ids: return genres_map
    for i in range(0, len(artist_ids), 50):
        chunk = artist_ids[i:i+50]
        params = {'ids': ','.join(chunk)}
        data = _get_api_data('artists', access_token, params=params)
        for artist in data.get('artists', []):
            if artist:
                genres_map[artist['id']] = artist.get('genres', [])
    return genres_map

def _get_ai_phase_name(phase_characteristics):
    gemini_api_key = os.getenv("GEMINI_API_KEY")
    fallback_name = f"Your {phase_characteristics['period']} Era"
    if not gemini_api_key: return fallback_name
    
    gemini_api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gemini_api_key}"
    prompt = f"""
You are an expert at creating cool, personal names for music phases. Your goal is to create a simple, evocative name based on the data provided. The name should be 3 to 5 words long and not include numbers.

**PHASE DATA:**
- **Time Period:** {phase_characteristics['period']}
- **Top Genres:** {', '.join(phase_characteristics['top_genres'])}
- **Vibe:** {'Modern' if phase_characteristics['avg_release_year'] > 2010 else 'Throwback'}

Generate only the name, without any extra text or quotation marks.
"""
    
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    try:
        response = requests.post(gemini_api_url, headers={"Content-Type": "application/json"}, data=json.dumps(payload), timeout=20)
        response.raise_for_status()
        return response.json()['candidates'][0]['content']['parts'][0]['text'].strip().replace('"', '')
    except Exception as e:
        logging.error(f"AI name generation failed: {e}")
        return fallback_name

# ===================================================================
# FLASK ROUTES
# ===================================================================

@app.route('/')
def index():
    return render_template('login.html')

@app.route('/login')
def login():
    params = {'response_type': 'code', 'redirect_uri': REDIRECT_URI, 'scope': SCOPE, 'client_id': CLIENT_ID}
    return redirect(f"{AUTH_URL}?{urlencode(params)}")

@app.route('/callback')
def callback():
    if 'error' in request.args: return render_template('login.html', error=request.args['error'])
    if 'code' in request.args:
        payload = {'grant_type': 'authorization_code', 'code': request.args['code'], 'redirect_uri': REDIRECT_URI, 'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET}
        res = requests.post(TOKEN_URL, data=payload)
        session['access_token'] = res.json().get('access_token')
        
        user_data = _get_api_data('me', session['access_token'])
        session['display_name'] = user_data.get('display_name', 'friend')
        
        return redirect(url_for('timeline'))
    return render_template('login.html', error="Authentication failed.")

@app.route('/timeline')
def timeline():
    access_token = session.get('access_token')
    if not access_token: return redirect(url_for('index'))

    try:
        time_ranges = {
            'Your Current Obsession': 'short_term',
            'Your Recent Vibe': 'medium_term',
            'Your All-Time DNA': 'long_term'
        }
        
        final_phases = []
        
        for name, term in time_ranges.items():
            logging.info(f"Analyzing {name} ({term})...")
            top_tracks = _get_api_data('me/top/tracks', access_token, {'limit': 50, 'time_range': term})['items']
            if not top_tracks: continue

            artist_ids = {t['artists'][0]['id'] for t in top_tracks if t.get('artists')}
            genres_map = _get_artist_genres(list(artist_ids), access_token)

            genres_count = defaultdict(int)
            for track in top_tracks:
                artist_id = track['artists'][0]['id'] if track.get('artists') else None
                if artist_id in genres_map:
                    for genre in genres_map[artist_id]:
                        genres_count[genre] += 1

            valid_years = [int(t['album']['release_date'].split('-')[0]) for t in top_tracks if t.get('album') and t['album'].get('release_date') and '-' in t['album']['release_date']]
            avg_year = round(sum(valid_years) / len(valid_years)) if valid_years else 'N/A'
            
            phase_chars = {"period": name, "top_genres": sorted(genres_count, key=genres_count.get, reverse=True)[:5], "avg_release_year": avg_year}
            ai_name = _get_ai_phase_name(phase_chars)

            final_phases.append({
                'phase_title': name,
                'ai_phase_name': ai_name,
                'sample_tracks': [t['name'] for t in top_tracks[:5]],
                'phase_cover_url': top_tracks[0]['album']['images'][0]['url'] if top_tracks[0].get('album', {}).get('images') else 'https://placehold.co/128x128/121212/FFFFFF?text=?'
            })
            time.sleep(1) # Small delay to be nice to the APIs

        return render_template('timeline.html', phases=final_phases, display_name=session.get('display_name'))

    except Exception as e:
        logging.error(f"An error occurred during analysis: {e}")
        return render_template('login.html', error="An error occurred during analysis. Please try logging out and back in.")

# This is only for local development
if __name__ == '__main__':
    app.run(debug=True, port=5000)
