"""The local window always belongs to the running Windows principal, including admin readers."""
import re


def window_filters(subject,requested,current_accounts,primary=None):
    if not subject or not re.fullmatch(r"[0-9a-f]{64}",subject.get("principal_key", "")):
        return None
    result={"principal":subject["principal_key"]}
    requested=requested if isinstance(requested,dict) else {}
    if "account" not in requested:
        if current_accounts:result["account"]=primary if primary in current_accounts else current_accounts[0]
    else:
        account=requested.get("account")
        if account=="unassigned" or (isinstance(account,str) and re.fullmatch(r"[0-9a-f]{64}",account)):
            result["account"]=account
    return result
