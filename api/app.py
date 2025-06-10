import os
import requests
from flask import Flask, redirect, request, session, jsonify, url_for, render_template
from dotenv import load_dotenv
from urllib.parse import urlencode
import logging
from datetime import datetime, timedelta
from collections import defaultdict
import json
import time

# --- Setup ---
logging.basicConfig(level=logging.INFO)
load_dotenv()
# Vercel requires the templates folder to be relative to the app's location in the /api directory
app = Flask(__name__, template_folder='../templates') 
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(24))

# --- Spotify Credentials and API Configuration ---
CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
# --- IMPORTANT: The Redirect URI will now point to /analyze ---
REDIRECT_URI = os.getenv("REDIRECT_URI")
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE_URL = "https://api.spotify.com/v1/"
SCOPE = "user-top-read user-library-read"

# ===================================================================
# INTERNAL HELPER FUNCTIONS
# ===================================================================

def _get_api_data(endpoint, access_token, params=None):
    headers = {'Authorization': f'Bearer {access_token}'}
    res = requests.get(API_BASE_URL + endpoint, headers=headers, params=params)
    res.raise_for_status()
    return res.json()

def _get_all_pages(url, access_token, limit=1000):
    items = []
    endpoint = url
    while endpoint and len(items) < limit:
        page_limit = min(50, limit - len(items))
        params = {'limit': page_limit}
        data = _get_api_data(endpoint, access_token, params=params)
        fetched_items = data.get('items', [])
        items.extend(fetched_items)
        next_url = data.get('next')
        endpoint = next_url.replace(API_BASE_URL, '') if next_url and len(items) < limit else None
    return items

def _get_artist_genres(artist_ids, access_token):
    genres_map = {}
    if not artist_ids: return genres_map
    for i in range(0, len(artist_ids), 50):
        chunk = artist_ids[i:i+50]
        params = {'ids': ','.join(chunk)}
        data = _get_api_data('artists', access_token, params=params)
        for artist in data.get('artists', []):
            if artist: genres_map[artist['id']] = artist.get('genres', [])
    return genres_map

def _get_season_key(dt):
    month, year = dt.month, dt.year
    if month in (1, 2): return f"Winter {year - 1}"
    if month in (3, 4, 5): return f"Spring {year}"
    if month in (6, 7, 8): return f"Summer {year}"
    if month in (9, 10, 11): return f"Autumn {year}"
    if month == 12: return f"Winter {year}"

def _get_ai_phase_details(phase_characteristics, top_artists):
    gemini_api_key = os.getenv("GEMINI_API_KEY")
    fallback = {"phase_name": f"Your {phase_characteristics['period']} Era", "phase_summary": "A distinct period in your listening journey."}
    if not gemini_api_key: return fallback
    
    gemini_api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gemini_api_key}"
    prompt = f"""
You are a creative music journalist. Based on the following data about a person's music phase, generate two things: a cool, evocative "Daylist-style" name (3-5 words, no numbers), and a short, personal, one-paragraph summary describing the vibe of this era.
**Phase Data:**
- **Period:** {phase_characteristics['period']}
- **Top Genres:** {', '.join(phase_characteristics['top_genres'])}
- **Top Artists:** {', '.join(top_artists)}
- **Era Vibe:** {'Modern' if phase_characteristics['avg_release_year'] > 2010 else 'Throwback'}
Return the response ONLY as a valid JSON object with the keys "phase_name" and "phase_summary".
"""
    schema = {"type": "OBJECT", "properties": {"phase_name": {"type": "STRING"}, "phase_summary": {"type": "STRING"}}}
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema}}
    
    try:
        response = requests.post(gemini_api_url, headers={"Content-Type": "application/json"}, data=json.dumps(payload), timeout=20)
        response.raise_for_status()
        return json.loads(response.json()['candidates'][0]['content']['parts'][0]['text'])
    except Exception as e:
        logging.error(f"AI details generation failed: {e}")
        return fallback

# ===================================================================
# FLASK ROUTES
# ===================================================================

@app.route('/')
def index():
    return render_template('login.html')

@app.route('/login')
def login():
    params = {'response_type': 'code', 'redirect_uri': REDIRECT_URI, 'scope': SCOPE, 'client_id': CLIENT_ID, 'show_dialog': 'true'}
    return redirect(f"{AUTH_URL}?{urlencode(params)}")

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/analyze')
def analyze():
    # --- NEW: This is now the callback and analysis route ---
    auth_code = request.args.get('code')
    if not auth_code:
        return render_template('login.html', error="Authentication failed. Please try again.")

    try:
        # Step 1: Exchange the code for an access token immediately
        payload = {'grant_type': 'authorization_code', 'code': auth_code, 'redirect_uri': REDIRECT_URI, 'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET}
        res = requests.post(TOKEN_URL, data=payload)
        res.raise_for_status()
        access_token = res.json().get('access_token')
        
        # Step 2: Fetch user data with the fresh token
        user_data = _get_api_data('me', access_token)
        display_name = user_data.get('display_name', 'friend')

        # Step 3: Perform analysis with the 3-year limit
        logging.info("Performing analysis for the last 3 years...")
        all_saved_tracks = _get_all_pages('me/tracks?limit=50', access_token, limit=1000)
        
        if not all_saved_tracks:
            return render_template('timeline.html', phases=[], display_name=display_name, error="No saved songs found to analyze.")
        
        all_tracks_info = {}
        all_artist_ids = set()
        for item in all_saved_tracks:
            track = item.get('track')
            if not track or not track.get('id'): continue
            artist, album = track.get('artists', [{}])[0], track.get('album', {})
            images = album.get('images', [])
            all_tracks_info[track['id']] = {'name': track.get('name', 'N/A'), 'artist_id': artist.get('id'), 'album_id': album.get('id'), 'artist_name': artist.get('name', 'N/A'), 'popularity': track.get('popularity', 0), 'release_year': int(album.get('release_date', '0').split('-')[0]), 'added_at': item.get('added_at'), 'cover_url': images[0]['url'] if images else 'https://placehold.co/128x128/121212/FFFFFF?text=?'}
            if artist.get('id'): all_artist_ids.add(artist.get('id'))

        artist_genres_map = _get_artist_genres(list(all_artist_ids), access_token)

        phases = defaultdict(list)
        for track_id, info in all_tracks_info.items():
            dt = datetime.fromisoformat(info['added_at'].replace('Z', ''))
            key = _get_season_key(dt)
            if key: phases[key].append(info)
        
        final_phases_output = []
        used_album_ids = set()
        
        def get_sort_key(key):
            season, year_str = key.split(" ")
            return int(year_str), ["Winter", "Spring", "Summer", "Autumn"].index(season)
        
        for key in sorted(phases.keys(), key=get_sort_key):
            phase_tracks = phases[key]
            if not phase_tracks: continue
            
            genres_count = defaultdict(int)
            top_artists_list = defaultdict(int)
            for track in phase_tracks:
                top_artists_list[track['artist_name']] += 1
                if track.get('artist_id') in artist_genres_map:
                    for genre in artist_genres_map.get(track['artist_id'], []):
                        genres_count[genre] += 1

            album_stats = defaultdict(lambda: {'count': 0, 'cover': ''})
            for track in phase_tracks:
                if track.get('album_id'):
                    album_stats[track['album_id']]['count'] += 1
                    album_stats[track['album_id']]['cover'] = track['cover_url']
            
            album_candidates = sorted([{'id': aid, **stats} for aid, stats in album_stats.items()], key=lambda x: x['count'], reverse=True)
            cover_url = next((c['cover'] for c in album_candidates if c['id'] not in used_album_ids), 'https://placehold.co/128x128/121212/FFFFFF?text=?')
            if cover_url != 'https://placehold.co/128x128/121212/FFFFFF?text=?': used_album_ids.add(next((c['id'] for c in album_candidates if c['cover'] == cover_url), None))

            top_artists = sorted(top_artists_list.keys(), key=top_artists_list.get, reverse=True)[:5]
            valid_years = [t['release_year'] for t in phase_tracks if t.get('release_year', 0) > 0]
            avg_year = round(sum(valid_years) / len(valid_years)) if valid_years else 'N/A'
            avg_pop = round(sum(t['popularity'] for t in phase_tracks) / len(phase_tracks)) if phase_tracks else 0
            
            phase_chars = {"period": key, "top_genres": sorted(genres_count, key=genres_count.get, reverse=True)[:5], "avg_release_year": avg_year, "avg_popularity": avg_pop}
            ai_details = _get_ai_phase_details(phase_chars, top_artists)

            final_phases_output.append({
                'phase_period': key, 'ai_phase_name': ai_details.get('phase_name', f"Your {key} Era"), 'ai_phase_summary': ai_details.get('phase_summary', "..."),
                'track_count': len(phase_tracks), 'top_genres': phase_chars['top_genres'],
                'average_popularity': avg_pop, 'average_release_year': avg_year,
                'sample_tracks': [t['name'] for t in phase_tracks[:5]], 'phase_cover_url': cover_url
            })
        
        final_phases_output.reverse()
        return render_template('timeline.html', phases=final_phases_output, display_name=display_name)

    except Exception as e:
        logging.error(f"An error occurred during analysis: {e}")
        return render_template('login.html', error="An error occurred during analysis. Please try again.")

# This is only for local development
if __name__ == '__main__':
    app.run(debug=True, port=5000)
