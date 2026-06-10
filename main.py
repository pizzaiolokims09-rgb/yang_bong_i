import logging
import asyncio
from telegram_ui import YangbongBot
from broker_kis import KISBroker, KISAPIError
from council import MultiAssetCouncil
from data_provider import MarketDataProvider
from state_manager import StateManager
from config import INITIAL_WEIGHTS, TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, KIS_API_KEY, KIS_API_SECRET, KIS_CANO, KIS_ACNT_PRDT_CD, KIS_IS_PAPER

from rebalancer import Rebalancer
from chat_parser import ChatParser
from trend_hunter import TrendHunter
from market_sentinel import MarketSentinel
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timedelta
import pytz

# Configure Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

async def handle_api_error(e, broker, bot, state_manager=None):
    logging.error(f"⚠️ [API 통신 장애 감지] KISAPIError: {e}")
    kst = pytz.timezone('Asia/Seoul')
    now = datetime.now(kst)
    
    # KIS API 연속 실패 처리
    failures = 0
    if state_manager:
        # [수정] 이미 쿨다운 중이면 카운터를 추가로 올리지 않음 (카운터 무한 누적 방지)
        if state_manager.is_kis_in_cooldown():
            logging.info("⚠️ KIS API 쿨다운 중. 추가 알림 생략.")
        else:
            failures = state_manager.record_kis_failure()
            if failures >= 3:
                state_manager.set_kis_cooldown(900)  # 15분 쿨다운
                msg = f"⚠️ KIS API 연속 {failures}회 실패. 15분 동안 API 호출을 차단합니다."
                logging.warning(msg)
                await bot.send_notification(msg)
            else:
                logging.warning(f"⚠️ [일시적 KIS 오류] {failures}회 연속 실패 중. (3회 미만이면 알림 생략)")
    
    is_night_maintenance = False
    if now.hour == 23 and now.minute >= 30:
        is_night_maintenance = True
    elif 0 <= now.hour < 6:
        is_night_maintenance = True
        
    if is_night_maintenance:
        if now.hour == 23:
            target = (now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
        else:
            target = now.replace(hour=8, minute=0, second=0, microsecond=0)
            
        sleep_seconds = (target - now).total_seconds()
        msg = f"🛠️ [API 통신 장애] 증권사 서버 점검이 감지되었습니다.\n다음 날 아침 8시까지 봇을 수면 모드(Hibernation)로 전환합니다.\n(예상 대기: {sleep_seconds/3600:.1f}시간)"
        logging.warning(msg)
        await bot.send_notification(msg)
        
        await asyncio.sleep(sleep_seconds)
        
        logging.info("☀️ 기상 완료! KIS 토큰을 즉각 재발급받으며 세션을 복구합니다...")
        broker.auth()
        if state_manager:
            state_manager.record_kis_success()
        await bot.send_notification("☀️ [수면 해제 & 세션 복구 완료] 양봉이가 기상하여 접속 토큰을 갱신하고 정상 작동을 재개합니다.")
    else:
        # 야간 점검이 아닌 일반 주간 장애 시
        if state_manager and state_manager.is_kis_in_cooldown():
            logging.info("⚠️ KIS API 쿨다운 중. 추가 알림 생략.")
        else:
            # 쿨다운 상태가 아닌 단순 실패 시 (3회 미만)
            logging.warning("⚠️ [일시적 서버 과부하] KIS API 오류 발생. (텔레그램 알림 생략)")


async def run_market_surveillance(state_manager, broker, data_provider, council, bot, rebalancer):
    """15분마다 돌아가는 시장 가격 모니터링 및 배당금(DRIP) 스나이핑 백그라운드 태스크"""
    try:
        if state_manager.state.get("is_emergency_stop", False):
            logging.warning("🚨 [긴급] 패닉 모드가 활성화되어 있습니다. 감시 태스크를 스킵합니다.")
            return
        
        # [패치] KIS API 쿨다운 중이면 태스크 스킵 (불필요한 재시도 방지)
        if state_manager.is_kis_in_cooldown():
            logging.info("⚠️ KIS API 쿨다운 중. 시장 감시 태스크를 스킵합니다.")
            return
            
        logging.info("🔍 [스케줄링 태스크] 정기 시장 감시 및 잔고 추적을 시작합니다...")
        market_data = await data_provider.get_latest_data(bot)
        balance_data = broker.get_balance()
        
        # --- 🎯 [Smart DRIP] 배당금 감지 로직 (하드코딩 방식 - AI 호출 없음) ---
        current_cash = balance_data.get("cash", 0)
        last_cash = state_manager.state.get("last_cash_balance", current_cash)
        cash_diff = current_cash - last_cash
        
        if cash_diff >= 10000:
            logging.info(f"💰 현금 급증 감지: +{cash_diff:,}원 (배당금 등)")
            # [패치] AI 호출 제거: 목표 비중 대비 가장 부족한 종목에 자동 투자 (하드코딩)
            from config import ASSET_TICKERS
            config_assets = state_manager.portfolio_config.get("assets", [])
            holdings = {item.get("pdno", ""): float(item.get("evlu_amt", 0)) for item in balance_data.get("stocks", [])}
            total_value = balance_data.get("total_value", 1)
            
            # 각 종목의 목표 비중 vs 현재 비중 차이를 계산하여 가장 부족한 종목 선택
            best_target = None
            best_gap = -999
            for asset in config_assets:
                ticker = ASSET_TICKERS.get(asset["name"])
                if not ticker:
                    continue
                target_pct = asset.get("target_weight", 0)
                current_val = holdings.get(ticker, 0)
                current_pct = (current_val / total_value * 100) if total_value > 0 else 0
                gap = target_pct - current_pct  # 양수이면 부족
                if gap > best_gap:
                    best_gap = gap
                    best_target = {"name": asset["name"], "ticker": ticker}
            
            if best_target and best_gap > 0.5:  # 0.5% 이상 부족한 경우만 매수
                ticker = best_target["ticker"]
                price = broker.get_price(ticker)
                if price > 0:
                    qty = int(cash_diff / price)
                    if qty > 0:
                        logging.info(f"[Smart DRIP] {best_target['name']} {qty}주 매수 스나이핑 실행")
                        broker.place_order(ticker, qty, "BUY")
                        await bot.send_notification(
                            f"🎯 [DRIP 스나이핑]\n\n"
                            f"새로운 현금(배당금) {cash_diff:,}원이 입금되었습니다!\n"
                            f"목표 비중 대비 가장 부족한 {best_target['name']} {qty}주를 추가 매수했습니다."
                        )
        
        # 현금 잔고 업데이트
        state_manager.state["last_cash_balance"] = balance_data.get("cash", 0)
        state_manager.save_state()
        
        # --- 시장 이상 감지 (Crash Check) - 하드코딩 안전장치만 작동 (패치: AI 호출 제거) ---
        from config import ASSET_TICKERS
        name_map = {
            "NIKKEI225": "일본니케이225", "CSI300": "차이나CSI300", "K200TR": "200TR",
            "NASDAQ100": "미국나스닥100", "NIFTY50": "인도Nifty", "DIVIDEND_DOW": "미국배당다우존스",
            "GOLD": "ACE KRX금현물"
        }

        for eng_name, info in market_data.items():
            if isinstance(info, dict) and info.get("is_real_crash", False):
                kor_name = name_map.get(eng_name)
                ticker = ASSET_TICKERS.get(kor_name) if kor_name else None
                
                if ticker and not state_manager.is_ticker_in_crisis(ticker):
                    await bot.send_notification(
                        f"🚨 [시장 긴급 상황 발생]\n\n"
                        f"실제 자산인 {kor_name}({ticker}) 종목의 폭락이 2차 검증을 통해 확인되었습니다.\n"
                        f"즉시 보유 비중 50%를 매도(브레이크)하고 위기 모드로 전환합니다.\n"
                        f"(포트폴리오 재배치는 오후 3시 정규 리밸런싱 시점에 AI 위원회가 수행합니다.)"
                    )
                    
                    logging.critical(f"🚨 [변동성 브레이크] {kor_name}({ticker}) 폭락 감지! 브레이크를 가동합니다.")
                    state_manager.set_crisis_mode(ticker, True)
                    await rebalancer.execute_emergency_brake(ticker, kor_name)
                    # [패치] AI 위원회 긴급 소집 제거 - 브레이크만 작동시키고, 재배치는 15시 정규 리밸런싱에 위임
                    logging.info(f"✅ [변동성 브레이크 완료] {kor_name} 50% 매도 실행. 재배치는 15시 정규 리밸런싱에서 수행됩니다.")
        
        # 스케줄 종료 후 메모리 최적화
        state_manager.optimize_memory()
            
    except KISAPIError as e:
        await handle_api_error(e, broker, bot, state_manager)
    except Exception as e:
        logging.error(f"Error in market surveillance task: {e}")

async def run_daily_rebalance(state_manager, broker, data_provider, council, bot, rebalancer):
    """매일 새벽 4시에 단 1번 실행되는 정규 리밸런싱 태스크"""
    try:
        if state_manager.state.get("is_emergency_stop", False):
            return
            
        logging.info("👔 [스케줄링 태스크] 매일 1회 정규 AI 위원회를 소집합니다...")
        market_data = await data_provider.get_latest_data(bot)
        balance_data = broker.get_balance()
        
        decision = await council.generate_rebalance_decision(state_manager, market_data, balance_data)
        
        # 모든 결정 사항(REBALANCE, HOLD, CRISIS_RESOLVED)에 대해 AI 회의록 저장
        if decision:
            await state_manager.add_meeting_record(
                decision['minutes'], 
                summarizer_fn=council.summarize_instructions
            )

        if decision["action"] == "REBALANCE":
            logging.info(f"Rebalancing Needed! Minutes: {decision['minutes']}")
            state_manager.update_portfolio_config({"assets": decision["weights"]})
            
            if state_manager.state.get("is_auto_mode", False):
                logging.info("Auto-mode: Executing rebalancing...")
                orders, tlh_assets, skipped_dust_assets, price_zero_assets = rebalancer.calculate_orders(decision["weights"])
                await rebalancer.execute_rebalancing(orders, tlh_assets)
                
                # [신규] 더스트 패스 및 전체 시드 상황 안내 (항상 보고)
                try:
                    min_seed_req = rebalancer.calculate_minimum_required_seed(decision["weights"])
                    bal = broker.get_balance()
                    short_amount = max(0, min_seed_req - bal["total_value"])
                    
                    report_msg = (f"📅 **[일일 AI 위원회 회의 결과 보고 (리밸런싱 완료)]**\n\n"
                                  f"{decision['minutes']}\n\n"
                                  f"━━━━━━━━━━━━━━━━━━\n\n")
                    if skipped_dust_assets:
                        dust_names = [a["name"] for a in skipped_dust_assets]
                        report_msg += (f"💡 **[1주 단위 매매: 잔차(Dust) 스킵 안내]**\n"
                                       f"배분 금액이 ETF 1주 단가보다 모자라, 다음 종목들은 매수 보류(Pass) 처리하였습니다:\n"
                                       f"👉 `{', '.join(dust_names)}`\n\n")
                    
                    report_msg += (f"💰 **[포트폴리오 필요 자금 현황]**\n"
                                   f"가장 비싼 주가/가장 적은 비중을 기준으로, 모든 종목을 비율대로 담기 위한 계좌 최소 규모는 약 **{min_seed_req:,.0f}원**입니다.\n")
                    
                    if short_amount > 0:
                        report_msg += f"👉 따라서 현재 자산에서 약 **{short_amount:,.0f}원**을 추가 입금하시면 모든 톱니바퀴가 완벽히 맞물려 리밸런싱됩니다."
                    else:
                        report_msg += "✅ **현재 계좌 규모는 완벽한 리밸런싱을 수행하기에 충분히 여유롭습니다!**"
                    
                    # [신규] 자동 매매 이후 수익률 기록 (seed가 0보다 커야 함)
                    seed = state_manager.state.get("seed_amount", 0)
                    if seed > 0:
                        p_rate = ((bal["total_value"] - seed) / seed) * 100
                        state_manager.add_profit_history(p_rate)
                        
                    await bot.send_notification(report_msg)
                except Exception as e:
                    logging.error(f"리밸런싱 완료 알림 전송 중 에러 발생: {e}")
            else:
                report_msg = (f"📅 **[일일 AI 위원회 회의 결과 보고 (수동 모드)]**\n\n"
                              f"비중 조절이 권고되었습니다. (수동 모드이므로 자동 매매되지 않습니다.)\n\n"
                              f"💡 **위원회 판단 근거**:\n{decision['minutes']}")
                await bot.send_notification(report_msg)
        elif decision["action"] == "CRISIS_RESOLVED":
            logging.info("🌞 [브레이크 해제] AI 위원회가 시장 안정을 선포했습니다. 모든 위기 모드를 해제합니다.")
            state_manager.state["crisis_tickers"] = []
            state_manager.save_state()
            await bot.send_notification("🌞 **[변동성 브레이크 해제]**\n\nAI 위원회가 시장의 변동성이 잦아들고 추세가 회복되었다고 판단했습니다. 이제 모든 종목의 추가 매수 차단이 해제되었으며, 정상적인 리밸런싱 모드로 복구되었습니다.")
        elif decision["action"] == "ERROR":
            logging.error(f"AI 위원회 분석 실패: {decision['minutes']}")
            report_msg = (f"🚨 **[AI 위원회 통신 장애]**\n\n"
                          f"회장님, 현재 구글 Gemini AI 서버와 통신이 원활하지 않아 3번의 재시도(Flash 모델 우회 포함)에도 불구하고 리밸런싱 분석을 완료하지 못했습니다.\n\n"
                          f"💡 **장애 원인**:\n{decision['minutes']}\n\n"
                          f"기존 포트폴리오 비중을 유지(HOLD)하며, 다음 스케줄에 다시 시도하겠습니다.🙇‍♂️")
            await bot.send_notification(report_msg)
        else:
            logging.info(f"Holding. Reason: {decision['minutes']}")
            # [패치] 쿨다운 스킵과 정상 HOLD를 구분하여 알림
            is_cooldown_skip = "쿨다운" in decision.get("minutes", "") or "할당량" in decision.get("minutes", "")
            if is_cooldown_skip:
                report_msg = (f"⏸️ [AI 위원회 일시 중단]\n\n"
                              f"Gemini API 서버가 일시적으로 응답하지 않아(Rate Limit) 이번 회차 분석을 건너뛰었습니다.\n"
                              f"기존 포트폴리오 비중을 유지(HOLD)하며, 5분 후 자동 복구됩니다.\n\n"
                              f"(일일 호출 한도 문제가 아닌 Gemini 서버 측 순간 트래픽 제한입니다.)")
            else:
                report_msg = (f"📅 [일일 AI 위원회 회의 결과 보고]\n\n"
                              f"AI 위원회의 분석 결과, 현재 포트폴리오 비중이 목표치와 매우 일치하여 이번 회차는 매매 없이 'HOLD(관망)'를 결정하였습니다.\n\n"
                              f"💡 위원회 판단 근거:\n{decision['minutes']}")
            await bot.send_notification(report_msg)
            
        # 스케줄 종료 후 메모리 최적화
        state_manager.optimize_memory()
            
    except KISAPIError as e:
        await handle_api_error(e, broker, bot, state_manager)
    except Exception as e:
        logging.error(f"Error in daily rebalance task: {e}")


async def run_sentinel_scan(sentinel, state_manager, bot):
    """장 중 15분 간격으로 실행되는 시장 구조 감시 태스크"""
    try:
        logging.info("🛡️ [MarketSentinel] 시장 구조적 위험 신호 스캔 시작...")
        scan_result = await sentinel.run_full_scan(state_manager, bot)

        alerts = scan_result.get("alerts_sent", 0)
        exposure = scan_result.get("dynamic_exposure", {})
        exposure_pct = exposure.get("max_exposure_pct", 100) if exposure else 100

        logging.info(
            f"🛡️ [MarketSentinel] 스캔 완료. "
            f"알림 발송: {alerts}건, "
            f"가변 비중: {exposure_pct}%"
        )
    except KISAPIError as e:
        logging.error(f"⚠️ [MarketSentinel] KIS API 오류로 스캔 스킵: {e}")
        await bot.send_notification(
            f"⚠️ **[MarketSentinel API 오류]**\n"
            f"한투 거래소가 점검중이거나 장마감 시간이어서 API 호출이 실패했습니다.\n"
            f"사유: {str(e)[:200]}"
        )
    except Exception as e:
        logging.error(f"❌ [MarketSentinel] 스캔 중 예외 발생: {e}")


async def send_expiry_alerts(bot):
    """만기일 알림을 확인하고 해당하는 경우 텔레그램으로 발송합니다."""
    try:
        kst = pytz.timezone('Asia/Seoul')
        today = datetime.now(kst).date()
        expiry_dates = MarketSentinel.get_next_expiry_dates(months_ahead=2)

        for expiry in expiry_dates:
            days_diff = (expiry["date"] - today).days

            # 2일 전, 1일 전, 당일 알림
            if days_diff in [2, 1, 0]:
                msg = MarketSentinel.format_expiry_alert(expiry, days_diff)
                await bot.send_notification(msg)
                logging.info(f"📅 [만기일 알림] {expiry['label']} (D-{days_diff})")
                break  # 가장 가까운 만기일만 알림

    except Exception as e:
        logging.error(f"❌ [만기일 알림] 오류: {e}")

async def retry_failed_orders(broker, state_manager, bot):
    """15분 간격 앰뷸런스 로직 (실패한 매매 재시도)"""
    from datetime import datetime
    import pytz
    
    kst = pytz.timezone('Asia/Seoul')
    now = datetime.now(kst)
    # 장 마감 15:30 경과 후 남은 대기열 초기화
    if now.hour >= 15 and now.minute >= 30:
        if state_manager.get_retry_orders():
            logging.info("장 마감 15:30 경과. 오늘 종가 동시호가로도 구출하지 못한 희귀 잔여 대기열을 초기화합니다.")
            state_manager.clear_retry_orders()
        return
        
    # 정규장 내(09:00~15:30)에만 구출 시도
    if not (9 <= now.hour <= 15):
        return

    orders = state_manager.get_retry_orders()
    if not orders:
        return
        
    is_closing_auction = (now.hour == 15 and now.minute >= 20)
    if is_closing_auction:
        logging.critical(f"🎯 [동시호가 100% 체결 투하] 15:20 장마감 동시호가에 진입했습니다! 대기열의 {len(orders)}건을 종가 체결로 밀어넣습니다!")
    else:
        logging.info(f"🚑 [주문 앰뷸런스 출동] 실패 병동에 입원 중인 {len(orders)}건의 튕긴 주문을 재투입해봅니다.")
        
    for order in list(orders):
        ticker = order["ticker"]
        name = order["name"]
        quantity = order["quantity"]
        side = order["side"]
        order_type = order.get("order_type", "01")
        placed_at = order.get("placed_at")
        
        # [신규] 15분 경과 지정가 주문 강제 구출 로직
        should_force_market = False
        if order_type == "00" and placed_at:
            now_kst = datetime.now(kst)
            placed_dt = datetime.fromisoformat(placed_at)
            # placed_at이 이미 KST aware라고 가정
            if placed_dt.tzinfo is None:
                placed_dt = kst.localize(placed_dt)
                
            elapsed = (now_kst - placed_dt).total_seconds()
            if elapsed >= 900: # 15분(900초) 경과
                should_force_market = True
                
        try:
            if should_force_market:
                logging.info(f"🚑 [지정가 타임아웃] {name} 종목이 15분간 체결되지 않아 취소 후 시장가로 강제 전환합니다.")
                # 1. 기존 지정가 주문 취소
                if order.get("odno") and order.get("org_no"):
                    broker.cancel_order(order["odno"], order["org_no"])
                # 2. 시장가로 재투입
                res = broker.place_order(ticker, quantity, side, order_type="01")
                msg = f"🎯 **[지정가->시장가 강제체결]**\n15분간 미체결된 **{name}** 지정가 주문을 취소하고, 시장가로 즉시 전환하여 체결을 완료했습니다!"
            elif is_closing_auction:
                res = broker.place_order(ticker, quantity, side, order_type="01")
                msg = f"🎯 **[동시호가 구출 완료]**\n장마감 동시호가를 통해 **{name}({ticker})** {quantity}주 {side} 체결을 100% 보장 상태로 전송 완료했습니다! (대기열 삭제)"
            else:
                # 일반 재시도 (기존 방식 유지하되 시장가로 우선 시도)
                res = broker.place_order(ticker, quantity, side, order_type="01")
                msg = f"🚑 **[종목 구출 성공!]**\n시장의 유동성 거절/에러를 뚫고 15분 재시도 앰뷸런스가 **{name}({ticker})** {quantity}주 {side} 체결을 무사히 관철시켰습니다! (입원 명단에서 비웁니다)"
            
            await bot.send_notification(msg)
            state_manager.remove_retry_order(ticker)
            import asyncio
            await asyncio.sleep(0.5)
        except Exception as e:
            logging.error(f"🚑 [구출 실패] {name}({ticker}) 재투입도 튕겼습니다. 원인: {str(e)}")
            # 실패 시 그대로 큐에 남겨둬 다음 사이클을 기다림

async def main():
    logging.info("Initializing Yangbong-i Bot (KIS + Gemini Version)...")
    
    # Initialize components
    state_manager = StateManager()
    broker = KISBroker(KIS_API_KEY, KIS_API_SECRET, f"{KIS_CANO}{KIS_ACNT_PRDT_CD}", KIS_IS_PAPER)
    data_provider = MarketDataProvider()
    council = MultiAssetCouncil(GEMINI_API_KEY, state_manager)
    chat_parser = ChatParser(GEMINI_API_KEY, state_manager)
    trend_hunter = TrendHunter(GEMINI_API_KEY, state_manager)
    rebalancer = Rebalancer(broker, state_manager)
    sentinel = MarketSentinel(broker)  # 신규: 시장 구조 감시 모듈
    
    bot = YangbongBot(
        TELEGRAM_BOT_TOKEN, 
        state_manager, 
        broker, 
        council, 
        data_provider,
        chat_parser,
        rebalancer,
        trend_hunter
    )
    
    # 의존성 브로커 내부 주입 (Rate Limiter 연동용)
    broker.state_manager = state_manager
    broker.bot = bot
    
    # Auth broker
    broker.auth()
    
    # 텔레그램 봇 비동기 실행 (이벤트 기반 대기)
    await bot.start_polling()
    
    # K-API를 통한 데이터 강제 동기화 (복구 및 안전 부팅)
    await rebalancer.boot_sequence_audit()
    
    logging.info("Yangbong-i is ready! Switching to Event-driven Async architecture.")
    
    # CPU 점유율 극소화를 위한 APScheduler 세팅 (한국 표준시 기준)
    scheduler = AsyncIOScheduler(timezone=pytz.timezone('Asia/Seoul'))
    
    # [신규] 15분 간격 앰뷸런스 매매 재시도 스케줄 (가장 높은 우선도)
    scheduler.add_job(
        retry_failed_orders,
        trigger='cron',
        minute='*/15',
        second=10,  # [패치] API 병목 방지를 위해 10초 딜레이
        day_of_week='mon-fri',
        args=[broker, state_manager, bot]
    )
    
    # 1. 시장 감시 및 DRIP 태스크 (한국 장 평일 09:00 ~ 14:45, 15분 간격 실행)
    scheduler.add_job(
        run_market_surveillance, 
        'cron', 
        day_of_week='mon-fri',
        hour='9-14',
        minute='0,15,30,45',
        second=0,   # 정각 실행
        args=[state_manager, broker, data_provider, council, bot, rebalancer],
        id='market_surveillance_task'
    )
    
    # 2. 정규 리밸런싱 (한국 장 평일 마감 30분 전인 15:00 정각 1회 실행)
    scheduler.add_job(
        run_daily_rebalance, 
        'cron', 
        day_of_week='mon-fri',
        hour=15, 
        minute=0, 
        second=20,  # [패치] API 병목 방지를 위해 20초 딜레이
        args=[state_manager, broker, data_provider, council, bot, rebalancer],
        id='daily_rebalance_task'
    )
    
    # 3. [신규] MarketSentinel 시장 구조 감시 (장 중 09:05 ~ 15:20, 15분 간격)
    scheduler.add_job(
        run_sentinel_scan,
        'cron',
        day_of_week='mon-fri',
        hour='9-15',
        minute='5,20,35,50',
        args=[sentinel, state_manager, bot],
        id='sentinel_scan_task'
    )
    
    # 4. [신규] 만기일 알림 (2일 전/1일 전: 14:00, 당일: 08:30)
    scheduler.add_job(
        send_expiry_alerts,
        'cron',
        day_of_week='mon-fri',
        hour=14,
        minute=0,
        args=[bot],
        id='expiry_alert_afternoon'
    )
    scheduler.add_job(
        send_expiry_alerts,
        'cron',
        day_of_week='mon-fri',
        hour=8,
        minute=30,
        args=[bot],
        id='expiry_alert_morning'
    )
    
    scheduler.start()
    logging.info(
        "APScheduler has been started with Asia/Seoul timezone. "
        "Configured tasks: Surveillance(Mon-Fri 09-14:45, every 15m), "
        "Rebalance(Mon-Fri at 15:00 KST), "
        "Sentinel(Mon-Fri 09:05-15:20, every 15m), "
        "ExpiryAlert(Mon-Fri 08:30 & 14:00)."
    )
    
    # 메인 스레드를 무기한으로 잠재워 CPU 점유율을 0%에 가깝게 유지 (Event-driven)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
