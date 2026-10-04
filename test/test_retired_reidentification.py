"""The retired CLI and dynamically imported helpers must have no legacy effects."""
from pathlib import Path
import builtins
import io
import unittest
from unittest.mock import patch


class RetiredReidentificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = Path(__file__).resolve().parents[1] / 'scripts/reid_native_upload.py'
        cls.code = compile(cls.path.read_bytes(), str(cls.path), 'exec')

    def _execute(self, *, entry=False):
        allowed_import = builtins.__import__
        imports = []
        def inert_import(name, *args, **kwargs):
            imports.append(name)
            if name not in {'sys', '__future__'}:
                raise AssertionError('Retired workflow attempted a dependency import')
            return allowed_import(name, *args, **kwargs)
        namespace = {'__name__': '__main__' if entry else 'retired_capture_fixture'}
        output = io.StringIO()
        with patch('builtins.__import__', side_effect=inert_import), \
                patch('builtins.open', side_effect=AssertionError('Retired workflow opened a file')), \
                patch('sys.stderr', output), \
                patch('sys.argv', [str(self.path), 'private-capture-canary', 'private-account-canary']):
            if entry:
                with self.assertRaises(SystemExit) as result:
                    exec(self.code, namespace)
                self.assertEqual(result.exception.code, 2)
            else:
                exec(self.code, namespace)
        return namespace, output.getvalue(), imports

    def test_import_is_inert_and_does_not_load_application_dependencies(self):
        _, output, imports = self._execute()
        self.assertEqual(output, '')
        self.assertEqual(set(imports), {'sys', '__future__'})

    def test_cli_fails_without_io_auth_or_echoing_arguments(self):
        _, output, _ = self._execute(entry=True)
        self.assertIn('aposentado', output)
        self.assertNotIn('private-capture-canary', output)
        self.assertNotIn('private-account-canary', output)

    def test_legacy_dynamic_helpers_fail_before_side_effects(self):
        namespace, _, _ = self._execute()
        with patch('builtins.open', side_effect=AssertionError('Unexpected file access')):
            for name, args in (
                ('find_capture', ('private-capture-canary',)),
                ('reid_zip', (b'private-zip-canary', 'replacement-id-canary', 'replacement-date-canary')),
            ):
                with self.subTest(name=name):
                    with self.assertRaises(namespace['RetiredCaptureWorkflow']) as exc:
                        namespace[name](*args)
                    for canary in ('private-capture-canary','private-zip-canary','replacement-id-canary','replacement-date-canary'):
                        self.assertNotIn(canary,str(exc.exception))

class LegacyCaptureAliasesRetirementTests(unittest.TestCase):
    """Historical preparer/sender aliases must be inert before dependency IO."""

    @classmethod
    def setUpClass(cls):
        scripts = Path(__file__).resolve().parents[1] / 'scripts'
        cls.codes = {
            name: compile((scripts / name).read_bytes(), str(scripts / name), 'exec')
            for name in ('forensic_prepare.py', 'forensic_resend.py')
        }

    def _execute_alias(self, name, mode, argv=None):
        allowed_import = builtins.__import__
        imports = []

        def inert_import(module_name, *args, **kwargs):
            imports.append(module_name)
            if module_name not in {'sys', '__future__'}:
                raise AssertionError('Retired alias attempted a dependency import')
            return allowed_import(module_name, *args, **kwargs)

        def forbidden_io(*args, **kwargs):
            raise AssertionError('Retired alias attempted file IO')

        namespace = {'__name__': '__main__' if mode == 'cli' else 'retired_alias_fixture'}
        output = io.StringIO()
        canaries = ['private-capture-path-canary', 'private-account-password-canary']
        with patch('builtins.__import__', side_effect=inert_import), \
                patch('builtins.open', side_effect=forbidden_io), \
                patch.object(Path, 'open', side_effect=forbidden_io), \
                patch.object(Path, 'read_bytes', side_effect=forbidden_io), \
                patch.object(Path, 'read_text', side_effect=forbidden_io), \
                patch.object(Path, 'write_bytes', side_effect=forbidden_io), \
                patch.object(Path, 'write_text', side_effect=forbidden_io), \
                patch('sys.stderr', output), \
                patch('sys.argv', [name, *canaries]):
            if mode == 'cli':
                with self.assertRaises(SystemExit) as result:
                    exec(self.codes[name], namespace)
                self.assertEqual(result.exception.code, 2)
            else:
                exec(self.codes[name], namespace)
                if mode == 'main':
                    self.assertEqual(namespace['main'](argv), 2)
        return output.getvalue(), imports

    def test_both_alias_imports_are_inert_without_application_dependencies(self):
        for name in self.codes:
            with self.subTest(alias=name):
                output, imports = self._execute_alias(name, 'import')
                self.assertEqual(output, '')
                self.assertEqual(set(imports), {'sys', '__future__'})

    def test_both_cli_aliases_reject_before_io_and_hide_arguments(self):
        for name in self.codes:
            with self.subTest(alias=name):
                output, imports = self._execute_alias(name, 'cli')
                self.assertIn('aposentado', output)
                self.assertIn('inspect_original_capture.py', output)
                self.assertNotIn('private-capture-path-canary', output)
                self.assertNotIn('private-account-password-canary', output)
                self.assertEqual(set(imports), {'sys', '__future__'})

    def test_both_direct_main_aliases_reject_without_parsing_or_echo(self):
        for name in self.codes:
            for argv in (None, ['private-capture-path-canary', 'private-account-password-canary']):
                with self.subTest(alias=name, explicit_argv=argv is not None):
                    output, imports = self._execute_alias(name, 'main', argv)
                    self.assertIn('aposentado', output)
                    self.assertIn('inspect_original_capture.py', output)
                    self.assertNotIn('private-capture-path-canary', output)
                    self.assertNotIn('private-account-password-canary', output)
                    self.assertEqual(set(imports), {'sys', '__future__'})


if __name__ == '__main__':
    unittest.main()
