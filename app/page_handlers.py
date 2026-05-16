"""
app/page_handlers.py — App-specific page handler overrides.

This file overrides the template's page_handlers.py. You have three options:

─────────────────────────────────────────────────────────────────────────────
OPTION A — Full override (replace both standard handlers):
    Implement handle_claude_call_page and handle_no_call_page from scratch.
    Use this when your app needs fundamentally different logic for the
    generic /api/<page_name> route.
─────────────────────────────────────────────────────────────────────────────
OPTION B — Partial override (extend one or both standard handlers):
    Import the base handlers and wrap them with pre/post logic.

    Example:
        from page_handlers import (
            handle_claude_call_page as _base_claude,
            handle_no_call_page as _base_no_call,
        )

        def handle_claude_call_page(page_name, form_data, uploaded_files_data,
                                     server_files_data, session_id, claude_client):
            form_data['injected_context'] = 'some extra context'
            return _base_claude(page_name, form_data, uploaded_files_data,
                                server_files_data, session_id, claude_client)
─────────────────────────────────────────────────────────────────────────────
OPTION C — Custom page type:
    Define handle_custom_page() to handle forms that set page_type="custom".
    Use this when a page needs server-side logic that doesn't fit the
    claude-call / no-call pattern but still submits via the generic
    /api/<page_name> route.

    Signature:
        def handle_custom_page(page_name, form_data, uploaded_files_data,
                               server_files_data, session_id, claude_client):
            ...
            return jsonify({...})
─────────────────────────────────────────────────────────────────────────────
OPTION D — Custom API endpoints (recommended for complex apps):
    Define register_routes(app, claude_client_ref) to add your own routes
    directly on the Flask app. Called automatically by app.py at startup.
    Use this when your app needs endpoints that don't fit the generic
    /api/<page_name> pattern — e.g. REST-style resource routes, file upload
    endpoints, or a multi-step pipeline.

    Example:
        def register_routes(app, claude_client_ref):
            @app.route('/api/myresource', methods=['GET'])
            def list_resources():
                return jsonify({'items': [...]})

            @app.route('/api/myapp/run', methods=['POST'])
            def run_pipeline():
                client = claude_client_ref()
                ...
─────────────────────────────────────────────────────────────────────────────

Delete this file entirely if you don't need any custom handler logic —
the template's page_handlers.py will be used as-is.
"""

# PLACEHOLDER: Implement the option(s) you need, then remove unused stubs.

# ── Option B example (uncomment to use) ──────────────────────────────────────
# from page_handlers import handle_claude_call_page as _base_claude  # noqa
# from page_handlers import handle_no_call_page as _base_no_call      # noqa
#
# def handle_claude_call_page(page_name, form_data, uploaded_files_data,
#                              server_files_data, session_id, claude_client):
#     form_data['injected_context'] = 'extra context here'
#     return _base_claude(page_name, form_data, uploaded_files_data,
#                         server_files_data, session_id, claude_client)


# ── Option C example (uncomment to use) ──────────────────────────────────────
# from flask import jsonify
#
# def handle_custom_page(page_name, form_data, uploaded_files_data,
#                        server_files_data, session_id, claude_client):
#     # Your logic here
#     return jsonify({'result': '...'})


# ── Option D example (uncomment to use) ──────────────────────────────────────
# from flask import request, jsonify
#
# def register_routes(app, claude_client_ref):
#     @app.route('/api/myresource', methods=['GET'])
#     def list_resources():
#         return jsonify({'items': []})
