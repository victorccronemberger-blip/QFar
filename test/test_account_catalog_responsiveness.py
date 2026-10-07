"""Account reads must remain independent of cold catalog classification."""
import json
import threading
import time
from contextlib import ExitStack
from unittest.mock import patch

from moneymin import background_work, credential_store, task_matching, web
from moneymin.web import server
from moneymin.web.catalog_loader import CatalogLoader


def test_service_bootstrap_does_not_compute_catalog():
    with patch.object(web, '_harden_stdio'), patch.object(web.tls, 'configure_environment'), \
         patch.object(web, '_watch_parent'), patch.object(web, 'create_app') as create, \
         patch.object(web, '_serve') as serve, \
         patch.object(web.campaign, 'warm_task_catalog') as warm, \
         patch.object(web.threading, 'Thread') as thread:
        web._run_claimed_service('127.0.0.1', 8876, None)
    serve.assert_called_once_with(create.return_value, '127.0.0.1', 8876, False)
    warm.assert_not_called()
    thread.assert_not_called()


def test_budget_yields_only_inside_catalog_and_restores_after_exception():
    clock = [0.0]
    with patch.object(background_work.time, 'monotonic', side_effect=lambda: clock[0]), \
         patch.object(background_work.time, 'sleep') as sleep:
        background_work.checkpoint()
        try:
            with background_work.responsive_catalog_work():
                clock[0] = .02
                with background_work.responsive_catalog_work():
                    background_work.checkpoint()
                background_work.checkpoint()
                raise RuntimeError('fixture')
        except RuntimeError:
            pass
        clock[0] = 100
        background_work.checkpoint()
    sleep.assert_called_once_with(.001)


def test_catalog_budget_preserves_full_event_classification():
    events = task_matching.prepare_span_events([
        (0, '#C C cuts onions and places them in a pot'),
        (10, '#C C washes dishes with a sponge'),
        (20, '#C C looks at a phone'),
        (30, '#C C carries a bag'),
    ])
    rules = tuple(task_matching.TASK_RULES.items())
    normal = task_matching.label_span_events(events, rules)
    with background_work.responsive_catalog_work():
        assert task_matching.label_span_events(events, rules) == normal


def test_accounts_stay_responsive_and_credential_integrity_holds_during_catalog(tmp_path):
    data = tmp_path / 'data'
    secrets = tmp_path / 'secrets'
    data.mkdir()
    secrets.mkdir()
    emails = [f'fixture{i}@example.invalid' for i in range(49)]
    for email in emails:
        credential_store.save(secrets, email, 'fixture-password')
    corrupt = credential_store.record_path(secrets, emails[0])
    corrupt.write_bytes(b'fixture-corrupt-primary')
    legacy_email = 'legacy@example.invalid'
    missing_email = 'missing@example.invalid'
    legacy = data / 'crowtado_passwords.json'
    legacy.write_text(json.dumps({emails[0]: 'fixture-stale',
                                  legacy_email: 'fixture-legacy'}), encoding='utf8')
    rows = [{'email': email, 'last_check': {}}
            for email in [*emails, legacy_email, missing_email]]
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    loader = CatalogLoader()

    def classify(progress):
        entered.set()
        try:
            rule = next(iter(task_matching.TASK_RULES.values()))
            while not release.is_set():
                task_matching.span_evidence_possible(rule, 'fixture unrelated words ' * 1000)
            return {'tasks': []}, 200
        finally:
            finished.set()

    with ExitStack() as stack:
        for name, value in [('DATA_DIR', data), ('SECRETS_DIR', secrets)]:
            stack.enter_context(patch.object(server.config, name, value))
        stack.enter_context(patch.object(server, 'CROWTADO_PW_PATH', legacy))
        stack.enter_context(patch.object(server, '_list_accounts', return_value=rows))
        stack.enter_context(patch.object(server, '_removed_accounts', return_value=set()))
        stack.enter_context(patch.object(server, '_load_account_health_history', return_value={}))
        stack.enter_context(patch.object(server.registration_state, 'load', return_value={}))
        stack.enter_context(patch.object(server, '_load_balances', return_value={}))
        bulk = stack.enter_context(patch.object(credential_store, 'load_all',
            side_effect=AssertionError('account list must not bulk decrypt then lookup again')))
        lookup = stack.enter_context(patch.object(credential_store, 'lookup', wraps=credential_store.lookup))
        client = server.create_app(for_testing=True).test_client()
        try:
            assert loader.get(('cpu',), classify)[1] == 202
            assert entered.wait(2)
            started = time.monotonic()
            response = client.get('/api/accounts')
            duration = time.monotonic() - started
            assert response.status_code == 200
            assert duration < 2, duration
            accounts = {row['email']: row for row in response.json['accounts']}
            assert accounts[emails[0]]['has_password'] is False
            assert all(accounts[e]['has_password'] for e in emails[1:])
            assert accounts[legacy_email]['has_password'] is True
            assert accounts[missing_email]['has_password'] is False
            assert 'fixture-password' not in response.get_data(as_text=True)
            assert 'fixture-stale' not in response.get_data(as_text=True)
            assert lookup.call_count == len(rows)
            bulk.assert_not_called()
            assert corrupt.read_bytes() == b'fixture-corrupt-primary'
        finally:
            release.set()
            assert finished.wait(3)
    deadline = time.monotonic() + 3
    while loader.get(('cpu',), classify)[1] == 202 and time.monotonic() < deadline:
        time.sleep(.01)
    assert loader.get(('cpu',), classify) == ({'tasks': []}, 200)
