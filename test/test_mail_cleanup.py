import hashlib
import time
import unittest
from email.message import EmailMessage
from email.parser import BytesParser
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
                     '<a href="https://wise.com/transfers/a">Open</a>', '<b>pay</b>out is ready'):
            with self.subTest(body=body):
                self.assertEqual(c.classify(message(body=body, subtype='html'))[0], 'payment')

    def test_transport_headers_are_not_payment_content(self):
        for header, value in (
                ('DKIM-Signature', 'v=1; h=From:Subject:Content-Transfer-Encoding; b=randomwiseclaimdots'),
                ('ARC-Message-Signature', 'h=Content-Transfer-Encoding:Subject'),
                ('X-SG-EID', 'randomtransferrewardCLAIMwise'),
                ('Message-ID', '<transfer-payment@dots.dev>'),
                ('Return-Path', '<bounce@payments.example.com>')):
            msg = BytesParser(policy=c.policy.default).parsebytes(message())
            if header in msg:
                msg.replace_header(header, value)
            else:
                msg[header] = value
            with self.subTest(header=header):
                self.assertEqual(c.classify(msg.as_bytes())[0], 'move')

    def test_real_unrelated_templates_no_longer_look_like_payouts(self):
        cases = (
            ('Confirme sua solicitação de encaminhamento de e-mails', 'Comece a receber e-mails neste endereço.'),
            ('Prepare sua nova conta da AWS', 'Se preferir não receber e-mails no futuro, cancele sua inscrição.'),
            ('AWS Account Closure Confirmation', 'We must receive full payment of any outstanding balance.'),
            ('Introducing Our New Referral Program · Claru', 'Head to the Rewards page. Share your referral link.'),
            ('Verify your email address', 'To complete your Dots account, verify at https://my.dots.dev/confirm-email/id'),
            ('New sign-in to your OpenAI account', 'A new device signed in. No action required.'),
            ('Ver o que você pode fazer agora', 'Discover the new features.'),
            ('Revisão da conta do Minute', 'Ganhos aprovados e pagamentos anteriores continuam vinculados a ela.'),
        )
        for subject, body in cases:
            with self.subTest(subject=subject):
                self.assertEqual(c.classify(message(subject, body))[0], 'move')

    def test_substrings_and_provider_footer_do_not_protect_newsletters(self):
        for body in ('Otherwise, consider the dots on this diagram. Disclaimer: claim rules apply.',
                     '<style>.payment { color: red }</style><p>News</p>'
                     '<a href="https://wise.com/privacy">Wise</a>',
                     '<p>Your Dots account</p><a href="https://dots.dev">Home</a>',
                     '<a href="https://example.com/transfer?campaign=claim-reward">News</a>'):
            with self.subTest(body=body):
                self.assertEqual(c.classify(message('Newsletter', body, subtype='html'))[0], 'move')

    def test_actual_payout_routes_and_money_records_remain_protected(self):
        for body in ('<a href="https://my.dots.dev/flow/secret">Open</a>',
                     '<a href="https://www.tremendous.com/rewards/payout/id">Open</a>',
                     'Transfer of $38.64 has been received.',
                     'Receba seu dinheiro', 'Seu resgate está disponível',
                     'Your reward is ready', 'Saque recusado; tente novamente'):
            with self.subTest(body=body):
                self.assertEqual(c.classify(message('Update', body, subtype='html'))[0], 'payment')

    def test_unfamiliar_financial_route_requires_review(self):
        raw = message('Update', '<a href="https://wise.com/a">Open</a>', subtype='html')
        self.assertEqual(c.classify(raw)[0], 'review')

    def test_newsletter_inline_logo_is_not_unreadable_attachment(self):
        msg = BytesParser(policy=c.policy.default).parsebytes(message('News', '<p>News</p>', subtype='html'))
        msg.add_related(b'logo', maintype='image', subtype='png', cid='<logo>', disposition='inline')
        self.assertEqual(c.classify(msg.as_bytes())[0], 'move')

    def test_unreadable_attachment_and_payout_attachment_name_still_protected(self):
        for filename, action in (('document.pdf', 'review'), ('payout.pdf', 'payment')):
            msg = BytesParser(policy=c.policy.default).parsebytes(message('Document', 'See attached'))
            msg.add_attachment(b'%PDF', maintype='application', subtype='pdf', filename=filename)
            self.assertEqual(c.classify(msg.as_bytes())[0], action)

    def test_provider_domain_lookalike_does_not_create_payment_evidence(self):
        for link in ('https://my.dots.dev.example.com/flow/id', 'https://evil.example/payout/id'):
            self.assertEqual(c.classify(message('News', 'Read the news: ' + link))[0], 'move')

    def test_payment_body_is_checked_even_for_auth_subject(self):
        self.assertEqual(c.classify(message('Your verification code',
                         'Also claim your payout at https://my.dots.dev/flow/id'))[0], 'payment')

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

    def test_server_page_size_is_respected_instead_of_stopping_at_short_page(self):
        box = c.Mailbox({'token': 'secret', 'mailbox_id': 'box'})
        pages = [{'data': [{'uid': 1}], 'pagination': {'page': 1, 'totalPages': 2}},
                 {'data': [{'uid': 2}], 'pagination': {'page': 2, 'totalPages': 2}}]
        with patch.object(box, 'get', side_effect=pages) as get:
            self.assertEqual([r['uid'] for r in box.pages('/folders/INBOX/messages')], [1, 2])
            self.assertEqual(get.call_count, 2)

    def test_missing_advertised_page_blocks_plan(self):
        box = c.Mailbox({'token': 'secret', 'mailbox_id': 'box'})
        with patch.object(box, 'get', return_value={'data': [], 'pagination': {'page': 1, 'totalPages': 2}}):
            with self.assertRaises(c.CleanupError):
                list(box.pages('/folders/INBOX/messages'))

    def test_cleanup_preview_and_move_use_same_rules_for_signed_auth_mail(self):
        msg = BytesParser(policy=c.policy.default).parsebytes(message())
        msg['DKIM-Signature'] = 'h=From:Content-Transfer-Encoding:Subject; b=wise'
        raw = msg.as_bytes()
        box = Mock()
        box.trash.return_value = 'INBOX.Trash'
        box.messages.return_value = iter([{'uid': 1, 'subject': 'Your verification code'}])
        box.source.return_value = raw
        cleanup = c.Cleanup()
        with patch.object(c, 'Mailbox', return_value=box):
            cleanup.scan({})
        cleanup.execute(box, 'INBOX.Trash', cleanup.plan[2], [1])
        box.move.assert_called_once_with(1, 'INBOX.Trash')
        self.assertEqual(cleanup.snapshot()['moved'], 1)

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
        client = server.create_app(for_testing=True).test_client()
        with patch.object(c, 'CLEANUP', c.Cleanup()), patch.object(c.mail, 'configured_connections', return_value=[]):
            self.assertEqual(client.post('/api/mail-cleanup/apply', json={'uids': [1]}).status_code, 400)
            self.assertEqual(client.post('/api/mail-cleanup/preview', json={'profile_id': 'bad'}).status_code, 400)
            self.assertEqual(client.get('/api/mail-cleanup').json['state'], 'idle')


if __name__ == '__main__':
    unittest.main()
