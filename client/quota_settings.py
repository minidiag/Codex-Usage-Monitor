"""Test a proposed quota route without changing the installed configuration."""
import argparse
import json
from pathlib import Path
import sys
import tempfile

from client.quota import QuotaError, read_quota, validate_network_settings


def test_connection(settings):
    try:
        validate_network_settings(settings)
        with tempfile.TemporaryDirectory(prefix="codex-quota-network-") as temporary:
            path = Path(temporary) / "network.json"
            path.write_text(json.dumps(settings), encoding="utf-8")
            quota = read_quota(network_path=path)
        return dict(ok=True, message="连接成功，已读取官方额度接口的 " + str(len(quota["windows"])) + " 个窗口。")
    except QuotaError as error:
        messages = {
            "quota_proxy_invalid": "代理地址无效，请填写不含用户名、密码或路径的 HTTP/HTTPS 地址。",
            "quota_timeout": "读取超时，请检查代理是否可用及当前网络。",
            "codex_not_found": "未找到本机 Codex，请检查安装状态。",
            "quota_unavailable": "额度接口暂不可用，请检查网络和 Codex 登录状态。",
        }
        return dict(ok=False, message=messages.get(str(error), "连接测试失败，请稍后重试。"))
    except (OSError, ValueError, TypeError):
        return dict(ok=False, message="无法完成连接测试，请检查本机运行环境。")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", required=True)
    parser.parse_args()
    try:
        raw = sys.stdin.read(4097)
        settings = json.loads(raw) if len(raw) <= 4096 else None
        result = test_connection(settings)
    except (ValueError, TypeError):
        result = dict(ok=False, message="连接测试设置无效。")
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
