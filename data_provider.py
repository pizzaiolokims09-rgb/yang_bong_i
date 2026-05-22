import yfinance as yf
import pandas as pd
import logging

class MarketDataProvider:
    def __init__(self):
        # 9개 자산 및 주요 매크로 지표 티커 (Yahoo Finance 기준)
        self.tickers = {
            "NIKKEI225": "^N225",
            "CSI300": "000300.SS",
            "K200TR": "^KS200", # Placeholder for TR index
            "NASDAQ100": "^NDX",
            "NIFTY50": "^NSEI",
            "DIVIDEND_DOW": "SCHD", # U.S. Dividend Proxy
            "GOLD": "GC=F",
            "US_10Y": "^TNX",
            "DXY": "DX-Y.NYB", # Dollar Index
            "VIX": "^VIX"
        }
        # 폭락 감지에서 제외할 매크로/시장 지표 (VIX 하락은 오히려 호재임)
        self.macro_indicators = ["US_10Y", "DXY", "VIX"]

    async def get_latest_data(self, bot=None):
        logging.info("시장 데이터 수집 및 1차 검증(Sanity Check) 중...")
        data = {}
        suspicious_assets = []
        
        for name, ticker in self.tickers.items():
            try:
                asset = yf.Ticker(ticker)
                hist = asset.history(period="5d")
                if not hist.empty:
                    current_price = hist['Close'].iloc[-1]
                    prev_price = hist['Close'].iloc[-2] if len(hist) > 1 else current_price
                    change_1d = (current_price / prev_price - 1) * 100 if prev_price > 0 else 0
                    
                    data[name] = {
                        "price": current_price,
                        "change_1d": change_1d,
                        "is_real_crash": False
                    }
                    
                    # 1차 감지 (Suspicion) - 매크로 지표는 제외
                    if name in self.macro_indicators:
                        continue

                    threshold = 10.0 if "GOLD" in name else 7.0
                    # [수정] abs()를 제거하여 폭락(-7% 이하) 상황에서만 Suspicion이 발동하도록 수정
                    if change_1d <= -threshold:
                        logging.warning(f"⚠️ [Suspicion] {name} 폭락 위험 감지 (변동률: {change_1d:.2f}%). 배드 틱 여부 검증 시작.")
                        suspicious_assets.append((name, ticker, prev_price, change_1d))
            except Exception as e:
                logging.error(f"Error fetching {name}: {e}")
                data[name] = {"price": 0, "error": str(e), "is_real_crash": False}
                
        # 2차 시간 지연 검증 (Time-delay Verification)
        if suspicious_assets:
            logging.info("⏳ 데이터 이상 의심 상태 발동: 120초(2분) 대기 후 교차 검증을 시작합니다.")
            import asyncio
            await asyncio.sleep(120)
            
            for name, ticker, prev_price, original_change in suspicious_assets:
                asset = yf.Ticker(ticker)
                hist = asset.history(period="1d")
                if not hist.empty:
                    new_price = hist['Close'].iloc[-1]
                    new_change = (new_price / prev_price - 1) * 100
                    
                    # 2분 후에도 폭락(-7% 이하)이 유지되면 실제 상황으로 간주
                    if new_change <= -7.0:
                        logging.critical(f"🚨 [실제 폭락 판명] {name} 여전히 {new_change:.2f}% 폭락 유지 중!")
                        data[name]["is_real_crash"] = True
                        data[name]["price"] = new_price
                        data[name]["change_1d"] = new_change
                        # [수정] 직접 알림 대신 main.py의 필터링 로직에 위임합니다.
                    else:
                        logging.info(f"✅ [오류 판명] {name} 정상 가격으로 회귀. 배드 틱(Bad Tick)이었습니다.")
                        data[name]["price"] = new_price
                        data[name]["change_1d"] = new_change
                        if bot:
                            await bot.send_notification(f"⚠️ **[데이터 오류]**\n{name} 종목에서 일시적인 시세 오류가 감지되어 2분 대기 후 정상 가격으로 복구했습니다.")

        # Additional Macro estimation
        data["Macro_Context"] = {
            "Trend": "Side-way",
            "Sentiment": "Neutral"
        }
        return data
