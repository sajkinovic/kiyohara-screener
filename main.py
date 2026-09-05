import datetime
import os
import time
from typing import Dict, List, Optional
import requests

JQUANTS_BASE_URL = "https://api.jquants.com/v2"
MAX_RETRIES = 3


class JQuantsClient:
    def __init__(self):
        self.session = requests.Session()
        self.api_key = os.environ.get("JQUANTS_API_KEY")
        if not self.api_key:
            raise RuntimeError("JQUANTS_API_KEY が設定されていません。")

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

    def get_listed_info(self) -> List[dict]:
        res = self._get("/equities/master")
        return res.get("info", res.get("equities", []))

    def get_daily_quotes_by_code(self, code: str, start_date: str, end_date: str) -> List[dict]:
        res = self._get(
            "/equities/bars/daily",
            params={"code": code, "from": start_date, "to": end_date}
        )
        return res.get("daily_quotes", res.get("bars", []))

    def get_financial_statements(self, code: str) -> List[dict]:
        res = self._get(
            "/fins/summary",
            params={"code": code}
        )
        return res.get("statements", res.get("summary", []))


def parse_float(val) -> Optional[float]:
    if val is None or val == "" or val == "-":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def get_latest_statement(statements: List[dict]) -> Optional[dict]:
    if not statements:
        return None
    valid = [s for s in statements if s.get("DisclosedDate")]
    if not valid:
        return statements[-1]
    valid.sort(key=lambda x: x["DisclosedDate"])
    return valid[-1]


def screen_stock(client: JQuantsClient, stock: dict, start_date_str: str, end_date_str: str) -> Optional[dict]:
    code = stock.get("Code", "")
    company_name = stock.get("CompanyName", "")
    market = stock.get("MarketCodeName", stock.get("Section", ""))

    if not code:
        return None

    quotes = client.get_daily_quotes_by_code(code, start_date_str, end_date_str)
    if not quotes:
        return None

    quotes.sort(key=lambda x: x.get("Date", ""))
    latest_quote = quotes[-1]
    close_price = parse_float(latest_quote.get("Close"))
    if not close_price or close_price <= 0:
        return None

    statements = client.get_financial_statements(code)
    stmt = get_latest_statement(statements)
    if not stmt:
        return None

    equity = parse_float(stmt.get("NetAssets", stmt.get("Equity")))
    bps = parse_float(stmt.get("BookValuePerShare", stmt.get("BPS")))
    eps = parse_float(stmt.get("EarningsPerShare", stmt.get("EPS")))
    shs_out = parse_float(stmt.get("NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYearIncludingTreasuryStock"))

    if not bps or bps <= 0:
        return None

    pbr = close_price / bps
    per = (close_price / eps) if (eps and eps > 0) else None
    market_cap = (close_price * shs_out) if shs_out else None

    if pbr < 1.0:
        return {
            "Code": code,
            "Name": company_name,
            "Market": market,
            "Close": close_price,
            "PBR": round(pbr, 2),
            "PER": round(per, 2) if per else "N/A",
            "BPS": bps,
            "EPS": eps if eps else "N/A",
            "MarketCap": round(market_cap / 1e8, 2) if market_cap else "N/A"
        }

    return None


def main():
    print("J-Quants API (V2) に接続を開始します...")
    client = JQuantsClient()

    print("上場銘柄一覧を取得中...")
    stocks = client.get_listed_info()
    print(f"対象銘柄数: {len(stocks)}")

    today = datetime.date.today()
    start_date = today - datetime.timedelta(days=30)
    start_date_str = start_date.strftime("%Y-%m-%d")
    end_date_str = today.strftime("%Y-%m-%d")

    results = []
    test_target = stocks[:50]

    print("スクリーニング処理を実行中...")
    for i, stock in enumerate(test_target, 1):
        try:
            res = screen_stock(client, stock, start_date_str, end_date_str)
            if res:
                results.append(res)
            time.sleep(0.1)
        except Exception as e:
            print(f"銘柄 {stock.get('Code')} でエラー発生: {e}")

    print("\n--- スクリーニング結果 ---")
    print(f"抽出条件に合致した銘柄数: {len(results)}")
    for r in results:
        print(f"[{r['Code']}] {r['Name']} ({r['Market']}) - 株価: {r['Close']}円 / PBR: {r['PBR']}倍 / PER: {r['PER']}")


if __name__ == "__main__":
    main()
