"""Save a source API key through the local encrypted credential endpoint."""
import argparse
import getpass
import json
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.build_info import local_url


def main():
    parser = argparse.ArgumentParser(description="为本机看台配置数据来源密钥；输入不会显示或写入命令历史。")
    parser.add_argument("source", choices=("api_football", "football_data"))
    parser.add_argument("--clear", action="store_true", help="移除此来源的已存密钥")
    args = parser.parse_args()
    key = "" if args.clear else getpass.getpass("API key（输入不显示）：").strip()
    if not args.clear and not key:
        print("没有输入密钥，未作修改。")
        return 1
    request = Request(local_url() + f"/api/data-sources/{args.source}/credential",
                      data=None if args.clear else json.dumps({"api_key": key}).encode(),
                      method="DELETE" if args.clear else "PUT", headers={"Content-Type": "application/json"})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=10) as response:
            result = json.load(response)
        print("密钥已加密保存。" if result.get("configured") else "已移除此来源密钥。")
        return 0
    except HTTPError as exc:
        print(f"配置未完成（HTTP {exc.code}）。请确认正在运行的是本机看台，并检查密钥格式。")
    except (URLError, TimeoutError):
        print("无法连接本机看台，请先启动应用。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
