"""Small read-only dashboard backed by saved submissions, not audit row counts."""
import json
from flask import jsonify, render_template
from . import storage


def analytics(path):
    with storage.connect(path) as db:
        db.execute('BEGIN')
        total = db.execute('SELECT COUNT(*) FROM contents').fetchone()[0]
        counts = dict(db.execute("SELECT json_extract(decision_json, '$.attribution'), COUNT(*) FROM contents GROUP BY 1"))
        policies = dict(db.execute("SELECT COALESCE(json_extract(decision_json, '$.policy_version'), 'legacy'), COUNT(*) FROM contents GROUP BY 1"))
        appealed = db.execute('SELECT COUNT(*) FROM appeals').fetchone()[0]
        certified = db.execute('SELECT COUNT(*) FROM certificates').fetchone()[0]
        rows = db.execute('''SELECT c.decision_json, c.status, p.certificate_id
                             FROM contents c LEFT JOIN certificates p USING(content_id)
                             ORDER BY c.created_at DESC, c.content_id DESC LIMIT 20''').fetchall()
    return {
        'total': total,
        'verdicts': {name: {'count': counts.get(name, 0), 'percent': round(100 * counts.get(name, 0) / total, 1) if total else 0}
                     for name in ('likely_ai', 'likely_human', 'uncertain')},
        'appealed': appealed, 'appeal_rate': round(100 * appealed / total, 1) if total else 0,
        'certified': certified, 'certificate_rate': round(100 * certified / total, 1) if total else 0,
        'policies': policies,
        'recent': [dict(json.loads(payload), status=status, certificate_id=certificate)
                   for payload, status, certificate in rows],
    }


def register_dashboard(app):
    @app.get('/analytics')
    def analytics_json():
        result = analytics(app.config['DATABASE_PATH'])
        result.pop('recent')
        return jsonify(result)

    @app.get('/dashboard')
    def dashboard():
        return render_template('dashboard.html', data=analytics(app.config['DATABASE_PATH']))

    @app.get('/content/<content_id>/view')
    def content_view(content_id):
        return render_template('content.html', content=storage.get_content(app.config['DATABASE_PATH'], content_id))
