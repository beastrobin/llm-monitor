import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import monitor
from collectors.hermes_session import latest_desktop_selection


class HermesSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.config = self.home / 'config.yaml'
        self.config.write_text('model:\n  base_url: http://127.0.0.1:8080/v1\n')
        self.db = sqlite3.connect(self.home / 'state.db')
        self.addCleanup(self.db.close)
        self.db.execute('''CREATE TABLE sessions (
            id INTEGER PRIMARY KEY, source TEXT, started_at REAL,
            last_activity_at REAL, ended_at REAL, archived INTEGER DEFAULT 0,
            hidden INTEGER DEFAULT 0, model TEXT, model_config TEXT,
            billing_base_url TEXT)''')

    def session(self, runtime, activity=10, source='desktop', **fields):
        values = dict(source=source, started_at=activity, last_activity_at=activity,
                      model=runtime.get('model'), model_config=json.dumps(runtime), **fields)
        self.db.execute('INSERT INTO sessions (%s) VALUES (%s)' % (
            ','.join(values), ','.join('?' for _ in values)), list(values.values()))
        self.db.commit()

    def resolve(self):
        with patch.object(monitor, 'hermes_config_candidates', return_value=[self.config]):
            return monitor.resolve_hermes_router('http://127.0.0.1:9999')

    def test_session_switch_overrides_unchanged_default(self):
        self.session({'provider': 'loader', 'base_url': 'http://127.0.0.1:8080/v1'})
        self.assertEqual(self.resolve()[0], 'http://127.0.0.1:8080')
        self.db.execute('UPDATE sessions SET model_config=?', (json.dumps({
            'provider': 'llamacpp', 'base_url': 'http://127.0.0.1:18434/v1'}),))
        self.db.commit()
        url, reason = self.resolve()
        self.assertEqual(url, 'http://127.0.0.1:18434')
        self.assertIn('state.db', reason)
        self.assertIn(':8080/', self.config.read_text())

    def test_latest_activity_ignores_background_and_archived_sessions(self):
        self.session({'base_url': 'http://localhost:8080/v1'}, activity=10)
        self.session({'base_url': 'http://localhost:18434/v1'}, activity=20)
        for excluded in ({'source': 'oneshot'}, {'archived': 1},
                         {'hidden': 1}, {'ended_at': 30}):
            self.session({'base_url': 'http://localhost:9999/v1'}, activity=30, **excluded)
        self.assertEqual(latest_desktop_selection(self.home)[0], 'http://localhost:18434/v1')

    def test_nested_runtime_and_billing_fallback(self):
        self.session({}, billing_base_url='http://localhost:8200/v1')
        self.assertEqual(latest_desktop_selection(self.home)[0], 'http://localhost:8200/v1')
        self.session({'base_url': 'http://localhost:8080/v1', 'gateway_runtime': {
            'provider': 'llamacpp', 'base_url': 'http://localhost:18434/v1'}}, activity=20)
        self.assertEqual(latest_desktop_selection(self.home)[0], 'http://localhost:18434/v1')

    def test_missing_route_does_not_reuse_previous_provider_or_older_session(self):
        self.session({'base_url': 'http://localhost:18434/v1'})
        self.session({'provider': 'new-provider'}, activity=20,
                     billing_base_url='http://localhost:18434/v1')
        self.assertEqual(self.resolve()[0], 'http://127.0.0.1:8080')

    def test_session_without_url_follows_matching_loaded_router(self):
        self.session({'provider': 'llamacpp', 'model': 'Qwen3.8'}, activity=20)
        reply = {'data': [{'id': 'Qwen3.8', 'status': {'value': 'loaded'}},
                          {'id': 'Qwen3.6', 'status': {'value': 'unloaded'}}]}
        with patch.object(monitor, 'hermes_config_candidates', return_value=[self.config]), \
                patch.object(monitor, '_get_json', return_value=reply) as get_json:
            url, reason = monitor.resolve_hermes_router(
                'http://127.0.0.1:18434', api_key='test-key')
        self.assertEqual(url, 'http://127.0.0.1:18434')
        self.assertIn('loaded session model', reason)
        get_json.assert_called_once_with('http://127.0.0.1:18434/v1/models',
                                         'test-key', timeout=0.5)

    def test_session_without_url_rejects_unrelated_router(self):
        self.session({'provider': 'llamacpp', 'model': 'Qwen3.8'}, activity=20)
        with patch.object(monitor, 'hermes_config_candidates', return_value=[self.config]), \
                patch.object(monitor, '_get_json', return_value={'data': [
                    {'id': 'Qwen3.6', 'status': {'value': 'loaded'}}]}):
            self.assertEqual(monitor.resolve_hermes_router(
                'http://127.0.0.1:18434')[0], 'http://127.0.0.1:8080')

    def test_session_without_url_ignores_other_provider_even_if_name_matches(self):
        self.session({'provider': 'strata', 'model': 'Qwen3.8'}, activity=20)
        with patch.object(monitor, 'hermes_config_candidates', return_value=[self.config]), \
                patch.object(monitor, '_get_json') as get_json:
            self.assertEqual(monitor.resolve_hermes_router(
                'http://127.0.0.1:18434')[0], 'http://127.0.0.1:8080')
        get_json.assert_not_called()

    def test_remote_session_url_cannot_be_replaced_by_matching_local_model(self):
        self.session({'provider': 'llamacpp', 'model': 'Qwen3.8',
                      'base_url': 'https://remote.example/v1'}, activity=20)
        with patch.object(monitor, 'hermes_config_candidates', return_value=[self.config]), \
                patch.object(monitor, '_get_json') as get_json:
            self.assertEqual(monitor.resolve_hermes_router(
                'http://127.0.0.1:18434')[0], 'http://127.0.0.1:8080')
        get_json.assert_not_called()

    def test_invalid_or_remote_routes_fall_back(self):
        for url in ('https://example.com/v1', 'http://localhost:bad/v1',
                    'http://user:secret@localhost:8080/v1',
                    'http://localhost:8080/wrong-path', 42):
            with self.subTest(url=url):
                self.db.execute('DELETE FROM sessions')
                self.session({'base_url': url})
                self.assertEqual(self.resolve()[0], 'http://127.0.0.1:8080')
        self.db.execute("UPDATE sessions SET model_config='invalid json'")
        self.db.commit()
        self.assertEqual(self.resolve()[0], 'http://127.0.0.1:8080')
        absent = self.home / 'missing'
        self.assertIsNone(latest_desktop_selection(absent)[0])
        self.assertFalse(absent.exists())

    def test_hub_follows_switch_and_marks_deduplicated_current_source(self):
        self.session({'base_url': 'http://localhost:8080/v1'})
        config = copy.deepcopy(monitor.DEFAULT_CONFIG)
        config['live']['enabled'] = False
        config['stats']['enabled'] = False
        config['gpu']['enabled'] = False
        config['system']['enabled'] = False
        config['endpoints'] += [
            {'id': 'loader', 'base_url': 'http://127.0.0.1:8080'},
            {'id': 'strata', 'base_url': 'http://127.0.0.1:8200'},
        ]
        with patch.object(monitor, 'hermes_config_candidates', return_value=[self.config]):
            hub = monitor.Hub(config)
            self.addCleanup(hub._pool.shutdown)
            self.assertEqual([ep.id for ep in hub._active_endpoints()], ['loader', 'strata'])
            self.assertEqual(hub.router_covered_by, 'loader')
            self.session({'base_url': 'http://localhost:8200/v1'}, activity=20)
            hub._refresh_router(force=True)
            for ep in hub.endpoints:
                ep.poll = lambda ep=ep: dict(id=ep.id, base_url=ep.base_url, up=True, models=[])
            hub.sample()
            self.assertEqual(hub.snapshot()['current_endpoint_id'], 'strata')
            self.session({'base_url': 'http://localhost:18434/v1'}, activity=30)
            hub._refresh_router(force=True)
            hub.sample()
            self.assertEqual(hub.snapshot()['current_endpoint_id'], 'hermes')
            self.assertEqual(hub.hermes_endpoint.base_url, 'http://localhost:18434')

    def test_a_card_covering_an_app_address_covers_it_without_polling_it(self):
        """Hermes names the Strata app; the Strata card polls its engine.

        The address on the card and the address being asked are then different
        things, and dedup has to go by the address the card *represents*.
        Comparing polled addresses alone would show one machine twice and book
        its tokens twice in the ledger.
        """
        self.session({'base_url': 'http://localhost:47831/v1'})
        config = copy.deepcopy(monitor.DEFAULT_CONFIG)
        config['live']['enabled'] = False
        config['stats']['enabled'] = False
        config['gpu']['enabled'] = False
        config['system']['enabled'] = False
        config['endpoints'] += [
            {'id': 'strata', 'kind': 'strata', 'base_url': 'http://127.0.0.1:47831'},
        ]
        with patch.object(monitor, 'hermes_config_candidates', return_value=[self.config]):
            hub = monitor.Hub(config)
            self.addCleanup(hub._pool.shutdown)
            strata = next(ep for ep in hub.endpoints if ep.id == 'strata')
            self.assertEqual([ep.id for ep in hub._active_endpoints()], ['strata'])
            # Now the card follows the engine behind that address: the address
            # it asks and the address Hermes names no longer match, and without
            # the address it represents the hermes card would come back -- one
            # machine on screen twice, and its tokens booked twice.
            strata.base_url = 'http://127.0.0.1:18500'
            strata.proxied_url = 'http://127.0.0.1:47831'
            self.assertEqual([ep.id for ep in hub._active_endpoints()], ['strata'])
            self.assertEqual(hub.router_covered_by, 'strata')
            # A different model behind a different app, and the hermes card has
            # nothing left to yield to: two servers, two cards.
            self.session({'base_url': 'http://localhost:18434/v1'}, activity=30)
            hub._refresh_router(force=True)
            self.assertEqual([ep.id for ep in hub._active_endpoints()],
                             ['hermes', 'strata'])
            self.assertEqual(hub.router_covered_by, '')

    def test_the_live_card_survives_the_router_card_being_covered(self):
        """Dedup silences the polling card, never the log card.

        The live card answers about itself -- whichever instance writes the
        log, and the child servers only it goes looking for -- so it books no
        token the covering card also books.  Hiding it alongside the router
        card silenced the current-model strip exactly when the panel most
        needed an account of what Hermes is talking to.
        """
        self.session({'base_url': 'http://localhost:47831/v1'})
        log_file = self.home / 'llama-server.log'
        log_file.write_text('proxying request to model x on port 1\n')
        config = copy.deepcopy(monitor.DEFAULT_CONFIG)
        config['live'] = {'enabled': True, 'log': str(log_file)}
        config['stats']['enabled'] = False
        config['gpu']['enabled'] = False
        config['system']['enabled'] = False
        config['endpoints'] += [
            {'id': 'strata', 'kind': 'strata', 'base_url': 'http://127.0.0.1:47831'},
        ]
        with patch.object(monitor, 'hermes_config_candidates', return_value=[self.config]):
            hub = monitor.Hub(config)
            self.addCleanup(hub._pool.shutdown)
            active = hub._active_endpoints()
            self.assertEqual(hub.router_covered_by, 'strata')
            self.assertNotIn(hub.hermes_endpoint, active)
            lives = [ep for ep in active if getattr(ep, 'log_path', None) is not None]
            self.assertEqual(lives, [hub.live_endpoint])
            # The polling card comes back the moment the addresses differ --
            # the live card was never what dedup was guarding.
            self.session({'base_url': 'http://localhost:18434/v1'}, activity=30)
            hub._refresh_router(force=True)
            self.assertIn(hub.hermes_endpoint, hub._active_endpoints())


if __name__ == '__main__':
    unittest.main()
