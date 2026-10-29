"""On-demand private pipe to the native UI. Never persist returned credentials."""
import json

from client.identity import windows_subject
from client.paths import credential_path
from client.transport import Api,load_credentials


def user_access(config):
    api=Api(config)
    subject=windows_subject(config['identity_key'])
    catalog=api.identities()
    if catalog.get('scope')!='principal' or catalog.get('principal_key')!=subject['principal_key']:
        return dict(ok=False,code='user_scope_required')
    return dict(ok=True,read_token=config['read_token'],dashboard_url=api.base+'/')


def main():
    try:result=user_access(load_credentials(credential_path()))
    except Exception:result=dict(ok=False,code='access_unavailable')
    # Only the requesting native process receives stdout. No status/log files.
    print(json.dumps(result,ensure_ascii=True))


if __name__=='__main__':main()
