"""Show stretch features with synthetic fixtures, without Groq or a real certificate."""
import argparse
import json
from pathlib import Path
import secrets
from tempfile import TemporaryDirectory
from unittest.mock import patch

from provenance_guard import create_app


def build_demo(path):
    app = create_app(dict(DATABASE_PATH=str(path), RATELIMIT_ENABLED=False, DEMO_MODE=True,
                          REVIEWER_TOKEN=secrets.token_urlsafe(32),
                          REVIEWER_NAME='Simulated reviewer — demo only'),
                     signal=lambda text: dict(name='groq', status='ok', score=.95, details={'source': 'controlled demo'}, error_code=None))
    client = app.test_client()
    text = 'one two three. ' * 20
    decisions = []
    # Controlled scores exercise each label; they are not accuracy measurements.
    for value, name in ((.95, 'AI fixture'), (.1, 'Human fixture'), (.6, 'Uncertain fixture')):
        app.extensions['groq_signal'] = lambda text, v=value: dict(name='groq', status='ok', score=v, details={'source': 'controlled demo'}, error_code=None)
        style = dict(name='stylometry', status='ok', score=value,
                     details={'word_count': 60, 'sentence_count': 20, 'source': 'controlled demo'}, error_code=None)
        with patch('provenance_guard.analyze_stylometry', return_value=style):
            response = client.post('/submit', json=dict(creator_id='DEMO ONLY: ' + name, text=text))
        assert response.status_code == 200
        decisions.append(response.json)
    content_id = decisions[1]['content_id']
    pending = client.post('/verification/request', json=dict(content_id=content_id, text=text,
        draft='Synthetic draft for a software demonstration.',
        explanation='This is simulated review evidence, not a real authorship claim.'))
    assert pending.status_code == 201
    certificate = client.post('/verification/' + pending.json['request_id'] + '/review',
        headers={'Authorization': 'Bearer ' + app.config['REVIEWER_TOKEN']},
        json=dict(approve=True, attestation=True))
    assert certificate.status_code == 200
    assert client.post('/appeal', json=dict(content_id=decisions[2]['content_id'], creator_reasoning='Simulated appeal for dashboard testing.')).status_code == 201
    metrics = client.get('/analytics').json
    assert metrics['total'] == 3 and metrics['appeal_rate'] == 33.3 and metrics['certificate_rate'] == 33.3
    assert 'Verified human' in client.get('/content/' + content_id + '/view').get_data(as_text=True)
    assert client.get('/dashboard').status_code == 200
    # Disable additional certificate issuance and live detection after seeding.
    app.config['REVIEWER_TOKEN'] = None
    def unavailable(text):
        return dict(name='groq', status='unavailable', score=None,
                    details={'source': 'Demo has no live provider'}, error_code='demo_only')
    app.extensions['groq_signal'] = unavailable
    return app, dict(mode='Synthetic demo, no live model or real authorship verification',
                     decisions=decisions, certificate=certificate.json['certificate'], analytics=metrics)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Check the workflow and print evidence without starting a server')
    args = parser.parse_args()
    with TemporaryDirectory() as directory:
        app, evidence = build_demo(Path(directory) / 'demo.sqlite3')
        if args.check:
            print(json.dumps(evidence, indent=2))
        else:
            print('Synthetic demo only: http://127.0.0.1:5001/dashboard')
            app.run(host='127.0.0.1', port=5001, use_reloader=False)


if __name__ == '__main__':
    main()
