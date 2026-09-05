"""
JPX全銘柄スクリーニングツール

データソースは J-Quants API (https://jpx-jquants.com/) を使用する。
実行には J-Quants のアカウントが必要で、認証情報を環境変数（GitHub Actions では
Secrets）として以下のいずれかの形で渡す。

  - JQUANTS_REFRESH_TOKEN            : リフレッシュトークンを直接渡す場合
  - JQUANTS_MAILADDRESS / JQUANTS_PASSWORD : メールアドレスとパスワードを渡す場合
                                        （内部でリフレッシュトークンを取得する）

抽出条件（すべて満たす銘柄を抽出、環境変数で閾値の上書き可）:
  - ネットキャッシュ比率 >= 0.5   （NET_CASH_RATIO_MIN）
  - PER <= 15倍                  （PER_MAX）
  - PBR <= 1倍                   （PBR_MAX）
  - 営業キャッシュフロー黒字
  - 当期純利益黒字
  - 時価総額 <= 300億円           （MARKET_CAP_MAX、円単位）

ネットキャッシュ比率の定義について:
  J-Quants の財務情報サマリー(fins/statements)では有利子負債や投資有価証券の
  内訳までは取得できないため、本スクリプトでは入手可能な項目から
    ネットキャッシュ = 現金及び現金同等物(CashAndEquivalents)
                        - 総負債(TotalAssets - Equity)
    ネットキャッシュ比率 = ネットキャッシュ / 時価総額
  として簡易的に算出する。より厳密な「有利子負債ベース」の値とは異なる点に注意。
"""

import csv
import datetime as dt
import os
import sys
import time
from dataclasses import asdict, dataclass
from typing import Optional

import requests

JQUANTS_BASE_URL = "https://api.jquants.com/v1"

# --- スクリーニング条件（環境変数で上書き可） ---
NET_CASH_RATIO_MIN = float(os.environ.get("NET_CASH_RATIO_MIN", "0.5"))
PER_MAX = float(os.environ.get("PER_MAX", "15"))
PBR_MAX = float(os.environ.get("PBR_MAX", "1"))
MARKET_CAP_MAX = float(os.environ.get("MARKET_CAP_MAX", str(30_000_000_000)))  # 300億円

# 対象市場区分（プライム/スタンダード/グロース）。ETF・REIT・PRO Market等は除外。
TARGET_MARKET_CODES = {"0111", "0112", "0113"}

REQUEST_INTERVAL_SEC = float(os.environ.get("JQUANTS_REQUEST_INTERVAL", "0.2"))
MAX_RETRIES = 3


class JQuantsClient:
    def __init__(self):
        self.session = requests.Session()
        self.id_token = self._get_id_token()

    def _get_refresh_token(self) -> str:
        refresh_token = os.environ.get("JQUANTS_REFRESH_TOKEN")
        if refresh_token:
            return refresh_token

        mail = os.environ.get("JQUANTS_MAILADDRESS")
        password = os.environ.get("JQUANTS_PASSWORD")
        if not mail or not password:
            raise RuntimeError(
                "認証情報が設定されていません。環境変数 JQUANTS_REFRESH_TOKEN、"
                "または JQUANTS_MAILADDRESS と JQUANTS_PASSWORD を設定してください。"
            )
        resp = self.session.post(
            f"{JQUANTS_BASE_URL}/token/auth_user",
            json={"mailaddress": mail, "password": password},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["refreshToken"]

    def _get_id_token(self) -> str:
        refresh_token = self._get_refresh_token()
        resp = self.session.post(
            f"{JQUANTS_BASE_URL}/token/auth_refresh",
            params={"refreshtoken": refresh_token},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["idToken"]

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        headers = {"Authorization": f"Bearer {self.id_token}"}
        last_exc = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.get(
                    f"{JQUANTS_BASE_URL}{path}", headers=headers, params=params, timeout=30
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

    def get_listed_info(self) -> list:
        data = self._get("/listed/info")
        return data.get("info", [])

    def get_daily_quotes(self, date: str) -> list:
        """指定日の全銘柄の株価を取得する（ページネーション対応）。"""
        results = []
        params = {"date": date}
        while True:
            data = self._get("/prices/daily_quotes", params=params)
            results.extend(data.get("daily_quotes", []))
            key = data.get("pagination_key")
            if not key:
                break
            params["pagination_key"] = key
        return results

    def get_statements(self, code: str) -> list:
        data = self._get("/fins/statements", params={"code": code})
        return data.get("statements", [])


def latest_business_day_quotes(client: JQuantsClient, max_lookback: int = 10):
    """直近で株価データが取得できる営業日をさかのぼって探す。"""
    d = dt.date.today()
    for _ in range(max_lookback):
        date_str = d.strftime("%Y-%m-%d")
        quotes = client.get_daily_quotes(date_str)
        if quotes:
            return date_str, quotes
        d -= dt.timedelta(days=1)
    raise RuntimeError("直近の株価データが見つかりませんでした。")


def to_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass
class ScreenResult:
    code: str
    name: str
    market: str
    close: float
    market_cap: float
    per: float
    pbr: float
    net_cash_ratio: float
    operating_cf: float
    net_income: float


def latest_annual_statement(statements: list) -> Optional[dict]:
    """通期決算（FY）の開示のうち最新のものを返す。"""
    annual = [
        s
        for s in statements
        if "FY" in (s.get("TypeOfDocument") or "") or s.get("TypeOfCurrentPeriod") == "FY"
    ]
    candidates = annual if annual else statements
    candidates = [s for s in candidates if s.get("DisclosedDate")]
    if not candidates:
        return None
    candidates.sort(key=lambda s: s["DisclosedDate"], reverse=True)
    return candidates[0]


def shares_outstanding_from_statement(statement: dict) -> Optional[float]:
    issued = to_float(
        statement.get(
            "NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYearIncludingTreasuryStock"
        )
    )
    if issued is None:
        return None
    treasury = to_float(statement.get("NumberOfTreasuryStockAtTheEndOfFiscalYear")) or 0.0
    return issued - treasury


def evaluate(code, name, market, close, shares_outstanding, statement) -> Optional[ScreenResult]:
    net_income = to_float(statement.get("Profit"))
    operating_cf = to_float(statement.get("CashFlowsFromOperatingActivities"))
    eps = to_float(statement.get("EarningsPerShare"))
    bps = to_float(statement.get("BookValuePerShare"))
    cash = to_float(statement.get("CashAndEquivalents"))
    total_assets = to_float(statement.get("TotalAssets"))
    equity = to_float(statement.get("Equity"))

    required = (net_income, operating_cf, eps, bps, cash, total_assets, equity, close, shares_outstanding)
    if any(v is None for v in required):
        return None
    if shares_outstanding <= 0 or close <= 0:
        return None

    market_cap = close * shares_outstanding
    if market_cap <= 0:
        return None

    if net_income <= 0 or operating_cf <= 0:
        return None
    if eps <= 0 or bps <= 0:
        return None

    per = close / eps
    pbr = close / bps

    total_liabilities = total_assets - equity
    net_cash = cash - total_liabilities
    net_cash_ratio = net_cash / market_cap

    if net_cash_ratio < NET_CASH_RATIO_MIN:
        return None
    if per > PER_MAX:
        return None
    if pbr > PBR_MAX:
        return None
    if market_cap > MARKET_CAP_MAX:
        return None

    return ScreenResult(
        code=code,
        name=name,
        market=market,
        close=close,
        market_cap=market_cap,
        per=round(per, 2),
        pbr=round(pbr, 2),
        net_cash_ratio=round(net_cash_ratio, 3),
        operating_cf=operating_cf,
        net_income=net_income,
    )


def write_csv(path: str, results: list) -> None:
    fieldnames = [
        "code", "name", "market", "close", "market_cap",
        "per", "pbr", "net_cash_ratio", "operating_cf", "net_income",
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow(asdict(r))


def write_github_summary(quote_date: str, results: list) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as f:
        f.write(f"# JPXスクリーニング結果 ({quote_date})\n\n")
        f.write(
            f"条件: ネットキャッシュ比率>={NET_CASH_RATIO_MIN} / PER<={PER_MAX}倍 / "
            f"PBR<={PBR_MAX}倍 / 営業CF黒字 / 純利益黒字 / "
            f"時価総額<={MARKET_CAP_MAX / 1e8:.0f}億円\n\n"
        )
        f.write(f"該当銘柄数: {len(results)}\n\n")
        if results:
            f.write("| コード | 銘柄名 | 市場 | 株価 | 時価総額(億円) | PER | PBR | ネットキャッシュ比率 |\n")
            f.write("|---|---|---|---|---|---|---|---|\n")
            for r in results:
                f.write(
                    f"| {r.code} | {r.name} | {r.market} | {r.close:.1f} | "
                    f"{r.market_cap / 1e8:.1f} | {r.per} | {r.pbr} | {r.net_cash_ratio} |\n"
                )


def main() -> None:
    print("J-Quants APIへ認証しています...")
    client = JQuantsClient()

    print("上場銘柄一覧を取得しています...")
    listed = client.get_listed_info()
    targets = [s for s in listed if s.get("MarketCode") in TARGET_MARKET_CODES]
    print(f"対象銘柄数: {len(targets)}")

    print("直近営業日の株価を取得しています...")
    quote_date, quotes = latest_business_day_quotes(client)
    print(f"基準日: {quote_date}")

    close_by_code = {}
    for q in quotes:
        close = to_float(q.get("Close"))
        if close is not None:
            close_by_code[q["Code"]] = close

    results = []
    total = len(targets)
    for i, s in enumerate(targets, 1):
        code = s.get("Code")
        name = s.get("CompanyName")
        market = s.get("MarketCodeName")
        close = close_by_code.get(code)
        if close is None:
            continue

        try:
            statements = client.get_statements(code)
        except requests.HTTPError as e:
            print(f"  [警告] {code} {name}: 決算データ取得失敗 ({e})", file=sys.stderr)
            continue
        finally:
            time.sleep(REQUEST_INTERVAL_SEC)

        statement = latest_annual_statement(statements)
        if not statement:
            continue

        shares = shares_outstanding_from_statement(statement)
        if shares is None:
            continue

        result = evaluate(code, name, market, close, shares, statement)
        if result:
            results.append(result)
            print(f"  該当: {code} {name}")

        if i % 200 == 0:
            print(f"  進捗: {i}/{total}")

    results.sort(key=lambda r: r.net_cash_ratio, reverse=True)

    os.makedirs("results", exist_ok=True)
    dated_path = os.path.join("results", f"screener_{quote_date.replace('-', '')}.csv")
    latest_path = os.path.join("results", "screener_latest.csv")
    write_csv(dated_path, results)
    write_csv(latest_path, results)

    print(f"\n条件に合致した銘柄数: {len(results)}")
    print(f"結果を保存しました: {dated_path}")

    write_github_summary(quote_date, results)


if __name__ == "__main__":
    main()
