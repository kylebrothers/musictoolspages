"""
page_handlers.py — App-specific handlers for playlistrec.

This file adds custom API routes via register_routes() and does NOT override
the template's handle_claude_call_page / handle_no_call_page — those are
used as-is from the template for any generic page submissions.

Custom endpoints registered by register_routes():
  GET    /api/profiles              — list profiles
  POST   /api/profiles              — create profile
  GET    /api/profiles/<id>         — get profile
  PUT    /api/profiles/<id>         — update profile (name, seed_artists, disliked_artists)
  DELETE /api/profiles/<id>         — delete profile
  POST   /api/profiles/<id>/csv     — upload CSV, compute centroid
  GET    /api/profiles/<id>/feedback — get all feedback + notes
  POST   /api/profiles/<id>/feedback — upsert rating + optional maybe note
  DELETE /api/profiles/<id>/feedback — clear all feedback
  POST   /api/playlist-rec/run      — full two-pass recommendation pipeline
"""

import io
import os
import csv
import json
import logging
import requests
from flask import request, jsonify, current_app

import db

logger = logging.getLogger(__name__)

# ── Audio feature columns used for centroid computation ───────────────────────
CENTROID_FIELDS = [
    "Energy", "Danceability", "Valence", "Acousticness",
    "Instrumentalness", "Loudness", "Tempo", "Speechiness",
]

# Column index map (0-based) matching the Spotify CSV export format
_COL = {
    "Track Name": 1,
    "Artist Name(s)": 3,
    "Danceability": 12,
    "Energy": 13,
    "Loudness": 15,
    "Speechiness": 17,
    "Acousticness": 18,
    "Instrumentalness": 19,
    "Valence": 21,
    "Tempo": 22,
}

# ── Claude model ──────────────────────────────────────────────────────────────
# If CLAUDE_MODEL is set in the environment, use it directly.
# Otherwise, query the Anthropic models API at startup and pick the latest
# Sonnet. Falls back to a hardcoded value if the API call fails.

_CLAUDE_MODEL_FALLBACK = "claude-sonnet-4-5"
_resolved_model = None


def get_claude_model(api_key=None):
    """
    Return the Claude model to use for all pipeline calls.
    Resolution order:
      1. CLAUDE_MODEL env var (allows pinning via .env)
      2. Latest Sonnet from Anthropic models API
      3. Hardcoded fallback
    Result is cached after first call.
    """
    global _resolved_model
    if _resolved_model:
        return _resolved_model

    env_model = os.environ.get("CLAUDE_MODEL", "").strip()
    if env_model:
        logger.info(f"Claude model: {env_model} (from CLAUDE_MODEL env var)")
        _resolved_model = env_model
        return _resolved_model

    key = api_key or os.environ.get("CLAUDE_API_KEY", "")
    if key:
        try:
            resp = requests.get(
                "https://api.anthropic.com/v1/models",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                },
                timeout=5,
            )
            models = resp.json().get("data", [])
            sonnets = sorted(
                [m["id"] for m in models if "sonnet" in m["id"].lower()],
                reverse=True,
            )
            if sonnets:
                _resolved_model = sonnets[0]
                logger.info(f"Claude model: {_resolved_model} (latest Sonnet from API)")
                return _resolved_model
        except Exception as e:
            logger.warning(f"Could not resolve latest Sonnet from API: {e}")

    logger.warning(f"Claude model: {_CLAUDE_MODEL_FALLBACK} (fallback)")
    _resolved_model = _CLAUDE_MODEL_FALLBACK
    return _resolved_model

# ── Last.fm ───────────────────────────────────────────────────────────────────
LASTFM_ENDPOINT = "https://ws.audioscrobbler.com/2.0/"
LASTFM_SEED_ARTISTS = ["Weezer", "Barenaked Ladies", "Fountains of Wayne"]
LASTFM_SIMILAR_COUNT = 15


# handle_claude_call_page and handle_no_call_page are intentionally NOT defined
# here. app.py imports them from the template's page_handlers.py, which provides
# the full generic implementations. This app uses register_routes() exclusively
# for its own endpoints and does not need to override the generic handlers.


# ── CSV processing ────────────────────────────────────────────────────────────

def parse_spotify_csv(file_bytes):
    """
    Parse a Spotify export CSV (bytes).
    Returns (centroid dict, top_artists list, track_count int).
    """
    text = file_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)

    if not rows:
        raise ValueError("Empty CSV file.")

    # Detect header row and build column map
    header = [h.strip() for h in rows[0]]
    col = {name: header.index(name) for name in _COL if name in header}

    missing = [f for f in list(_COL.keys()) if f not in col]
    if missing:
        raise ValueError(f"CSV missing columns: {', '.join(missing)}")

    artist_counts = {}
    feature_sums = {f: 0.0 for f in CENTROID_FIELDS}
    valid = 0

    for row in rows[1:]:
        if len(row) < max(col.values()) + 1:
            continue
        try:
            for field in CENTROID_FIELDS:
                feature_sums[field] += float(row[col[field]])
            valid += 1
        except (ValueError, IndexError):
            continue

        # Count artists (may be semicolon-separated)
        for artist in row[col["Artist Name(s)"]].split(";"):
            artist = artist.strip()
            if artist:
                artist_counts[artist] = artist_counts.get(artist, 0) + 1

    if valid == 0:
        raise ValueError("No valid audio feature rows found.")

    centroid = {f: round(feature_sums[f] / valid, 4) for f in CENTROID_FIELDS}

    top_artists = sorted(artist_counts, key=lambda a: -artist_counts[a])[:15]

    return centroid, top_artists, valid


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
                    "method": "artist.getsimilar",
                    "artist": artist,
                    "api_key": api_key,
                    "format": "json",
                    "limit": count,
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

def _build_pass1_prompt(profile, similar_artists, feedback):
    centroid = profile.get("centroid") or {}
    seed_artists = profile.get("seed_artists") or []
    disliked = profile.get("disliked_artists") or []

    yes_tracks = [k for k, v in feedback.items() if v == "yes"]
    maybe_tracks = [k for k, v in feedback.items() if v == "maybe"]
    no_tracks = [k for k, v in feedback.items() if v == "no"]

    lines = [
        "You are generating a raw candidate list of song recommendations for human validation.",
        "This is Pass 1 — do NOT self-correct, retract, or second-guess any suggestion.",
        "Output ONLY the structured format below. No preamble. No explanation. No self-correction.",
        "",
        "## Listener Profile",
        f"Seed artists: {', '.join(seed_artists) if seed_artists else 'None'}",
        f"Disliked artists (exclude these entirely): {', '.join(disliked) if disliked else 'None'}",
        "",
        "## Audio Centroid (Spotify feature averages)",
    ]
    for k, v in centroid.items():
        lines.append(f"  {k}: {v}")

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

    lines += [
        "",
        "## Output Format (20 candidates, one per line)",
        '"Song Title" – Artist (Year) | energy:[0-1] danceability:[0-1] valence:[0-1] acousticness:[0-1] tempo:[BPM] | solo:[yes/no/notable] | [one sentence on fit]',
        "",
        "Generate 20 candidates now:",
    ]

    return "\n".join(lines)


def _build_pass2_prompt(profile, candidates_text, feedback):
    disliked = profile.get("disliked_artists") or []
    no_tracks = [k for k, v in feedback.items() if v == "no"]

    lines = [
        "You are validating and filtering a candidate song list. Apply the rules strictly.",
        "Output ONLY the structured format below. No preamble. No explanation. No self-correction.",
        "The final output MUST begin with the word RECOMMENDATIONS on its own line.",
        "",
        "## Hard Rejection Rules",
        "Reject a candidate if ANY of the following are true:",
        "  - Energy > 0.94",
        "  - Acousticness > 0.28",
        "  - Tempo outside 92–172 BPM",
        "  - Valence < 0.18",
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
    Returns dict: {recommendations: [...], rabbit_holes: [...], pass1_raw: str}
    """
    if not claude_client:
        return {"error": "Claude API not available"}, 503

    # Last.fm similar artists
    similar = fetch_lastfm_similar(lastfm_api_key, profile.get("seed_artists"))

    # Resolve model once per pipeline run
    model = get_claude_model()

    # Pass 1
    p1_prompt = _build_pass1_prompt(profile, similar, feedback)
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
    rabbit_holes = []
    section = None

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
        "recommendations": recommendations,
        "rabbit_holes": rabbit_holes,
        "pass1_raw": candidates_text,
        "lastfm_similar_count": len(similar),
    }, 200


# ── Custom routes — registered via app.py's before_first_request or directly ──
# These are imported in a register_routes() call from app.py if it exists,
# or wired via a Flask Blueprint. For simplicity under the template pattern,
# we expose a register() function that app.py can call after creating the app.

def register_routes(app, claude_client_ref):
    """
    Register playlistrec-specific API routes on the Flask app.
    Call this from app.py after create_app().

    Usage in app.py:
        from page_handlers import register_routes
        register_routes(app, lambda: claude_client)
    """
    import db as _db
    _db.init_db()  # idempotent — safe to call here; creates tables if not present

    # Resolve and log the Claude model at startup rather than on first request
    get_claude_model()

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

    # ── CSV upload → centroid ─────────────────────────────────────────────────

    @app.route("/api/profiles/<int:pid>/csv", methods=["POST"])
    def upload_csv(pid):
        profile = _db.get_profile(pid)
        if not profile:
            return jsonify({"error": "Profile not found"}), 404
        if "csv_file" not in request.files:
            return jsonify({"error": "No csv_file in request"}), 400

        f = request.files["csv_file"]
        try:
            centroid, top_artists, track_count = parse_spotify_csv(f.read())
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        _db.update_profile(pid, centroid=centroid)

        return jsonify({
            "centroid": centroid,
            "top_artists": top_artists,
            "track_count": track_count,
            "message": f"Centroid computed from {track_count} tracks.",
        })

    # ── Feedback ──────────────────────────────────────────────────────────────

    @app.route("/api/profiles/<int:pid>/feedback", methods=["GET"])
    def get_feedback(pid):
        if not _db.get_profile(pid):
            return jsonify({"error": "Not found"}), 404
        ratings = _db.get_feedback(pid)
        notes = _db.get_maybe_notes(pid)
        return jsonify({"feedback": ratings, "notes": notes})

    @app.route("/api/profiles/<int:pid>/feedback", methods=["POST"])
    def set_feedback(pid):
        if not _db.get_profile(pid):
            return jsonify({"error": "Not found"}), 404
        data = request.get_json(force=True, silent=True) or {}
        track_key = (data.get("track_key") or "").strip()
        rating = data.get("rating", "")
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

    # ── Recommendation pipeline ───────────────────────────────────────────────

    @app.route("/api/playlist-rec/run", methods=["POST"])
    def run_recommendations():
        data = request.get_json(force=True, silent=True) or {}
        pid = data.get("profile_id")
        if not pid:
            return jsonify({"error": "profile_id required"}), 400
        profile = _db.get_profile(int(pid))
        if not profile:
            return jsonify({"error": "Profile not found"}), 404
        if not profile.get("centroid"):
            return jsonify({"error": "Profile has no centroid — upload a CSV first"}), 400

        feedback = _db.get_feedback(int(pid))
        result, status = run_pipeline(
            profile, claude_client_ref(), lastfm_key, feedback
        )
        return jsonify(result), status
