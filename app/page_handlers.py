"""
page_handlers.py — App-specific handlers for playlistrec.

Custom endpoints registered by register_routes():
  GET    /api/profiles                                  — list profiles
  POST   /api/profiles                                  — create profile
  GET    /api/profiles/<id>                             — get profile
  PUT    /api/profiles/<id>                             — update profile
  DELETE /api/profiles/<id>                             — delete profile
  GET    /api/profiles/<id>/feedback                    — get feedback + notes
  POST   /api/profiles/<id>/feedback                    — upsert rating
  DELETE /api/profiles/<id>/feedback                    — clear all feedback
  GET    /api/spotify/playlists                         — list user's Spotify playlists
  POST   /api/spotify/playlists/<pid>/link              — fetch tracks, generate sonic profile, store on profile
  POST   /api/spotify/playlists/<pid>/refresh-profile   — re-generate sonic profile for linked playlist
  POST   /api/spotify/playlists/<pid>/add               — search + add yes-rated tracks to playlist
  POST   /api/playlist-rec/run                          — two-pass recommendation pipeline
"""

import os
import json
import logging
import requests
from collections import defaultdict
from flask import request, jsonify

import db
from config import get_claude_model
from spotify_auth import spotify_get, spotify_post

logger = logging.getLogger(__name__)

# ── Last.fm ───────────────────────────────────────────────────────────────────
LASTFM_ENDPOINT    = "https://ws.audioscrobbler.com/2.0/"
LASTFM_SEED_ARTISTS = ["Weezer", "Barenaked Ladies", "Fountains of Wayne"]
LASTFM_SIMILAR_COUNT = 15


# ── Spotify playlist helpers ──────────────────────────────────────────────────

def _fetch_all_playlist_tracks(playlist_id):
    """
    Fetch every track from a Spotify playlist, handling pagination.
    Returns list of dicts: {track_name, artist_name, artist_names: [...]}
    """
    tracks = []
    url = f"/playlists/{playlist_id}/tracks"
    params = {"fields": "items(track(name,artists(name))),next", "limit": 100}

    while url:
        data, err = spotify_get(url, params=params)
        if err:
            raise RuntimeError(f"Spotify error fetching tracks: {err}")
        for item in data.get("items", []):
            t = item.get("track")
            if not t or not t.get("name"):
                continue
            artist_names = [a["name"] for a in t.get("artists", []) if a.get("name")]
            if artist_names:
                tracks.append({
                    "track_name":  t["name"],
                    "artist_name": artist_names[0],
                    "artist_names": artist_names,
                })
        # Spotify returns full next URL; strip the base for spotify_get
        next_url = data.get("next")
        if next_url:
            url = next_url.replace("https://api.spotify.com/v1", "")
            params = {}
        else:
            url = None

    return tracks


def _group_tracks_by_artist(tracks):
    """
    Group track list into {artist: [track_name, ...]} ordered by appearance count.
    Returns list of (artist, [tracks]) tuples, most-represented artist first.
    """
    artist_tracks = defaultdict(list)
    for t in tracks:
        artist_tracks[t["artist_name"]].append(t["track_name"])
    # Sort by number of tracks descending
    return sorted(artist_tracks.items(), key=lambda x: -len(x[1]))


def _build_sonic_profile_prompt(grouped_tracks):
    """Build the pre-pass prompt for sonic profile generation."""
    lines = [
        "You are a music expert tasked with building a detailed sonic profile of a listener's taste.",
        "Based on the playlist below, describe what this person's music taste sounds like.",
        "",
        "Focus ONLY on sonic and musical properties — not cultural associations, era, or demographics.",
        "Describe: vocal style, instrumentation, guitar tone if present, tempo feel, energy level,",
        "harmonic complexity, production style, emotional register, and any other defining sonic traits.",
        "Note which artists share which qualities, and weight artists by their track count.",
        "End with a short 'Avoid:' line listing sonic qualities clearly absent or unwanted.",
        "",
        "Be specific and detailed. This description will be used to find new music the listener hasn't heard.",
        "Do NOT mention artist names in your description — only sonic properties.",
        "",
        "## Playlist (grouped by artist, track count in parentheses)",
    ]

    for artist, track_list in grouped_tracks:
        count = len(track_list)
        sample = track_list[:8]  # cap per-artist track list to keep tokens reasonable
        lines.append(f"\n{artist} ({count} tracks):")
        for t in sample:
            lines.append(f"  - {t}")
        if count > 8:
            lines.append(f"  - ... and {count - 8} more")

    lines += [
        "",
        "## Sonic Profile",
        "Write 3–5 detailed paragraphs describing this listener's sonic taste.",
        "Do not mention specific artists. Focus entirely on sound.",
    ]
    return "\n".join(lines)


def _generate_sonic_profile(claude_client, grouped_tracks):
    """
    Run the sonic profile pre-pass. Returns profile text string.
    Raises RuntimeError on failure.
    """
    model = get_claude_model()
    prompt = _build_sonic_profile_prompt(grouped_tracks)
    try:
        resp = claude_client.messages.create(
            model=model,
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        raise RuntimeError(f"Sonic profile generation failed: {e}")


# ── Last.fm ───────────────────────────────────────────────────────────────────

def fetch_lastfm_similar(api_key, seed_artists=None, count=LASTFM_SIMILAR_COUNT):
    """
    Fetch similar artists from Last.fm for each seed artist.
    Returns deduplicated list of artist names.
    """
    if not api_key:
        return []
    seeds = seed_artists or LASTFM_SEED_ARTISTS
    seen = set(s.lower() for s in seeds)
    results = []

    for artist in seeds:
        try:
            resp = requests.get(
                LASTFM_ENDPOINT,
                params={
                    "method":  "artist.getsimilar",
                    "artist":  artist,
                    "api_key": api_key,
                    "format":  "json",
                    "limit":   count,
                },
                timeout=5,
            )
            data = resp.json()
            for a in data.get("similarartists", {}).get("artist", []):
                name = a.get("name", "").strip()
                if name and name.lower() not in seen:
                    seen.add(name.lower())
                    results.append(name)
        except Exception as e:
            logger.warning(f"Last.fm error for {artist}: {e}")

    return results


# ── Prompt builders ───────────────────────────────────────────────────────────

def _build_pass1_prompt(profile, similar_artists, feedback, playlist_tracks=None):
    sonic_profile  = profile.get("sonic_profile") or ""
    seed_artists   = profile.get("seed_artists") or []
    disliked       = profile.get("disliked_artists") or []

    yes_tracks   = [k for k, v in feedback.items() if v == "yes"]
    maybe_tracks = [k for k, v in feedback.items() if v == "maybe"]
    no_tracks    = [k for k, v in feedback.items() if v == "no"]

    lines = [
        "You are generating a raw candidate list of song recommendations for human validation.",
        "This is Pass 1 — do NOT self-correct, retract, or second-guess any suggestion.",
        "Output ONLY the structured format below. No preamble. No explanation. No self-correction.",
        "",
        "## Listener Profile",
        f"Seed artists: {', '.join(seed_artists) if seed_artists else 'None'}",
        f"Disliked artists (exclude entirely): {', '.join(disliked) if disliked else 'None'}",
    ]

    if sonic_profile:
        lines += [
            "",
            "## Sonic Profile (prioritise this over era or cultural association)",
            sonic_profile,
        ]

    if similar_artists:
        lines += [
            "",
            "## Last.fm Similar Artists (bias candidates toward these)",
            ", ".join(similar_artists[:20]),
        ]

    if yes_tracks:
        lines += ["", "## Confirmed Good Fits (find more songs like these)"] + yes_tracks
    if maybe_tracks:
        lines += ["", "## Borderline (calibration signal — note nuances)"] + maybe_tracks
    if no_tracks:
        lines += ["", "## Hard Exclusions (do not recommend these artists or songs)"] + no_tracks

    if playlist_tracks:
        lines += [
            "",
            "## Already In Playlist — Do Not Recommend These",
            "(These tracks are already in the user's playlist. Exclude them and their obvious variants.)",
        ]
        for t in playlist_tracks:
            lines.append(f'  "{t["track_name"]}" — {t["artist_name"]}')

    lines += [
        "",
        "## IMPORTANT: Prioritise sonic similarity over era or cultural association.",
        "Recommend songs from ANY era that match the sonic profile, not just contemporaries.",
        "Include both well-known and deep-cut / less-popular tracks.",
        "",
        "## Output Format (20 candidates, one per line)",
        '"Song Title" – Artist (Year) | solo:[yes/no/notable] | [one sentence on sonic fit]',
        "",
        "Generate 20 candidates now:",
    ]

    return "\n".join(lines)


def _build_pass2_prompt(profile, candidates_text, feedback):
    disliked  = profile.get("disliked_artists") or []
    no_tracks = [k for k, v in feedback.items() if v == "no"]

    lines = [
        "You are validating and filtering a candidate song list. Apply the rules strictly.",
        "Output ONLY the structured format below. No preamble. No explanation. No self-correction.",
        "The final output MUST begin with the word RECOMMENDATIONS on its own line.",
        "",
        "## Hard Rejection Rules",
        "Reject a candidate if ANY of the following are true:",
        f"  - Artist is in disliked list: {', '.join(disliked) if disliked else 'None'}",
        "  - Already in the user's collection (common knowledge check)",
        "  - solo:no with no other strong qualifying factors",
    ]

    if no_tracks:
        lines += [
            "  - Song or artist appears in session exclusion list:",
        ] + [f"      {t}" for t in no_tracks]

    lines += [
        "",
        "## Candidate List",
        candidates_text,
        "",
        "## Output",
        "RECOMMENDATIONS",
        "List 8–10 approved songs, one per line, in the same format as the input.",
        "",
        "ARTIST RABBIT HOLES",
        "List 3–5 artist names worth exploring, one per line.",
    ]

    return "\n".join(lines)


# ── Pipeline runner ───────────────────────────────────────────────────────────

def run_pipeline(profile, claude_client, lastfm_api_key, feedback):
    """
    Execute the two-pass recommendation pipeline.
    Returns (result_dict, status_code).
    """
    if not claude_client:
        return {"error": "Claude API not available"}, 503

    model = get_claude_model()

    # Last.fm similar artists (seeded from profile seed_artists)
    similar = fetch_lastfm_similar(lastfm_api_key, profile.get("seed_artists"))

    # Fetch playlist tracks for exclusion if a playlist is linked
    playlist_tracks = []
    playlist_id = profile.get("spotify_playlist_id")
    if playlist_id:
        try:
            playlist_tracks = _fetch_all_playlist_tracks(playlist_id)
        except Exception as e:
            logger.warning(f"Could not fetch playlist tracks for exclusion: {e}")

    # Pass 1
    p1_prompt = _build_pass1_prompt(profile, similar, feedback, playlist_tracks)
    try:
        p1_resp = claude_client.messages.create(
            model=model,
            max_tokens=1500,
            messages=[{"role": "user", "content": p1_prompt}],
        )
        candidates_text = p1_resp.content[0].text.strip()
    except Exception as e:
        logger.error(f"Pass 1 error: {e}")
        return {"error": f"Pass 1 Claude error: {str(e)}"}, 500

    # Pass 2
    p2_prompt = _build_pass2_prompt(profile, candidates_text, feedback)
    try:
        p2_resp = claude_client.messages.create(
            model=model,
            max_tokens=1500,
            messages=[{"role": "user", "content": p2_prompt}],
        )
        p2_text = p2_resp.content[0].text.strip()
    except Exception as e:
        logger.error(f"Pass 2 error: {e}")
        return {"error": f"Pass 2 Claude error: {str(e)}"}, 500

    # Strip anything before RECOMMENDATIONS as a safety net
    if "RECOMMENDATIONS" in p2_text:
        p2_text = p2_text[p2_text.index("RECOMMENDATIONS"):]

    # Parse output
    recommendations = []
    rabbit_holes    = []
    section         = None

    for line in p2_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.upper().startswith("RECOMMENDATIONS"):
            section = "rec"
            continue
        if stripped.upper().startswith("ARTIST RABBIT HOLES"):
            section = "rabbit"
            continue
        if section == "rec" and stripped.startswith('"'):
            recommendations.append(stripped)
        elif section == "rabbit" and stripped:
            rabbit_holes.append(stripped)

    return {
        "recommendations":      recommendations,
        "rabbit_holes":         rabbit_holes,
        "pass1_raw":            candidates_text,
        "lastfm_similar_count": len(similar),
        "playlist_tracks_excluded": len(playlist_tracks),
    }, 200


# ── Route registration ────────────────────────────────────────────────────────

def register_routes(app, claude_client_ref):
    """
    Register playlistrec-specific API routes on the Flask app.
    Called from app.py after create_app().
    """
    import db as _db
    _db.init_db()

    get_claude_model()

    from spotify_auth import register_spotify_routes
    register_spotify_routes(app)

    lastfm_key = os.environ.get("LASTFM_API_KEY", "")

    # ── Profile CRUD ──────────────────────────────────────────────────────────

    @app.route("/api/profiles", methods=["GET"])
    def list_profiles():
        return jsonify({"profiles": _db.get_all_profiles()})

    @app.route("/api/profiles", methods=["POST"])
    def create_profile():
        data = request.get_json(force=True, silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name is required"}), 400
        if _db.get_profile_by_name(name):
            return jsonify({"error": f"Profile '{name}' already exists"}), 409
        profile = _db.create_profile(
            name=name,
            seed_artists=data.get("seed_artists", []),
            disliked_artists=data.get("disliked_artists", []),
        )
        return jsonify({"profile": profile}), 201

    @app.route("/api/profiles/<int:pid>", methods=["GET"])
    def get_profile(pid):
        p = _db.get_profile(pid)
        if not p:
            return jsonify({"error": "Not found"}), 404
        return jsonify({"profile": p})

    @app.route("/api/profiles/<int:pid>", methods=["PUT"])
    def update_profile(pid):
        if not _db.get_profile(pid):
            return jsonify({"error": "Not found"}), 404
        data = request.get_json(force=True, silent=True) or {}
        kwargs = {}
        for field in ("name", "seed_artists", "disliked_artists"):
            if field in data:
                kwargs[field] = data[field]
        _db.update_profile(pid, **kwargs)
        return jsonify({"profile": _db.get_profile(pid)})

    @app.route("/api/profiles/<int:pid>", methods=["DELETE"])
    def delete_profile(pid):
        if not _db.get_profile(pid):
            return jsonify({"error": "Not found"}), 404
        _db.delete_profile(pid)
        return jsonify({"deleted": pid})

    # ── Feedback ──────────────────────────────────────────────────────────────

    @app.route("/api/profiles/<int:pid>/feedback", methods=["GET"])
    def get_feedback(pid):
        if not _db.get_profile(pid):
            return jsonify({"error": "Not found"}), 404
        ratings = _db.get_feedback(pid)
        notes   = _db.get_maybe_notes(pid)
        return jsonify({"feedback": ratings, "notes": notes})

    @app.route("/api/profiles/<int:pid>/feedback", methods=["POST"])
    def set_feedback(pid):
        if not _db.get_profile(pid):
            return jsonify({"error": "Not found"}), 404
        data = request.get_json(force=True, silent=True) or {}
        track_key = (data.get("track_key") or "").strip()
        rating    = data.get("rating", "")
        if not track_key or rating not in ("yes", "maybe", "no"):
            return jsonify({"error": "track_key and rating (yes/maybe/no) required"}), 400
        _db.set_feedback(pid, track_key, rating)
        if rating == "maybe" and "note" in data:
            _db.set_maybe_note(pid, track_key, data["note"])
        return jsonify({"ok": True})

    @app.route("/api/profiles/<int:pid>/feedback", methods=["DELETE"])
    def clear_feedback(pid):
        if not _db.get_profile(pid):
            return jsonify({"error": "Not found"}), 404
        _db.clear_feedback(pid)
        return jsonify({"ok": True})

    # ── Spotify: list playlists ───────────────────────────────────────────────

    @app.route("/api/spotify/playlists", methods=["GET"])
    def list_spotify_playlists():
        playlists = []
        url    = "/me/playlists"
        params = {"limit": 50}

        while url:
            data, err = spotify_get(url, params=params)
            if err:
                return jsonify({"error": err}), 502
            for item in data.get("items", []):
                if item:
                    playlists.append({
                        "id":     item["id"],
                        "name":   item["name"],
                        "tracks": item.get("tracks", {}).get("total", 0),
                        "image":  (item.get("images") or [{}])[0].get("url"),
                    })
            next_url = data.get("next")
            url    = next_url.replace("https://api.spotify.com/v1", "") if next_url else None
            params = {}

        return jsonify({"playlists": playlists})

    # ── Spotify: link playlist → generate sonic profile ───────────────────────

    def _link_playlist_to_profile(pid, playlist_id, claude_client):
        """
        Shared logic for initial link and refresh:
        fetch tracks, generate sonic profile, persist both on profile.
        Returns (response_dict, status_code).
        """
        profile = _db.get_profile(pid)
        if not profile:
            return {"error": "Profile not found"}, 404

        # Fetch all tracks
        try:
            tracks = _fetch_all_playlist_tracks(playlist_id)
        except RuntimeError as e:
            return {"error": str(e)}, 502

        if not tracks:
            return {"error": "Playlist is empty or inaccessible"}, 400

        grouped = _group_tracks_by_artist(tracks)

        # Generate sonic profile via Claude pre-pass
        if not claude_client:
            return {"error": "Claude API not available"}, 503
        try:
            sonic_profile = _generate_sonic_profile(claude_client, grouped)
        except RuntimeError as e:
            return {"error": str(e)}, 500

        # Extract top artists for seed_artists if profile has none
        top_artists = [artist for artist, _ in grouped[:15]]

        updates = {
            "spotify_playlist_id": playlist_id,
            "sonic_profile":       sonic_profile,
        }
        if not profile.get("seed_artists"):
            updates["seed_artists"] = top_artists

        _db.update_profile(pid, **updates)

        return {
            "ok":            True,
            "track_count":   len(tracks),
            "artist_count":  len(grouped),
            "sonic_profile": sonic_profile,
            "top_artists":   top_artists,
        }, 200

    @app.route("/api/spotify/playlists/<playlist_id>/link/<int:pid>", methods=["POST"])
    def link_playlist(playlist_id, pid):
        result, status = _link_playlist_to_profile(pid, playlist_id, claude_client_ref())
        if status == 200:
            result["profile"] = _db.get_profile(pid)
        return jsonify(result), status

    @app.route("/api/spotify/playlists/<playlist_id>/refresh-profile/<int:pid>", methods=["POST"])
    def refresh_sonic_profile(playlist_id, pid):
        result, status = _link_playlist_to_profile(pid, playlist_id, claude_client_ref())
        if status == 200:
            result["profile"] = _db.get_profile(pid)
        return jsonify(result), status

    # ── Spotify: add tracks to playlist ──────────────────────────────────────

    @app.route("/api/spotify/playlists/<playlist_id>/add", methods=["POST"])
    def add_tracks_to_playlist(playlist_id):
        data        = request.get_json(force=True, silent=True) or {}
        track_strs  = data.get("tracks", [])   # list of "Song Title" – Artist strings
        if not track_strs:
            return jsonify({"error": "tracks list is required"}), 400

        uris     = []
        failures = []

        for track_str in track_strs:
            # Parse: "Song Title" – Artist (Year) | ...
            # Extract the part before the first |
            core = track_str.split("|")[0].strip()
            # Remove surrounding quotes from title portion
            core = core.replace('"', '')
            # Split on em-dash or regular dash with surrounding space
            for sep in [" – ", " - ", "–", "-"]:
                if sep in core:
                    parts = core.split(sep, 1)
                    title  = parts[0].strip()
                    artist = parts[1].strip()
                    # Strip year if present: "Artist (Year)" → "Artist"
                    if artist.endswith(")") and "(" in artist:
                        artist = artist[:artist.rfind("(")].strip()
                    break
            else:
                title  = core
                artist = ""

            query = f"track:{title} artist:{artist}" if artist else f"track:{title}"
            search_data, err = spotify_get("/search", params={
                "q": query, "type": "track", "limit": 1
            })
            if err or not search_data:
                failures.append({"track": track_str, "reason": err or "no results"})
                continue

            items = search_data.get("tracks", {}).get("items", [])
            if not items:
                failures.append({"track": track_str, "reason": "no search results"})
                continue

            uris.append(items[0]["uri"])

        if not uris:
            return jsonify({"error": "No tracks could be resolved", "failures": failures}), 400

        # Add in batches of 100 (Spotify limit)
        added = 0
        for i in range(0, len(uris), 100):
            batch = uris[i:i + 100]
            _, err = spotify_post(f"/playlists/{playlist_id}/tracks", {"uris": batch})
            if err:
                return jsonify({"error": f"Spotify error adding tracks: {err}"}), 502
            added += len(batch)

        return jsonify({
            "ok":       True,
            "added":    added,
            "failures": failures,
        })

    # ── Recommendation pipeline ───────────────────────────────────────────────

    @app.route("/api/playlist-rec/run", methods=["POST"])
    def run_recommendations():
        data = request.get_json(force=True, silent=True) or {}
        pid  = data.get("profile_id")
        if not pid:
            return jsonify({"error": "profile_id required"}), 400
        profile = _db.get_profile(int(pid))
        if not profile:
            return jsonify({"error": "Profile not found"}), 404

        # Require either a sonic profile (new flow) or legacy centroid
        if not profile.get("sonic_profile") and not profile.get("centroid"):
            return jsonify({
                "error": "Profile has no sonic profile — link a Spotify playlist first"
            }), 400

        feedback = _db.get_feedback(int(pid))
        result, status = run_pipeline(
            profile, claude_client_ref(), lastfm_key, feedback
        )
        return jsonify(result), status
