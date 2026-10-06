"""Stretch feature checks use a controlled provider; no live Groq calls."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from provenance_guard import create_app, storage
from provenance_guard.disclosure import analyze_disclosure
from provenance_guard.scoring import classify_ensemble


def signal(score):
    return dict(status='ok', score=score, details={'word_count': 60, 'sentence_count': 3})


class EnsembleTests(unittest.TestCase):
    def test_weighted_result_and_neutral_absence(self):
        result = classify_ensemble(signal(.95), signal(.95), analyze_disclosure('ordinary text'))
        self.assertEqual(result['ai_score'], .905)
        self.assertEqual(result['attribution'], 'likely_ai')
        self.assertEqual(sum(result['weights'].values()), 1)
        result = classify_ensemble(signal(.1), signal(.1), analyze_disclosure('ordinary text'))
        self.assertEqual(result['ai_score'], .14)
        self.assertEqual(result['attribution'], 'likely_human')

    def test_positive_disclosure_conflict_is_not_averaged_away(self):
        disclosure = analyze_disclosure('This was WRITTEN BY CHATGPT.')
        self.assertEqual(disclosure['score'], 1)
        result = classify_ensemble(signal(.01), signal(.01), disclosure)
        self.assertEqual(result['attribution'], 'uncertain')
        self.assertEqual(result['uncertainty_reasons'], ['signal_disagreement'])
        self.assertEqual(analyze_disclosure('As an AI language model, I can help.')['score'], 1)

    def test_missing_signal_and_short_input(self):
        for name in range(3):
            values = [signal(.95), signal(.95), analyze_disclosure('text')]
            values[name] = dict(status='unavailable', score=None)
            result = classify_ensemble(*values)
            self.assertIsNone(result['ai_score'])
            self.assertEqual(result['attribution'], 'uncertain')
        style = signal(1)
        style['details']['word_count'] = 49
        self.assertIn('insufficient_text', classify_ensemble(signal(1), style, analyze_disclosure('text'))['uncertainty_reasons'])

    def test_exact_score_and_conflict_boundaries(self):
        for g, s, expected in ((1, 1, 'likely_ai'), (0, 0, 'likely_human'), (.9, .9, 'uncertain')):
            self.assertEqual(classify_ensemble(signal(g), signal(s), analyze_disclosure('text'))['attribution'], expected)
        for style, conflict in ((.6, False), (.599999, True)):
            result = classify_ensemble(signal(1), signal(style), analyze_disclosure('text'))
            self.assertEqual('signal_disagreement' in result['uncertainty_reasons'], conflict)
        # With a neutral third signal these pairs land exactly on .20 and .90.
        self.assertEqual(classify_ensemble(signal(.05), signal(.35), analyze_disclosure('text'))['attribution'], 'likely_human')
        self.assertEqual(classify_ensemble(signal(.96), signal(.92), analyze_disclosure('text'))['ai_score'], .9)
        self.assertEqual(classify_ensemble(signal(.96), signal(.92), analyze_disclosure('text'))['attribution'], 'likely_ai')


class StretchWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'data.sqlite3'
        self.config = dict(TESTING=True, DATABASE_PATH=str(self.path), RATELIMIT_ENABLED=False,
                           REVIEWER_TOKEN='test-only-secret-' * 3, REVIEWER_NAME='Test reviewer')
        self.app = create_app(self.config, signal=lambda text: signal(.95))
        self.client = self.app.test_client()
        self.auth = {'Authorization': 'Bearer ' + self.config['REVIEWER_TOKEN']}
        self.text = 'one two three. ' * 20

    def submit(self, creator='writer'):
        response = self.client.post('/submit', json=dict(text=self.text, creator_id=creator))
        self.assertEqual(response.status_code, 200)
        return response.json

    def request(self, content_id):
        response = self.client.post('/verification/request', json=dict(content_id=content_id, text=self.text,
            draft='My private early draft of this passage.', explanation='I drafted this passage in my notebook and revised the ending.'))
        self.assertEqual(response.status_code, 201)
        return response.json['request_id']

    def approve(self, request_id, client=None):
        return (client or self.client).post('/verification/' + request_id + '/review', headers=self.auth,
                                           json=dict(approve=True, attestation=True))

    def test_certificate_requires_authentication_and_attestation(self):
        decision = self.submit()
        rid = self.request(decision['content_id'])
        url = '/verification/' + rid
        self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(self.client.post(url + '/review', json=dict(approve=True, attestation=True)).status_code, 401)
        self.assertEqual(self.client.post(url + '/review', headers=self.auth, json=dict(approve=True, attestation=False)).status_code, 400)
        self.assertEqual(self.client.get(url, headers=self.auth).json['draft'], 'My private early draft of this passage.')
        self.assertIsNone(self.client.get('/content/' + decision['content_id']).json['certificate'])
        self.app.config['REVIEWER_TOKEN'] = None
        self.assertEqual(self.approve(rid).status_code, 503)

    def test_approval_badge_persists_without_rewriting_decision(self):
        decision = self.submit('<script>alert(1)</script>')
        before = self.client.get('/log').json
        rid = self.request(decision['content_id'])
        approved = self.approve(rid)
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json['certificate']['text_sha256'], hashlib.sha256(self.text.encode()).hexdigest())
        self.assertEqual(self.approve(rid).status_code, 409)
        restarted = create_app(self.config, signal=lambda text: signal(.95)).test_client()
        content = restarted.get('/content/' + decision['content_id']).json
        self.assertEqual(content['certificate']['badge'], 'Verified human')
        self.assertEqual(content['label'], decision['label'])
        self.assertEqual(restarted.get('/log').json, before)
        page = restarted.get('/content/' + decision['content_id'] + '/view').get_data(as_text=True)
        self.assertIn('Verified human', page)
        self.assertIn(decision['label'], page)
        self.assertIn('55%', page)
        self.assertNotIn('<script>alert', page)
        self.assertIn('&lt;script&gt;', page)
        self.assertNotIn('My private early draft', page)
        self.assertNotIn('My private early draft', restarted.get('/dashboard').get_data(as_text=True))

    def test_mismatch_rejection_and_legacy_content(self):
        decision = self.submit()
        body = dict(content_id=decision['content_id'], text=self.text + 'changed', draft='Draft with enough detail.',
                    explanation='This explanation describes the original writing process.')
        self.assertEqual(self.client.post('/verification/request', json=body).status_code, 400)
        rid = self.request(decision['content_id'])
        rejected = self.client.post('/verification/' + rid + '/review', headers=self.auth, json=dict(approve=False, attestation=False))
        self.assertEqual(rejected.json['status'], 'rejected')
        self.assertIsNone(rejected.json['certificate'])
        self.assertEqual(self.approve(rid).status_code, 409)
        # Existing databases and historical records continue to work.
        legacy = dict(decision, content_id='legacy-id')
        legacy.pop('text_sha256')
        storage.save_decision(self.path, legacy)
        body.update(content_id='legacy-id', text=self.text)
        self.assertEqual(self.client.post('/verification/request', json=body).status_code, 409)
        storage.initialize(self.path)
        self.assertEqual(self.client.get('/analytics').json['total'], 2)

    def test_certificate_write_failure_rolls_back_review(self):
        rid = self.request(self.submit()['content_id'])
        with storage.connect(self.path) as db:
            db.execute("CREATE TRIGGER fail_certificate BEFORE INSERT ON certificates BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        self.assertEqual(self.approve(rid).status_code, 503)
        self.assertEqual(self.client.get('/verification/' + rid, headers=self.auth).json['status'], 'pending')
        self.assertEqual(self.client.get('/analytics').json['certified'], 0)

    def test_concurrent_reviews_issue_only_one_certificate(self):
        cid = self.submit()['content_id']
        ids = [self.request(cid), self.request(cid)]
        def approve(rid):
            with self.app.test_client() as client:
                return self.approve(rid, client).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(approve, ids)), [200, 409])
        self.assertEqual(self.client.get('/analytics').json['certified'], 1)

    def test_dashboard_denominators_and_empty_state(self):
        empty = self.client.get('/analytics').json
        self.assertEqual((empty['total'], empty['appeal_rate'], empty['certificate_rate']), (0, 0, 0))
        self.assertIn('No submissions yet', self.client.get('/dashboard').get_data(as_text=True))
        for value in (.1, .6, .95):
            self.app.extensions['groq_signal'] = lambda text, v=value: signal(v)
            with patch('provenance_guard.analyze_stylometry', return_value=signal(value)):
                decision = self.submit()
        self.client.post('/appeal', json=dict(content_id=decision['content_id'], creator_reasoning='Please review.'))
        self.approve(self.request(decision['content_id']))
        data = self.client.get('/analytics').json
        self.assertEqual(data['total'], 3)
        self.assertEqual(data['appeal_rate'], 33.3)
        self.assertEqual(data['certificate_rate'], 33.3)
        self.assertEqual([v['count'] for v in data['verdicts'].values()], [1, 1, 1])
        self.assertEqual(len(self.client.get('/log').json['events']), 4)
        self.assertEqual(data['policies'], {'ensemble-v1': 3})
        page = self.client.get('/dashboard').get_data(as_text=True)
        self.assertIn('Verified human', page)
        self.assertIn('33.3%', page)
        with self.client.get('/static/style.css') as response:
            self.assertEqual(response.status_code, 200)

    def test_invalid_inputs_and_missing_records(self):
        for body in (None, {}, {'content_id': 1, 'text': [], 'draft': 'a', 'explanation': 'b'}):
            self.assertIn(self.client.post('/verification/request', json=body).status_code, (400, 415))
        self.assertEqual(self.client.get('/verification/missing', headers=self.auth).status_code, 404)
        self.assertEqual(self.approve('missing').status_code, 404)
        self.assertEqual(self.client.get('/content/missing/view').status_code, 404)
