import json
from pathlib import Path
import unittest
from unittest.mock import patch

from client.quota import QuotaError
from client.quota_settings import test_connection


class QuotaSettingsTests(unittest.TestCase):
    def test_connection_uses_temporary_proposed_settings_and_returns_no_raw_quota(self):
        used=[]
        proposed={'proxy':'http://127.0.0.1:17890'}
        def reader(**kwargs):
            path=Path(kwargs['network_path']);used.append(path)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')),proposed)
            return {'windows':[{},{}],'private':'must-not-display'}
        with patch('client.quota_settings.read_quota',side_effect=reader):
            result=test_connection(proposed)
        self.assertTrue(result['ok'])
        self.assertNotIn('must-not-display',json.dumps(result))
        self.assertFalse(used[0].exists())

    def test_invalid_settings_never_start_a_network_test_or_expose_credentials(self):
        for value in ({'proxy':'http://user:private-password@localhost:7890'},{'mode':[]},{'mode':'unknown'}):
            with self.subTest(value=value),patch('client.quota_settings.read_quota') as reader:
                result=test_connection(value)
                self.assertFalse(result['ok']);reader.assert_not_called()
                self.assertNotIn('private-password',json.dumps(result))

    def test_timeout_is_reported_as_a_network_problem(self):
        with patch('client.quota_settings.read_quota',side_effect=QuotaError('quota_timeout')):
            result=test_connection({'mode':'direct'})
        self.assertFalse(result['ok'])
        self.assertIn('网络',result['message'])


if __name__=='__main__':unittest.main()
