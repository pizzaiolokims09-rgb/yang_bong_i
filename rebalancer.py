import logging
import time
import pytz
from config import ASSET_TICKERS

class Rebalancer:
    def __init__(self, broker, state_manager):
        self.broker = broker
        self.state_manager = state_manager

    async def boot_sequence_audit(self):
        import asyncio
        import datetime
        logging.info("=========================================")
        logging.info("🔄 [Boot Sequence Audit] 안전 부팅 복구 알고리즘 시작...")
        
        try:
            # 1. 미체결 주문(잔여 TWAP/일반) 전수 조사 및 일괄 취소
            unexecuted = self.broker.get_unexecuted_orders()
            if unexecuted:
                logging.info(f"방치된 미체결 주문 {len(unexecuted)}건을 발견했습니다. 전량 통제 취소(Cancel All)를 수행합니다.")
                for order in unexecuted:
                    odno = order.get("odno")
                    brno = order.get("ord_gno_brno")
                    prdt_name = order.get("prdt_name")
                    qty = order.get("rmn_qty")
                    if odno and brno:
                        self.broker.cancel_order(odno, brno)
                        logging.info(f"   => [취소 완료] {prdt_name} {qty}주 (주문번호: {odno})")
                        await asyncio.sleep(0.3)
            else:
                logging.info("방치된 미체결 주문이 없습니다.")
                
            # 2. 일일 체결 내역(Rate Limiter용) K-API 강제 동기화 
            logging.info("K-API에서 오늘치 자정~현재 누적 주문 데이터(Rate Limiter용)를 강제 동기화합니다.")
            today_count, today_vol = self.broker.get_today_orders_stats()
            
            kst = pytz.timezone('Asia/Seoul')
            today_str = datetime.datetime.now(kst).strftime("%Y-%m-%d")
            self.state_manager.state["last_order_date"] = today_str
            self.state_manager.state["today_orders_count"] = today_count
            self.state_manager.state["today_volume"] = today_vol
            self.state_manager.save_state()
            
            logging.info(f"✅ 동기화 완료: 오늘 체결 건수 {today_count}회, 누적 금액 {today_vol:,.0f}원")
            
            msg = "🔄 **[시스템 재구동 완료]**\n예기치 않은 시스템 종료로 인해 양봉이가 재시작되었습니다.\n\n증권사 서버의 미체결 주문을 싹 정리(Cancel All)하고, 당일 매매 횟수/잔고 동기화를 완벽히 마치고 정상 감시 모드에 돌입합니다!"
            if hasattr(self.broker, "bot") and self.broker.bot:
                asyncio.create_task(self.broker.bot.send_notification(msg))
                
        except Exception as e:
            logging.error(f"Boot sequence audit failed: {e}")
            if hasattr(self.broker, "bot") and self.broker.bot:
                asyncio.create_task(self.broker.bot.send_notification(f"⚠️ **[복구 실패]** 안전 부팅 중 오류가 발생했습니다: {e}"))
                
        logging.info("=========================================")

    def calculate_orders(self, target_weights):
        """
        목표 비중과 현재 잔고를 비교하여 매매 주문 목록 생성
        Tax-Loss Harvesting(TLH) 로직 포함
        """
        balance = self.broker.get_balance()
        total_eval = balance["total_value"]
        seed = self.state_manager.state["seed_amount"]
        
        # [신규] 기존 시드가 1주 단위 분할매수 최소 금액보다 작고, 계좌에 여유 현금(미할당 자산)이 있다면 자동으로 시드에 편입
        if seed > 0:
            min_seed_req = self.calculate_minimum_required_seed(target_weights)
            if seed < min_seed_req and total_eval > seed:
                available_unallocated = total_eval - seed
                amount_to_add = min(min_seed_req - seed, available_unallocated)
                new_seed = seed + amount_to_add
                logging.info(f"🔄 [시드 자동 증액] 최소 리밸런싱 필요액({min_seed_req:,.0f}원) 대비 시드({seed:,.0f}원)가 부족하여, 계좌 미할당 여유 자산 중 {amount_to_add:,.0f}원을 매매 시드로 자동 편입합니다. (변경 후 시드: {new_seed:,.0f}원)")
                self.state_manager.set_seed(new_seed)
                seed = new_seed
        
        investable_total = min(total_eval, seed) if seed > 0 else total_eval
        
        regular_orders = []
        skipped_dust_assets = []
        price_zero_assets = [] # [신규] 가격 조회 실패로 스킵된 종목 추적
        is_capital_gain_expected = False
        
        # 1. 일반 리밸런싱 주문 계산 및 수익 실현(매도) 여부 감지
        for asset_name, weight in target_weights.items():
            ticker = ASSET_TICKERS.get(asset_name)
            if not ticker:
                logging.error(f"❌ [티커 미매칭] {asset_name} 종목에 해당하는 티커(종목코드)를 찾을 수 없습니다. config.py를 확인하세요.")
                continue
            
            target_value = investable_total * (weight / 100)
            current_asset_info = balance["assets"].get(ticker, {"quantity": 0, "price": 0, "profit_rate": 0.0})
            
            # KISBroker는 price, quantity 필드를 반환함
            current_value = current_asset_info.get("quantity", 0) * current_asset_info.get("price", 0)
            
            diff_value = target_value - current_value
            price = self.broker.get_price(ticker)
            if price == 0:
                logging.error(f"⚠️ [가격 조회 실패] {asset_name}({ticker})의 현재가를 가져올 수 없어 리밸런싱에서 제외합니다. (거래소 시세 지연 혹은 종목 상태 재확인 요망)")
                price_zero_assets.append({"name": asset_name, "ticker": ticker})
                continue
            
            if abs(diff_value) > price: 
                quantity = int(diff_value / price)
                if quantity != 0:
                    side = "BUY" if quantity > 0 else "SELL"
                    
                    # 실현 수익 발생 여부 파악 (수익률 > 0 인 종목을 매도할 예정일 때)
                    if side == "SELL" and current_asset_info.get("profit_rate", 0.0) > 0:
                        is_capital_gain_expected = True
                    
                    # [모듈 9] 변동성 브레이크: 위기 종목은 매수(BUY) 원천 차단
                    if side == "BUY" and self.state_manager.is_ticker_in_crisis(ticker):
                        logging.warning(f"🚫 [브레이크 작동] {asset_name}({ticker}) 종목은 현재 위기 상태(CRISIS_MODE)이므로 추가 매수를 차단합니다.")
                        # 리스트에서 제거 (이번 리밸런싱에서 매수하지 않음)
                        continue
                    
                    regular_orders.append({
                        "ticker": ticker,
                        "name": asset_name,
                        "quantity": abs(quantity),
                        "side": side,
                        "type": "REGULAR"
                    })
            else:
                # 1주 단가에 비해 목표 매수 차액이 작아서 잔돈(Dust)으로 스킵되는 경우
                if diff_value > 0: # 사야(BUY) 하는 경우에만 기록
                    skipped_dust_assets.append({
                        "name": asset_name,
                        "ticker": ticker,
                        "price": price,
                        "diff_value": diff_value,
                        "weight": weight,
                        "is_held": (ticker in balance["assets"] and balance["assets"][ticker].get("quantity", 0) > 0)
                    })
        
        # [신규] 1.1 미등록 자산(Config에 없는 종목) 자동 전량 매도 처리
        config_tickers = [ASSET_TICKERS.get(name) for name in self.state_manager.portfolio_config.get("assets", {}).keys() if ASSET_TICKERS.get(name)]
        for ticker, info in balance["assets"].items():
            if ticker not in config_tickers and info.get("quantity", 0) > 0:
                logging.warning(f"🧹 [기타 자산 정리] 포트폴리오 설정에 없는 종목 발견: {info['name']}({ticker}) {info['quantity']}주 전량 매도 결정.")
                regular_orders.append({
                    "ticker": ticker,
                    "name": info["name"],
                    "quantity": info["quantity"],
                    "side": "SELL",
                    "type": "REGULAR"
                })
        
        sell_summary = [(o['name'], o['quantity']) for o in regular_orders if o['side'] == 'SELL']
        buy_summary = [(o['name'], o['quantity']) for o in regular_orders if o['side'] == 'BUY']
        logging.info(f"📋 [주문 계산 완료] SELL: {sell_summary}, BUY: {buy_summary}")

        tlh_assets = []
        # 2. 세금 최적화 (Tax-Loss Harvesting, TLH) 대상 스캔
        if is_capital_gain_expected:
            logging.info("수익 실현 매도 감지됨. 절세를 위한 손실 종목(TLH) 스캔을 시작합니다.")
            for ticker, info in balance["assets"].items():
                if info.get("profit_rate", 0.0) < 0 and info.get("quantity", 0) > 0:
                    logging.info(f"[TLH 대상 포착] {info['name']} ({ticker}) 손실률: {info['profit_rate']}%")
                    tlh_assets.append({
                        "ticker": ticker,
                        "name": info["name"],
                        "quantity": info["quantity"]
                    })
        
        return regular_orders, tlh_assets, skipped_dust_assets, price_zero_assets

    def calculate_minimum_required_seed(self, target_weights):
        """
        주어진 비중대로 모든 종목을 '최소 1주' 이상 동일 선상에 두기 위해 
        가장 비싼 주가/가장 적은 비중을 기준으로 이론적인 최소 포트폴리오(계좌) 규모를 구합니다.
        """
        required_minimum_eval = 0
        for asset_name, weight in target_weights.items():
            if weight <= 0: continue
            ticker = ASSET_TICKERS.get(asset_name)
            if not ticker: continue
            
            price = self.broker.get_price(ticker)
            if price == 0: continue
            
            # x * (weight/100) = price 
            asset_required_total = price / (weight / 100)
            
            if asset_required_total > required_minimum_eval:
                required_minimum_eval = asset_required_total
                
        return required_minimum_eval

    def calculate_emergency_orders(self, target_weights):
        """
        [긴급 매매 로직]
        실제 시장 폭락/급등 판명 시 호출됩니다.
        """
        logging.warning("🚨 [긴급 매매 모드] 자산 대피/재편성에 돌입합니다.")
        balance = self.broker.get_balance()
        total_eval = balance["total_value"]
        
        emergency_orders = []
        for asset_name, weight in target_weights.items():
            ticker = ASSET_TICKERS.get(asset_name)
            if not ticker: continue
            
            # [모듈 9] 위기 종목은 매수 금지
            if self.state_manager.is_ticker_in_crisis(ticker):
                logging.warning(f"🚫 [긴급-브레이크] {asset_name}({ticker}) 매수 시도 차단.")
                continue

            target_value = total_eval * (weight / 100)
            current_asset_info = balance["assets"].get(ticker, {"quantity": 0, "price": 0})
            current_value = current_asset_info.get("quantity", 0) * current_asset_info.get("price", 0)
            
            diff_value = target_value - current_value
            price = self.broker.get_price(ticker)
            if price == 0: continue
            
            quantity = int(diff_value / price)
            if quantity != 0:
                side = "BUY" if quantity > 0 else "SELL"
                # 위기 상태에서 실수로 매수가 생성되는 것 최종 방지
                if side == "BUY" and self.state_manager.is_ticker_in_crisis(ticker): continue
                
                emergency_orders.append({
                    "ticker": ticker,
                    "name": asset_name,
                    "quantity": abs(quantity),
                    "side": side,
                    "type": "EMERGENCY"
                })
        
        return emergency_orders

    async def execute_emergency_brake(self, ticker, asset_name):
        """
        [모듈 9] 변동성 브레이크: 보유 수량의 50% 즉시 시장가 매도
        """
        balance = self.broker.get_balance()
        asset_info = balance["assets"].get(ticker)
        
        if not asset_info or asset_info["quantity"] <= 0:
            logging.info(f"[브레이크] {asset_name}({ticker}) 보유 수량이 없어 매도를 생략합니다.")
            return

        sell_qty = max(1, asset_info["quantity"] // 2)
        logging.critical(f"🚨 [변동성 브레이크 집행] {asset_name}({ticker}) 보유량의 50%인 {sell_qty}주를 즉시 매도합니다.")
        
        res = self.broker.place_order(ticker, sell_qty, "SELL")
        
        msg = f"🚨 **[변동성 브레이크 발동]**\n\n{asset_name}({ticker}) 종목의 비정상적인 폭락(-7% 이상)이 확인되었습니다. 떨어지는 칼날을 피하기 위해 추가 매수를 전면 차단하고, 보유 물량의 절반({sell_qty}주)을 즉시 현금화하여 대피했습니다."
        if hasattr(self.broker, "bot") and self.broker.bot:
            await self.broker.bot.send_notification(msg)
            
        return res

    async def execute_rebalancing(self, orders, tlh_assets=None):
        """
        리밸런싱 집행 메인 함수.
        TLH 대상이 있을 경우 execute_tlh_sequence를 먼저 호출합니다.
        """
        results = []
        
        # [모듈 10] 절세 시퀀스 우선 실행
        if tlh_assets and len(tlh_assets) > 0:
            logging.info(f"💸 {len(tlh_assets)}개의 종목에 대해 3단계 절세 시퀀스(TLH)를 시작합니다.")
            tlh_results = await self.execute_tlh_sequence(tlh_assets, orders)
            results.extend(tlh_results)
            # TLH 시퀀스 내부에서 수익 실현 매도(STEP 3)까지 처리하므로, TLH가 처리한 종목의 SELL만 제거
            tlh_tickers = {a["ticker"] for a in tlh_assets}
            orders = [o for o in orders if not (o["side"] == "SELL" and o["ticker"] in tlh_tickers)]
            if not orders: return results

        logging.info("🔄 잔여 리밸런싱 주문 집행 시작 (SELL 우선 집행 로직 적용)...")
        
        # SELL / BUY 분리
        sell_orders = [o for o in orders if o["side"] == "SELL"]
        buy_orders = [o for o in orders if o["side"] == "BUY"]

        # --- 1. 매도(SELL) 주문 먼저 집행 ---
        logging.info(f"총 {len(sell_orders)}건의 SELL 주문 집행을 시작합니다.")
        sell_twap_tasks = []
        for order in sell_orders:
            price = self.broker.get_price(order["ticker"])
            total_amount = price * order["quantity"]
            
            logging.info(f"[일반/긴급 SELL] {order['name']} {order['side']} {order['quantity']}주 (약 {total_amount:,}원)")
                
            if total_amount >= 5000000 and order.get("type") in ["REGULAR", "EMERGENCY"]:
                logging.info(f"💡 대량 매도 감지! {order['name']} 종목에 대해 분할 매매(TWAP)를 가동합니다.")
                sell_twap_tasks.append(
                    self.broker.execute_twap_order(order["ticker"], order["quantity"], order["side"], duration_mins=10, chunks=5)
                )
            else:
                try:
                    res = self.broker.place_order(order["ticker"], order["quantity"], order["side"])
                    results.append(res)
                except Exception as e:
                    err_msg = f"⚠️ [매도 시장가 실패] {order['name']} 시장가 매도 실패: {str(e)}. 지정가(현재가)로 우회 시도합니다."
                    logging.warning(err_msg)
                    try:
                        price = self.broker.get_price(order["ticker"])
                        res_limit = self.broker.place_order(order["ticker"], order["quantity"], order["side"], order_type="00", price=price)
                        if res_limit.get("rt_cd") == "0":
                            order["order_type"] = "00"
                            order["price"] = price
                            order["odno"] = res_limit.get("output", {}).get("ODNO")
                            order["org_no"] = res_limit.get("output", {}).get("KRX_FWDG_ORD_ORGNO")
                            self.state_manager.add_retry_order(order)
                            await self.broker.bot.send_notification(f"🚑 **[지정가 우회]** {order['name']} 시장가 매도가 튕겨 현재가({price:,}원) 지정가로 전송했습니다. 15분 뒤에도 미체결 시 시장가로 재전환합니다.")
                        else:
                            raise Exception(res_limit.get("msg1", "지정가 주문도 거부됨"))
                    except Exception as e2:
                        final_err = f"🚨 **[매도 최종 실패]** {order['name']} 지정가 우회도 실패하여 대기열에만 등록합니다: {str(e2)}"
                        logging.error(final_err)
                        self.state_manager.add_retry_order(order)
                        if hasattr(self.broker, "bot") and self.broker.bot:
                            import asyncio
                            try: asyncio.create_task(self.broker.bot.send_notification(final_err))
                            except Exception: pass
                import asyncio
                await asyncio.sleep(0.5)

        if sell_twap_tasks:
            logging.info("⏳ SELL 분할 주문(TWAP)이 모두 완료될 때까지 대기합니다...")
            import asyncio
            twap_results = await asyncio.gather(*sell_twap_tasks)
            results.extend(twap_results)

        # SELL 이후 시간 딜레이 (예수금 동기화 확정 시간 확보)
        if sell_orders:
            import asyncio
            logging.info("⏳ 매도 주문 체결 및 예수금 동기화를 위해 잠시 대기합니다...")
            await asyncio.sleep(2)


        # --- 2. 매수(BUY) 전 잔고 Audit (교차 검증) ---
        if not buy_orders:
            return results
            
        logging.info("⚖️ 매수 집행 전 Sync Auditor 예수금 교차 검증을 시작합니다.")
        # 현재 진짜 예수금 조회
        try:
            current_balance = self.broker.get_balance()
            if not current_balance:
                raise Exception("API 응답 없음")
            available_cash = current_balance.get("cash", 0)
        except Exception as e:
            logging.error(f"⚠️ [Auditor 지연] 잔고 확인 실패: {str(e)}")
            raise Exception(f"매수 집행 전 잔고 확인 실패: {str(e)}")
        
        # [고도화] 매도로 인해 확보될 예상 예수금 합산 (0.5% 슬리피지 버퍼 적용)
        expected_sell_proceeds = 0
        for order in sell_orders:
            price = self.broker.get_price(order["ticker"])
            proceed = (price * order["quantity"]) * 0.995
            expected_sell_proceeds += proceed
            
        total_available_expected = available_cash + expected_sell_proceeds
        logging.info(f"[Auditor] 가용 예수금: {total_available_expected:,.0f}원 (현금 {available_cash:,.0f} + 매도수익 {expected_sell_proceeds:,.0f})")
        
        # 매수에 필요한 총 금액 계산 (수수료/슬리피지 대비 1.0% 버퍼)
        total_buy_required = 0
        for order in buy_orders:
            price = self.broker.get_price(order["ticker"])
            total_buy_required += (price * order["quantity"]) * 1.01
            
        logging.info(f"[Auditor] 매수필요: {total_buy_required:,.0f}원 vs 가용예수금: {total_available_expected:,.0f}원")
        
        if total_buy_required > total_available_expected:
            logging.warning(f"⚠️ [Auditor] 매수 필요({total_buy_required:,.0f}원) > 가용({total_available_expected:,.0f}원). 가용 범위 내 부분 매수로 전환합니다.")
            
            # 가용 예수금 범위 내에서 매수 가능한 주문만 선별 (금액이 작은 순서대로 우선 처리)
            buy_orders.sort(key=lambda o: self.broker.get_price(o["ticker"]) * o["quantity"])
            
            affordable_orders = []
            remaining_budget = total_available_expected
            skipped_orders = []
            
            for order in buy_orders:
                order_cost = self.broker.get_price(order["ticker"]) * order["quantity"] * 1.01
                if order_cost <= remaining_budget:
                    affordable_orders.append(order)
                    remaining_budget -= order_cost
                    logging.info(f"[Auditor] ✅ 매수 가능: {order['name']} {order['quantity']}주 ({order_cost:,.0f}원) → 잔여 예산: {remaining_budget:,.0f}원")
                else:
                    skipped_orders.append(order)
                    logging.info(f"[Auditor] ⏭️ 예산 초과 스킵: {order['name']} {order['quantity']}주 ({order_cost:,.0f}원)")
            
            buy_orders = affordable_orders
            
            if skipped_orders:
                skip_names = [f"{o['name']}({o['quantity']}주)" for o in skipped_orders]
                logging.warning(f"💡 [부분 매수] 예산 부족으로 다음 주문 보류: {', '.join(skip_names)}")
            
            if not buy_orders:
                raise Exception(f"잔고 부족(Sync Auditor): 매수 필요 금액({total_buy_required:,.0f}원)이 가용 예수금({total_available_expected:,.0f}원)을 초과하여, 매수 가능한 종목이 없습니다.")
            
        # 잔고 충분 시 BUY 실행
        logging.info(f"✅ 예수금 검증 통과 (필요: {total_buy_required:,.0f}원 / 예상가용: {total_available_expected:,.0f}원). BUY 주문 집행을 시작합니다.")
        buy_twap_tasks = []
        for order in buy_orders:
            price = self.broker.get_price(order["ticker"])
            total_amount = price * order["quantity"]
            
            if order.get("type", "").startswith("TLH"):
                logging.info(f"[세금최적화 재매수] {order['name']} {order['side']} {order['quantity']}주 (약 {total_amount:,}원)")
            else:
                logging.info(f"[일반/긴급 BUY] {order['name']} {order['side']} {order['quantity']}주 (약 {total_amount:,}원)")

                
            if total_amount >= 5000000 and order.get("type") in ["REGULAR", "EMERGENCY"]:
                logging.info(f"💡 대량 매수 감지! {order['name']} 종목에 대해 분할 매수(TWAP)를 가동합니다.")
                buy_twap_tasks.append(
                    self.broker.execute_twap_order(order["ticker"], order["quantity"], order["side"], duration_mins=10, chunks=5)
                )
            else:
                try:
                    res = self.broker.place_order(order["ticker"], order["quantity"], order["side"])
                    results.append(res)
                except Exception as e:
                    err_msg = f"⚠️ [매수 시장가 실패] {order['name']} 시장가 매수 실패: {str(e)}. 지정가(현재가)로 우회 시도합니다."
                    logging.warning(err_msg)
                    try:
                        price = self.broker.get_price(order["ticker"])
                        res_limit = self.broker.place_order(order["ticker"], order["quantity"], order["side"], order_type="00", price=price)
                        if res_limit.get("rt_cd") == "0":
                            order["order_type"] = "00"
                            order["price"] = price
                            order["odno"] = res_limit.get("output", {}).get("ODNO")
                            order["org_no"] = res_limit.get("output", {}).get("KRX_FWDG_ORD_ORGNO")
                            self.state_manager.add_retry_order(order)
                            await self.broker.bot.send_notification(f"🚑 **[지정가 우회]** {order['name']} 시장가 매수가 튕겨 현재가({price:,}원) 지정가로 전송했습니다. 15분 뒤에도 미체결 시 시장가로 재전환합니다.")
                        else:
                            raise Exception(res_limit.get("msg1", "지정가 주문도 거부됨"))
                    except Exception as e2:
                        final_err = f"🚨 **[매수 최종 실패]** {order['name']} 지정가 우회도 실패하여 대기열에만 등록합니다: {str(e2)}"
                        logging.error(final_err)
                        self.state_manager.add_retry_order(order)
                        if hasattr(self.broker, "bot") and self.broker.bot:
                            import asyncio
                            try: asyncio.create_task(self.broker.bot.send_notification(final_err))
                            except Exception: pass
                import asyncio
                await asyncio.sleep(0.5)

        if buy_twap_tasks:
            import asyncio
            logging.info("⏳ BUY 분할 주문(TWAP) 대기...")
            twap_results = await asyncio.gather(*buy_twap_tasks)
            results.extend(twap_results)
            
        return results

    async def execute_tlh_sequence(self, tlh_assets, original_orders):
        """
        [모듈 10] 3단계 절세용 매매 시퀀스 (Tax-Loss Harvesting)
        STEP 1: 손실 확정 (SELL)
        STEP 2: 포지션 복구 (BUY)
        STEP 3: 수익 실현 (SELL)
        """
        results = []
        import asyncio
        
        # STEP 1: 손실 확정 (마이너스 종목 전량 매도)
        logging.info("💸 [TLH STEP 1] 실현 손실 확정을 위한 매도를 집행합니다.")
        for asset in tlh_assets:
            res = self.broker.place_order(asset["ticker"], asset["quantity"], "SELL")
            results.append(res)
            await asyncio.sleep(0.5)
            
        # 예수금 동기화 대기
        await asyncio.sleep(2)
        
        # STEP 2: 포지션 복구 (방금 판 수량 그대로 다시 매수)
        logging.info("💸 [TLH STEP 2] 비중 유지를 위한 포지션 복구 매수를 집행합니다.")
        for asset in tlh_assets:
            # Sync Auditor 개념 적용: 예수금 확인 후 매수
            balance = self.broker.get_balance()
            price = self.broker.get_price(asset["ticker"])
            required = (price * asset["quantity"]) * 1.015
            
            if balance["cash"] >= required:
                res = self.broker.place_order(asset["ticker"], asset["quantity"], "BUY")
                results.append(res)
            else:
                logging.error(f"❌ [TLH 오류] {asset['name']} 복구 매수 중 예수금 부족 감지!")
            await asyncio.sleep(0.5)
            
        # STEP 3: 수익 실현 (원래 하려던 익절 매도 집행)
        logging.info("💸 [TLH STEP 3] 이제 원래 목표였던 수익 실현 익절 매도를 집행합니다.")
        profit_sell_orders = [o for o in original_orders if o["side"] == "SELL"]
        for order in profit_sell_orders:
            res = self.broker.place_order(order["ticker"], order["quantity"], "SELL")
            results.append(res)
            await asyncio.sleep(0.5)
            
        # 텔레그램 리포트 발송
        msg = f"💸 **[스마트 절세 작동 (TLH)]**\n\n익절 매도 직전, 현재 손실 중인 {len(tlh_assets)}개 종목을 상계 매매하여 올해의 실현 수익 세금을 완벽히 방어했습니다!\n(손실 확정 -> 포지션 복구 -> 수익 실현 3단계 완료)"
        if hasattr(self.broker, "bot") and self.broker.bot:
            asyncio.create_task(self.broker.bot.send_notification(msg))
            
        return results

