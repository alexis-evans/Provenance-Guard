"""Content-bound certificates issued after authenticated manual review."""
from datetime import datetime, timezone
import hashlib
import hmac
import json
import sqlite3
from uuid import uuid4

from flask import jsonify, request
from . import storage


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def get_certificate(path, content_id):
    with storage.connect(path) as db:
        row = db.execute('''SELECT p.certificate_id, p.issued_at, p.reviewer, p.text_sha256, c.creator_id
                            FROM certificates p JOIN contents c USING(content_id) WHERE p.content_id = ?''', (content_id,)).fetchone()
    if row is None:
        return None
    return dict(zip(('certificate_id', 'issued_at', 'reviewer', 'text_sha256', 'creator_id'), row),
                content_id=content_id, badge='Verified human',
                basis='Draft and writing process reviewed; creator interviewed',
                scope='This submission only; reviewer attestation, not automated proof')


def register_provenance(app, limiter):
    def failure(code, message, status):
        return jsonify(error=dict(code=code, message=message)), status

    def authorize():
        secret = app.config.get('REVIEWER_TOKEN')
        if not isinstance(secret, str) or len(secret) < 32:
            return failure('review_disabled', 'Configure a reviewer token of at least 32 characters.', 503)
        provided = request.headers.get('Authorization', '')
        if not hmac.compare_digest(provided.encode(), ('Bearer ' + secret).encode()):
            return failure('unauthorized', 'A reviewer bearer token is required.', 401)

    @app.after_request
    def protect_review_cache(response):
        if request.path.startswith('/verification/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.post('/verification/request')
    @limiter.limit('10 per minute;100 per day')
    def request_verification():
        body = request.get_json()
        required = {'content_id', 'text', 'draft', 'explanation'}
        if not isinstance(body, dict) or set(body) != required or any(not isinstance(v, str) for v in body.values()):
            return failure('invalid_request', 'Provide content_id, text, draft, and explanation as strings.', 400)
        if not 20 <= len(body['draft'].strip()) <= 20000 or not 40 <= len(body['explanation'].strip()) <= 5000:
            return failure('invalid_request', 'Provide a draft (20–20000 characters) and explanation (40–5000 characters).', 400)
        with storage.connect(app.config['DATABASE_PATH']) as db:
            row = db.execute('SELECT decision_json FROM contents WHERE content_id = ?', (body['content_id'],)).fetchone()
            if row is None:
                raise storage.ContentNotFound()
            digest = json.loads(row[0]).get('text_sha256')
            if not digest:
                return failure('legacy_content', 'Resubmit this text before requesting verification.', 409)
            if hashlib.sha256(body['text'].encode()).hexdigest() != digest:
                return failure('text_mismatch', 'text must match the original submission exactly.', 400)
            request_id = str(uuid4())
            db.execute('INSERT INTO verification_requests VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)',
                       (request_id, body['content_id'], body['draft'].strip(), body['explanation'].strip(), now(), 'pending'))
        return jsonify(request_id=request_id, status='pending',
                       next_step='Arrange a live authorship review with the reviewer.'), 201

    @app.get('/verification/<request_id>')
    def verification_details(request_id):
        denied = authorize()
        if denied:
            return denied
        with storage.connect(app.config['DATABASE_PATH']) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('''SELECT v.*, c.creator_id, c.decision_json
                                FROM verification_requests v JOIN contents c USING(content_id)
                                WHERE request_id = ?''', (request_id,)).fetchone()
        if row is None:
            return failure('request_not_found', 'No verification request exists with that ID.', 404)
        details = dict(row)
        details['decision'] = json.loads(details.pop('decision_json'))
        return jsonify(details)

    @app.post('/verification/<request_id>/review')
    def review(request_id):
        denied = authorize()
        if denied:
            return denied
        body = request.get_json()
        if (not isinstance(body, dict) or set(body) != {'approve', 'attestation'}
                or type(body['approve']) is not bool or type(body['attestation']) is not bool
                or (body['approve'] and not body['attestation'])):
            return failure('invalid_request', 'Provide boolean approve and attestation; approval requires attestation=true after the live review.', 400)
        path = app.config['DATABASE_PATH']
        with storage.connect(path) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT v.content_id, v.status, c.decision_json
                                FROM verification_requests v JOIN contents c USING(content_id)
                                WHERE request_id = ?''', (request_id,)).fetchone()
            if row is None:
                return failure('request_not_found', 'No verification request exists with that ID.', 404)
            content_id, status, decision_json = row
            if status != 'pending' or db.execute('SELECT 1 FROM certificates WHERE content_id = ?', (content_id,)).fetchone():
                return failure('already_reviewed', 'This request was reviewed or this content already has a certificate.', 409)
            timestamp, reviewer = now(), app.config['REVIEWER_NAME']
            status = 'approved' if body['approve'] else 'rejected'
            db.execute('UPDATE verification_requests SET status = ?, reviewed_at = ?, reviewer = ? WHERE request_id = ?',
                       (status, timestamp, reviewer, request_id))
            if body['approve']:
                db.execute('INSERT INTO certificates VALUES (?, ?, ?, ?, ?, ?)',
                           (str(uuid4()), content_id, request_id, timestamp, reviewer, json.loads(decision_json)['text_sha256']))
        return jsonify(status=status, certificate=get_certificate(path, content_id))
