import os
import time
from typing import Optional
import requests

JQUANTS_BASE_URL = "https://api.jquants.com/v2"
MAX_RETRIES = 3

class JQuantsClient:
    def __init__(self):
        self.session = requests.Session()
        self.api_key = os.environ.get("JQUANTS_API_KEY")
        if not self.api_key:
            raise RuntimeError("JQUANTS_API_KEY が設定されていません。")
        
        # V2 APIキー認証ヘッダー
        self.session.headers.update({"x-api-key": self.api_key})

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        last_exc = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.get(
                    f"{JQUANTS_BASE_URL}{path}",
                    params=params,
                    timeout=30
                )
                if resp.status_code == 429:
                    time.sleep(2 * attempt)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.HTTPError as exc:
                last_exc = exc
                if resp.status_code >= 500:
                    time.sleep(2 * attempt)
                    continue
                raise
        raise last_exc

def main():
    print("J-Quants APIへ認証しています...")
    client = JQuantsClient()
    print("認証に成功しました！")

if __name__ == "__main__":
    main()
