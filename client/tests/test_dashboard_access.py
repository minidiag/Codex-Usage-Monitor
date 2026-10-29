import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from client.dashboard_access import user_access,main


class DashboardAccessTests(unittest.TestCase):
    def test_only_user_read_token_and_url_are_returned(self):
        config=dict(identity_key='a'*64,read_token='r'*40,write_token='w'*40)
        with patch('client.dashboard_access.Api') as api,patch('client.dashboard_access.windows_subject',return_value=dict(principal_key='p')):
            api.return_value.base='https://example.test/codex-usage'
            api.return_value.identities.return_value=dict(scope='principal',principal_key='p')
            self.assertEqual(user_access(config),dict(ok=True,read_token='r'*40,dashboard_url='https://example.test/codex-usage/'))

    def test_tenant_read_token_is_never_returned(self):
        with patch('client.dashboard_access.Api') as api,patch('client.dashboard_access.windows_subject',return_value=dict(principal_key='p')):
            api.return_value.identities.return_value=dict(scope='tenant',principal_key=None)
            self.assertEqual(user_access(dict(identity_key='a'*64,read_token='SECRET')),dict(ok=False,code='user_scope_required'))

    def test_another_windows_user_is_rejected(self):
        with patch('client.dashboard_access.Api') as api,patch('client.dashboard_access.windows_subject',return_value=dict(principal_key='p')):
            api.return_value.identities.return_value=dict(scope='principal',principal_key='another')
            self.assertNotIn('read_token',user_access(dict(identity_key='a'*64,read_token='SECRET')))

    def test_failure_returns_no_credentials_or_exception_details(self):
        output=io.StringIO()
        with patch('client.dashboard_access.load_credentials',side_effect=ValueError('PRIVATE_DATA')),redirect_stdout(output):main()
        self.assertEqual(json.loads(output.getvalue()),dict(ok=False,code='access_unavailable'))
