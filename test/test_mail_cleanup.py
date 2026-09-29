import hashlib
import time
import unittest
from email.message import EmailMessage
from unittest.mock import Mock, patch

from moneymin import mail_cleanup as c
from moneymin.web import server


def message(subject='Your verification code', body='Code 123456', **kwargs):
    msg = EmailMessage()
    msg['From'] = 'noreply@crowtado.com'
    msg['To'] = 'test@example.com'
    msg['Subject'] = subject
    msg['Message-ID'] = '<unique@example.com>'
    msg['Date'] = 'Tue, 01 Sep 2026 12:00:00 +0000'
    msg.set_content(body, **kwargs)
    return msg.as_bytes()


class MailCleanupTests(unittest.TestCase):
    def test_preserves_all_screenshot_subjects_and_variations(self):
        for subject in ('Your $38.64 Crowtado payout is ready', 'Your $54.69 Crowtado payout is ready',
                        'Seu saque está disponível', 'Pagamento pronto', 'PAYPAL', 'Wise transfer',
                        'Claim your money', 'Resgate disponível', 'Your reward is ready'):
            with self.subTest(subject=subject):
                self.assertEqual(c.classify(message(subject))[0], 'payment')

    def test_protects_body_and_html_links_even_with_unrelated_subject(self):
        for body in ('Open https://pay.dots.dev/secret', 'Seu pagamento está pronto',
                     '<a href="https://wise.com/a">Open</a>', '<b>pay</b>out is ready'):
            with self.subTest(body=body):
                self.assertEqual(c.classify(message(body=body, subtype='html'))[0], 'payment')

    def test_auth_email_is_candidate_but_recent_auth_is_preserved(self):
        raw = message()
        self.assertEqual(c.classify(raw)[0], 'move')
        self.assertEqual(c.classify(raw, now=0)[0], 'review')

    def test_malformed_empty_unknown_attachment_and_encrypted_are_preserved(self):
        for raw in (b'', b'not a mail', message(body=''),
                    message().replace(b'text/plain', b'application/octet-stream'),
                    message().replace(b'text/plain', b'application/pkcs7-mime')):
            self.assertEqual(c.classify(raw)[0], 'review')

    def test_official_trash_only_no_guessed_folder(self):
        box = c.Mailbox({'token': 'secret', 'mailbox_id': 'box'})
        for rows in ([], [{'path': 'Trash', 'specialUse': None}],
                     [{'path': 'INBOX', 'specialUse': '\\Trash'}],
                     [{'path': 'a', 'specialUse': '\\Trash'}, {'path': 'b', 'specialUse': '\\Trash'}]):
            with patch.object(box, 'pages', return_value=iter(rows)):
                with self.assertRaises(c.CleanupError):
                    box.trash()
        with patch.object(box, 'pages', return_value=iter([{'path': 'INBOX.Trash', 'specialUse': '\\Trash'}])):
            self.assertEqual(box.trash(), 'INBOX.Trash')

    def test_only_explicit_move_from_inbox_never_delete(self):
        box = c.Mailbox({'token': 'secret', 'mailbox_id': 'box'})
        with patch.object(c.mail, '_request', return_value=(204, None)) as request:
            box.move(10, 'INBOX.Trash')
        request.assert_called_once_with('/api/v1/mailboxes/box/folders/INBOX/messages/10/move',
                                        'POST', {'targetFolder': 'INBOX.Trash'}, token='secret')

    def test_move_error_and_lost_response_never_retry(self):
        box = c.Mailbox({'token': 'secret', 'mailbox_id': 'box'})
        for outcome in ((404, {}), (500, {}), (200, {})):
            with patch.object(c.mail, '_request', return_value=outcome) as request:
                with self.assertRaises(c.CleanupError):
                    box.move(10, 'INBOX.Trash')
                self.assertEqual(request.call_count, 1)

    def test_pagination_finishes_before_any_move_and_protects_failed_reads(self):
        box = Mock()
        box.trash.return_value = 'INBOX.Trash'
        box.messages.return_value = iter([{'uid': 1, 'subject': 'Your payout is ready'},
                                         {'uid': 2, 'subject': 'Code'}, {'uid': 3, 'subject': 'Unknown'}])
        box.source.side_effect = [message(), c.CleanupError('unavailable')]
        cleanup = c.Cleanup()
        with patch.object(c, 'Mailbox', return_value=box):
            cleanup.scan({})
        self.assertEqual([i['action'] for i in cleanup.snapshot()['items']], ['payment', 'move', 'review'])
        self.assertEqual(set(cleanup.plan[2]), {2})
        box.move.assert_not_called()

    def test_changed_content_is_preserved_and_only_snapshot_uids_are_moved(self):
        raw = message()
        box = Mock()
        box.trash.return_value = 'INBOX.Trash'
        box.source.side_effect = [raw, message('Your payout is ready')]
        cleanup = c.Cleanup()
        cleanup.state.update(processed=0)
        cleanup.execute(box, 'INBOX.Trash', {1: hashlib.sha256(raw).hexdigest(), 2: 'old'}, [1, 2])
        box.move.assert_called_once_with(1, 'INBOX.Trash')
        self.assertEqual(cleanup.snapshot()['skipped'], 1)
        self.assertEqual(cleanup.snapshot()['state'], 'done')

    def test_unknown_move_outcome_stops_before_next_message(self):
        raw = message()
        box = Mock()
        box.trash.return_value = 'INBOX.Trash'
        box.source.return_value = raw
        box.move.side_effect = TimeoutError()
        cleanup = c.Cleanup()
        cleanup.state.update(processed=0)
        cleanup.execute(box, 'INBOX.Trash', {1: hashlib.sha256(raw).hexdigest(), 2: 'x'}, [1, 2])
        self.assertEqual(cleanup.snapshot()['state'], 'error')
        self.assertEqual(box.move.call_count, 1)

    def test_rejects_expired_reused_protected_and_duplicate_selection(self):
        cleanup = c.Cleanup()
        cleanup.state.update(state='ready', id='plan')
        cleanup.plan = (Mock(), 'INBOX.Trash', {1: 'hash'}, time.monotonic())
        for selected in ([2], [True], [1, 1], [], '1'):
            with self.assertRaises(c.CleanupError):
                cleanup.apply('plan', selected)
        with self.assertRaises(c.CleanupError):
            cleanup.apply('other', [1])
        cleanup.plan = (*cleanup.plan[:3], time.monotonic() - 1801)
        with self.assertRaises(c.CleanupError):
            cleanup.apply('plan', [1])

    def test_cancel_stops_without_moving(self):
        box = Mock()
        box.trash.return_value = 'INBOX.Trash'
        cleanup = c.Cleanup()
        cleanup.cancel.set()
        cleanup.execute(box, 'INBOX.Trash', {1: 'hash'}, [1])
        box.move.assert_not_called()
        self.assertEqual(cleanup.snapshot()['state'], 'cancelled')

    def test_api_requires_confirmation_and_cannot_supply_destination_or_token(self):
        client = server.create_app().test_client()
        with patch.object(c, 'CLEANUP', c.Cleanup()), patch.object(c.mail, 'configured_connections', return_value=[]):
            self.assertEqual(client.post('/api/mail-cleanup/apply', json={'uids': [1]}).status_code, 400)
            self.assertEqual(client.post('/api/mail-cleanup/preview', json={'profile_id': 'bad'}).status_code, 400)
            self.assertEqual(client.get('/api/mail-cleanup').json['state'], 'idle')


if __name__ == '__main__':
    unittest.main()
