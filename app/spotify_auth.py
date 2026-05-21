"""
spotify_auth.py — Spotify OAuth 2.0 Authorization Code flow for playlistrec.

Registers these routes on the Flask app via register_spotify_routes():
  GET  /spotify/login      — redirect user to Spotify authorization page
  GET  /spotify/callback   — handle OAuth callback, store tokens
  GET  /spotify/logout     — clear stored tokens
  GET  /spotify/status     — return connection status (for UI polling)

Tokens are stored per-user in the spotify_tokens SQLite table (added to db.py).
For a single-user home app, user_id is fixed to 1. If multi-user support is
needed later, tie user_id to Flask session.

Token refresh is handled transparently by get_spotify_token() — call it
anywhere you need a valid access token.
"""

import os
import time
import secrets
import logging
import requests
from flask import request, jsonify, redirect, session

logger = logging.getLogger(__name__)

# ── Spotify API constants ─────────────────────────────────────────────────────
SPOTIFY_AUTH_URL    = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL   = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE    = "https://api.spotify.com/v1"

SPOTIFY_SCOPES = " ".join([
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-private",
    "playlist-modify-public",
    "user-library-read",
])

# Fixed user ID for single-user deployment
_USER_ID = 1


# ── Token management ──────────────────────────────────────────────────────────

def get_spotify_token():
    """
    Return a valid Spotify access token for the current user.
    Refreshes automatically if the token is expired or within 60s of expiry.
    Returns None if the user has not connected Spotify.
    """
    import db
    token_row = db.get_spotify_token(_USER_ID)
    if not token_row:
        return None

    # Refresh if expired or expiring within 60 seconds
    if token_row["expires_at"] - time.time() < 60:
        refreshed = _refresh_token(token_row["refresh_token"])
        if not refreshed:
            return None
        return refreshed

    return token_row["access_token"]


def _refresh_token(refresh_token):
    """Exchange a refresh token for a new access token. Returns access token or None."""
    client_id     = os.environ.get("SPOTIFY_CLIENT_ID", "")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")

    try:
        resp = requests.post(
            SPOTIFY_TOKEN_URL,
            data={
                "grant_type":    "refresh_token",
                "refresh_token": refresh_token,
            },
            auth=(client_id, client_secret),
            timeout=10,
        )
        data = resp.json()
        if "access_token" not in data:
            logger.error(f"Token refresh failed: {data}")
            return None

        import db
        db.save_spotify_token(
            user_id       = _USER_ID,
            access_token  = data["access_token"],
            refresh_token = data.get("refresh_token", refresh_token),  # may not be returned
            expires_at    = time.time() + data.get("expires_in", 3600),
        )
        logger.info("Spotify token refreshed successfully")
        return data["access_token"]

    except Exception as e:
        logger.error(f"Token refresh error: {e}")
        return None


def spotify_get(path, params=None):
    """
    Make an authenticated GET request to the Spotify API.
    Returns (data_dict, error_string). On success error is None.
    """
    token = get_spotify_token()
    if not token:
        return None, "Spotify not connected"
    try:
        resp = requests.get(
            f"{SPOTIFY_API_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=10,
        )
        if resp.status_code == 401:
            return None, "Spotify token expired — please reconnect"
        resp.raise_for_status()
        return resp.json(), None
    except Exception as e:
        logger.error(f"Spotify GET {path} error: {e}")
        return None, str(e)


def spotify_post(path, json_body):
    """
    Make an authenticated POST request to the Spotify API.
    Returns (data_dict_or_None, error_string).
    """
    token = get_spotify_token()
    if not token:
        return None, "Spotify not connected"
    try:
        resp = requests.post(
            f"{SPOTIFY_API_BASE}{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
            },
            json=json_body,
            timeout=10,
        )
        if resp.status_code == 401:
            return None, "Spotify token expired — please reconnect"
        resp.raise_for_status()
        # 201 No Content is normal for playlist track additions
        return resp.json() if resp.content else {}, None
    except Exception as e:
        logger.error(f"Spotify POST {path} error: {e}")
        return None, str(e)


# ── Route registration ────────────────────────────────────────────────────────

def register_spotify_routes(app):
    """
    Register Spotify OAuth routes on the Flask app.
    Call this from page_handlers.register_routes().
    """
    client_id    = os.environ.get("SPOTIFY_CLIENT_ID", "")
    redirect_uri = os.environ.get("SPOTIFY_REDIRECT_URI", "")

    if not client_id or not redirect_uri:
        logger.warning(
            "SPOTIFY_CLIENT_ID or SPOTIFY_REDIRECT_URI not set — "
            "Spotify routes registered but OAuth will fail"
        )

    @app.route("/spotify/login")
    def spotify_login():
        """Redirect user to Spotify authorization page."""
        state = secrets.token_hex(16)
        session["spotify_oauth_state"] = state

        params = {
            "client_id":     client_id,
            "response_type": "code",
            "redirect_uri":  redirect_uri,
            "scope":         SPOTIFY_SCOPES,
            "state":         state,
            "show_dialog":   "false",
        }
        from urllib.parse import urlencode
        auth_url = f"{SPOTIFY_AUTH_URL}?{urlencode(params)}"
        logger.info("Redirecting to Spotify OAuth")
        return redirect(auth_url)

    @app.route("/spotify/callback")
    def spotify_callback():
        """Handle OAuth callback from Spotify."""
        error = request.args.get("error")
        if error:
            logger.warning(f"Spotify OAuth error: {error}")
            return redirect("/playlist-rec?spotify_error=" + error)

        code  = request.args.get("code", "")
        state = request.args.get("state", "")

        # Validate state to prevent CSRF
        expected_state = session.pop("spotify_oauth_state", None)
        if not expected_state or state != expected_state:
            logger.warning("Spotify OAuth state mismatch")
            return redirect("/playlist-rec?spotify_error=state_mismatch")

        # Exchange code for tokens
        client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
        try:
            resp = requests.post(
                SPOTIFY_TOKEN_URL,
                data={
                    "grant_type":   "authorization_code",
                    "code":         code,
                    "redirect_uri": redirect_uri,
                },
                auth=(client_id, client_secret),
                timeout=10,
            )
            data = resp.json()
            if "access_token" not in data:
                logger.error(f"Token exchange failed: {data}")
                return redirect("/playlist-rec?spotify_error=token_exchange_failed")

            import db
            db.save_spotify_token(
                user_id       = _USER_ID,
                access_token  = data["access_token"],
                refresh_token = data["refresh_token"],
                expires_at    = time.time() + data.get("expires_in", 3600),
            )
            logger.info("Spotify connected successfully")
            return redirect("/playlist-rec?spotify_connected=1")

        except Exception as e:
            logger.error(f"Spotify callback error: {e}")
            return redirect("/playlist-rec?spotify_error=callback_error")

    @app.route("/spotify/logout")
    def spotify_logout():
        """Clear stored Spotify tokens."""
        import db
        db.delete_spotify_token(_USER_ID)
        logger.info("Spotify disconnected")
        return redirect("/playlist-rec?spotify_disconnected=1")

    @app.route("/spotify/status")
    def spotify_status():
        """Return Spotify connection status. Used by UI on page load."""
        token = get_spotify_token()
        if not token:
            return jsonify({"connected": False})

        # Fetch display name to confirm token is live
        data, err = spotify_get("/me")
        if err:
            return jsonify({"connected": False, "error": err})
        return jsonify({
            "connected":    True,
            "display_name": data.get("display_name", ""),
            "spotify_id":   data.get("id", ""),
        })

    logger.info("Spotify OAuth routes registered: /spotify/login, /spotify/callback, /spotify/logout, /spotify/status")
