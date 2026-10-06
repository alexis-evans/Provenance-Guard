"""Flask application factory for the ensemble attribution pipeline."""

from datetime import datetime, timezone
import os
import hashlib
from pathlib import Path
import re
import sqlite3
from uuid import UUID, uuid4

from dotenv import dotenv_values
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from groq import Groq
from werkzeug.exceptions import BadRequest, HTTPException

from .signals import GroqSignal
from .stylometry import analyze_stylometry
from .scoring import classify_ensemble
from .disclosure import analyze_disclosure
from . import storage


ROOT = Path(__file__).resolve().parent.parent
SUBMISSION_LIMITS = "10 per minute;100 per day"


def create_app(config=None, *, signal=None):
    app = Flask(__name__, instance_path=str(ROOT / "instance"))
    # Environment overrides .env without copying secrets into os.environ.
    settings = {**dotenv_values(ROOT / ".env"), **os.environ}
    app.config.from_mapping(
        GROQ_API_KEY=settings.get("GROQ_API_KEY"),
        GROQ_MODEL=settings.get("GROQ_MODEL"),
        GROQ_REASONING_EFFORT=settings.get("GROQ_REASONING_EFFORT") or None,
        DATABASE_PATH=settings.get("DATABASE_PATH") or str(ROOT / "instance/provenance_guard.sqlite3"),
        MAX_CONTENT_LENGTH=131_072,
        RATELIMIT_ENABLED=True,
        REVIEWER_TOKEN=settings.get("REVIEWER_TOKEN"),
        REVIEWER_NAME=settings.get("REVIEWER_NAME") or "Local reviewer",
    )
    if config:
        app.config.update(config)
    if signal is None:
        for key in ("GROQ_API_KEY", "GROQ_MODEL"):
            value = app.config.get(key)
            if not isinstance(value, str) or not value.strip():
                raise RuntimeError(f"Set {key} in .env or the environment before starting.")
        client = Groq(api_key=app.config["GROQ_API_KEY"], timeout=15.0, max_retries=0)
        signal = GroqSignal(
            client, app.config["GROQ_MODEL"], app.config["GROQ_REASONING_EFFORT"]
        )
        app.extensions["groq_client"] = client
    app.extensions["groq_signal"] = signal
    path = Path(app.config["DATABASE_PATH"]).expanduser().resolve()
    app.config["DATABASE_PATH"] = str(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        storage.initialize(path)
    except (OSError, sqlite3.Error):
        raise RuntimeError("Cannot initialize DATABASE_PATH; check its location and permissions.") from None

    limiter = Limiter(
        get_remote_address, app=app, default_limits=[], storage_uri="memory://",
        strategy="fixed-window", headers_enabled=True, retry_after="delta-seconds",
    )
    app.extensions["submission_limiter"] = limiter

    def error(code, message, status):
        return jsonify(error={"code": code, "message": message}), status

    @app.errorhandler(HTTPException)
    def http_error(exc):
        mapping = {
            400: ("invalid_request", "Provide a valid JSON request."),
            413: ("payload_too_large", "The request body must not exceed 131072 bytes."),
            415: ("unsupported_media_type", "Use Content-Type: application/json."),
            429: ("rate_limited", "Submission limit exceeded. Retry after the time in Retry-After."),
        }
        code, message = mapping.get(exc.code, ("http_error", exc.name))
        return error(code, message, exc.code)

    @app.errorhandler(sqlite3.Error)
    def storage_error(exc):
        return error("storage_unavailable", "Storage is unavailable; no success was recorded.", 503)

    @app.errorhandler(storage.ContentNotFound)
    def missing_content(exc):
        return error("content_not_found", "No submission exists with that content_id.", 404)

    @app.errorhandler(storage.AppealExists)
    def duplicate_appeal(exc):
        return error("appeal_exists", "This submission already has an open appeal.", 409)

    @app.get("/")
    def index():
        return jsonify(
            service="Provenance Guard",
            message="The API is running. Submit text with POST /submit or view decisions with GET /log.",
            endpoints={
                "submit": {"method": "POST", "path": "/submit",
                           "json_body": {"text": "Your text here", "creator_id": "demo-writer"}},
                "log": {"method": "GET", "path": "/log"},
                "dashboard": {"method": "GET", "path": "/dashboard"},
                "analytics": {"method": "GET", "path": "/analytics"},
                "verification": {"method": "POST", "path": "/verification/request"},
                "content": {"method": "GET", "path": "/content/<content_id>"},
                "appeal": {"method": "POST", "path": "/appeal",
                           "json_body": {"content_id": "UUID from /submit", "creator_reasoning": "Why you contest the assessment"}},
            },
        )

    @app.post("/submit")
    @limiter.limit(SUBMISSION_LIMITS)
    def submit():
        body = request.get_json()
        if not isinstance(body, dict) or set(body) != {"text", "creator_id"}:
            return error("invalid_request", "Provide exactly text and creator_id.", 400)
        text, creator_id = body["text"], body["creator_id"]
        if not isinstance(text, str) or not text.strip() or len(text) > 20_000:
            return error("invalid_request", "text must be nonblank and at most 20000 characters.", 400)
        if not isinstance(creator_id, str) or not creator_id.strip() or len(creator_id.strip()) > 100:
            return error("invalid_request", "creator_id must be nonblank and at most 100 characters.", 400)

        # No provider call happens inside the SQLite write transaction.
        groq_signal = app.extensions["groq_signal"](text)
        stylometry_signal = analyze_stylometry(text)
        disclosure_signal = analyze_disclosure(text)
        decision = {
            "content_id": str(uuid4()),
            "creator_id": creator_id.strip(),
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            **classify_ensemble(groq_signal, stylometry_signal, disclosure_signal),
            "status": "classified",
            "signals": {
                "groq": groq_signal,
                "stylometry": stylometry_signal,
                "disclosure": disclosure_signal,
            },
        }
        storage.save_decision(app.config["DATABASE_PATH"], decision)
        return jsonify(decision)

    @app.get("/content/<content_id>")
    def content(content_id):
        try:
            canonical_id = str(UUID(content_id))
        except ValueError:
            raise storage.ContentNotFound() from None
        return jsonify(storage.get_content(app.config["DATABASE_PATH"], canonical_id))

    @app.post("/appeal")
    def appeal():
        body = request.get_json()
        if not isinstance(body, dict) or set(body) != {"content_id", "creator_reasoning"}:
            return error("invalid_request", "Provide exactly content_id and creator_reasoning.", 400)
        content_id, reasoning = body["content_id"], body["creator_reasoning"]
        if not isinstance(content_id, str):
            return error("invalid_request", "content_id must be a valid UUID string.", 400)
        try:
            canonical_id = str(UUID(content_id))
        except ValueError:
            return error("invalid_request", "content_id must be a valid UUID string.", 400)
        if not isinstance(reasoning, str) or not reasoning.strip() or len(reasoning.strip()) > 5_000:
            return error("invalid_request", "creator_reasoning must be nonblank and at most 5000 characters.", 400)
        return jsonify(storage.create_appeal(app.config["DATABASE_PATH"], canonical_id, reasoning.strip())), 201

    @app.get("/log")
    def log():
        if set(request.args) - {"limit", "offset"}:
            raise BadRequest()
        values = {}
        for name, default in (("limit", "20"), ("offset", "0")):
            raw = request.args.get(name, default)
            # SQLite binds signed 64-bit integers. Bound before int conversion.
            if len(request.args.getlist(name)) > 1 or not re.fullmatch(r"[0-9]{1,19}", raw):
                raise BadRequest()
            values[name] = int(raw)
        if not 1 <= values["limit"] <= 100 or not 0 <= values["offset"] <= 2**63 - 1:
            raise BadRequest()
        events = storage.read_events(app.config["DATABASE_PATH"], **values)
        return jsonify(events=events, **values)

    from .provenance import register_provenance
    register_provenance(app, limiter)
    from .dashboard import register_dashboard
    register_dashboard(app)
    return app
