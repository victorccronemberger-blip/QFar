"""Original progress integration; declared inert bytes and provider adapters.

BEFORE loads only the immutable v1 core module, with all remaining dependencies
current and explicitly bound by the offline runner. The exact same test bytes
run AFTER with the current core. No original footage or provider is involved.
"""
from __future__ import annotations

import hashlib
import importlib.util
import math
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import moneymin

_before_core = os.environ.get('ORIGINAL_PROGRESS_BEFORE_CORE')
if _before_core:
    _before_path = Path(_before_core)
    if hashlib.sha256(_before_path.read_bytes()).hexdigest() != '92bb1a0cfb486394814a482f7fe9ce52a2c3254b8d936c03121a5fe482f57078':
        raise RuntimeError('The immutable v1 core witness changed')
    _spec = importlib.util.spec_from_file_location('moneymin.original_capture', _before_path)
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = _module
    _spec.loader.exec_module(_module)
    moneymin.original_capture = _module

from moneymin import original_capture, upload
from moneymin.web.runner import CampaignRunner
_fixture_spec = importlib.util.spec_from_file_location('declared_original_workflow_fixture',
    Path(__file__).with_name('test_original_capture_workflow.py'))
fixture = importlib.util.module_from_spec(_fixture_spec)
sys.modules[_fixture_spec.name] = fixture
_fixture_spec.loader.exec_module(fixture)


class OriginalCaptureProgressTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.OriginalCaptureWorkflowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_real_zip_transport_details_reach_actual_runner_operation(self):
        plan = self.fixture.plan()
        raw_progress, observed = [], []
        real_blob, real_event = upload._put_blob, CampaignRunner._on_event

        def observe_blob(*args, **kwargs):
            callback = kwargs.get('on_progress')
            self.assertTrue(callable(callback))
            def observe(sent, total, elapsed):
                speed = sent / elapsed if elapsed > 0 else 0.0
                raw_progress.append(dict(artifact='sidecar', sent_bytes=sent, total_bytes=total,
                    speed_bps=speed, eta_s=max(0, total - sent) / speed if speed > 0 else None,
                    percent=sent * 100.0 / total if total else 100.0))
                return callback(sent, total, elapsed)
            return real_blob(*args, **{**kwargs, 'on_progress': observe})

        def observe_event(runner, kind, payload):
            real_event(runner, kind, payload)
            if kind == 'account_progress' and payload.get('phase') == 'transport' and payload.get('artifact') == 'sidecar' and 'percent' in payload:
                row = next(row for row in runner.operation.snapshot()['accounts'] if row['email'] == fixture.OWNER)
                observed.append((dict(payload), dict(row), runner.current))

        with patch.object(upload, '_put_blob', side_effect=observe_blob), patch.object(CampaignRunner, '_on_event', observe_event):
            runner, history, _api, committed = self.fixture.integrated_protocol(plan)
        self.assertTrue(raw_progress, 'The real sidecar transport emitted no progress')
        self.assertEqual(len(observed), len(raw_progress), 'Original core discarded ZIP progress details')
        for raw, (payload, row, current) in zip(raw_progress, observed):
            with self.subTest(sent_bytes=raw['sent_bytes']):
                for key, value in raw.items():
                    self.assertEqual(payload[key], value, key)
                self.assertEqual((row['state'], row['progress']), ('sending', round(raw['percent'])))
                self.assertIn('subindo sensores', current)
                self.assertIn(f"{raw['percent']:.0f}%", current)
                self.assertNotIn(str(self.fixture.root / 'source'), str(payload))
        self.assertEqual((runner.state, history['status']), ('done', 'done'))
        self.assertEqual(committed[f'{fixture.SID}_0.data.zip'], plan.captures[0].sidecar.path.read_bytes())
        self.assertEqual(runner.ok_sends, 1)

    def test_only_typed_bounded_public_progress_details_are_forwarded(self):
        plan = self.fixture.plan()
        allowed = dict(artifact='sidecar', sent_bytes=3, total_bytes=8,
                       percent=37.5, speed_bps=2.5, eta_s=None)
        private = dict(rawpath='private-canary', error='private-canary',
                       body={'secret': 'private-canary'}, sas='private-canary')
        invalid = [('artifact', 'private-canary'), ('percent', True), ('percent', '3'),
                   ('percent', -1), ('percent', 101), ('percent', math.nan), ('percent', math.inf),
                   ('speed_bps', False), ('speed_bps', -0.1), ('speed_bps', math.inf),
                   ('eta_s', True), ('eta_s', -1), ('eta_s', math.nan),
                   ('sent_bytes', True), ('sent_bytes', -1), ('sent_bytes', 2.0),
                   ('sent_bytes', 10**400), ('total_bytes', False), ('total_bytes', -1),
                   ('total_bytes', 2.0), ('total_bytes', math.inf)]
        supplied = [{**allowed, **private}, *[{**allowed, **private, key: value} for key, value in invalid]]
        observed = []
        def send(*_args, **kwargs):
            for details in supplied:
                kwargs['on_progress']('transport', upload.STATE_TRANSPORT, 1, **details)
            return fixture.fixture_result(plan)
        def observe(kind, payload):
            if kind == 'account_progress':
                observed.append(payload)
        with patch.object(upload, 'upload_session', side_effect=send):
            original_capture.run_original_capture_campaign(fixture.fixture_config(plan), progress=observe)
        self.assertEqual(len(observed), len(supplied))
        for index, payload in enumerate(observed):
            expected = dict(allowed)
            if index:
                expected.pop(invalid[index - 1][0])
            with self.subTest(index=index):
                actual = {key: value for key, value in payload.items() if key in allowed}
                self.assertEqual(actual, expected)
                self.assertFalse(set(private) & set(payload))
                self.assertNotIn('private-canary', str(payload))
