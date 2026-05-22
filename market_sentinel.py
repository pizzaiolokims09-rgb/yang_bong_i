"""
market_sentinel.py - 양봉이 시장 구조적 위험 감시 모듈
=====================================================
외국인 수급 불균형, 포지션 역전, 프로그램 매도 폭탄, 만기일 알림,
가변 현금 비중(Dynamic Exposure) 등을 감시합니다.

KIS REST API를 직접 호출하며, broker_kis.py의 인증/세션을 공유합니다.
"""

import logging
import asyncio
import time
import json
import requests
import pytz
import datetime
from datetime import timedelta

# KOSPI200 선물 1계약 승수 (지수 x 250,000원)
FUTURES_MULTIPLIER = 250_000

# --- 임계값 상수 ---
# (1) 수급 불균형: 외국인 현물 매수 > 1,000억 AND 선물 매도 > 5,000계약
SPOT_NET_BUY_THRESHOLD = 100_000_000_000   # 1,000억 원
FUTURES_NET_SELL_THRESHOLD = -5_000          # -5,000계약 (순매도이므로 음수)

# (2) 포지션 역전: 5분(300초) 이상 지속 시 경고
CROSSOVER_DURATION_THRESHOLD = 300  # 초

# (3) 프로그램 매도 폭탄: 지수 -1% 이상 급락 + 비차익 순매도 비중 60% 이상
INDEX_DROP_THRESHOLD = -1.0  # %
PROGRAM_SELL_RATIO_THRESHOLD = 0.60  # 60%

# (4) 가변 비중: 외국인 선물 순매도 10,000계약 단위로 10% 축소
EXPOSURE_STEP_CONTRACTS = 10_000
EXPOSURE_STEP_REDUCTION = 10  # %
MIN_EXPOSURE_PCT = 50  # 최저 한도


class MarketSentinel:
    """시장 구조적 위험을 실시간으로 감시하는 파수꾼 모듈"""

    def __init__(self, broker):
        """
        broker: KISBroker 인스턴스 (인증 토큰, base_url, _safe_request, _get_headers 공유)
        """
        self.broker = broker
        self._crossover_start_time = None  # 포지션 역전 시작 시각

    # =========================================================================
    # 내부 헬퍼: KIS API 호출
    # =========================================================================

    def _kis_get(self, path, tr_id, params):
        """KIS REST API GET 호출 (broker의 인증/재시도 로직 재사용)"""
        if time.time() > self.broker.token_exp:
            self.broker.auth()

        url = f"{self.broker.base_url}{path}"
        headers = self.broker._get_headers(tr_id)
        res = self.broker._safe_request("GET", url, headers=headers, params=params)
        return res.json()

    # =========================================================================
    # (1) 수급 불균형 감시 (Arbitrage Logic)
    # =========================================================================

    async def check_foreign_flow(self) -> dict:
        """
        외국인의 현물 순매수액과 선물 순매수량을 수집합니다.
        TR: FHKST01010900 (투자자별 매매동향 - 종합)

        임계값 알림 조건:
        - 외국인 현물 매수 > 1,000억 원
        - AND 선물 매도 > 5,000계약
        """
        result = {
            "spot_net_buy": 0,       # 외국인 현물 순매수 금액(원)
            "futures_net_buy": 0,    # 외국인 선물 순매수 수량(계약)
            "alert": False,
            "error": None
        }

        try:
            # 현물 투자자별 매매동향 조회
            kst = pytz.timezone('Asia/Seoul')
            today = datetime.datetime.now(kst).strftime("%Y%m%d")

            spot_data = await asyncio.to_thread(
                self._kis_get,
                "/uapi/domestic-stock/v1/quotations/inquire-investor",
                "FHKST01010900",
                {
                    "FID_COND_MRKT_DIV_CODE": "J",   # J: 주식
                    "FID_INPUT_ISCD": "0001",          # 0001: 코스피 전체
                    "FID_INPUT_DATE_1": today,
                    "FID_INPUT_DATE_2": today,
                    "FID_PERIOD_DIV_CODE": "D"         # D: 일별
                }
            )

            # output에서 외국인(frgn) 순매수 금액 추출
            if "output" in spot_data:
                for row in spot_data["output"]:
                    # frgn_ntby_tr_pbmn: 외국인 순매수 거래대금
                    frgn_val = row.get("frgn_ntby_tr_pbmn", row.get("frgn_ntby_qty", "0"))
                    result["spot_net_buy"] = int(frgn_val)
                    break  # 첫 행이 당일 합계

            # 선물 투자자별 매매동향 조회
            futures_data = await asyncio.to_thread(
                self._kis_get,
                "/uapi/domestic-stock/v1/quotations/inquire-investor",
                "FHKST01010900",
                {
                    "FID_COND_MRKT_DIV_CODE": "F",   # F: 선물
                    "FID_INPUT_ISCD": "0001",
                    "FID_INPUT_DATE_1": today,
                    "FID_INPUT_DATE_2": today,
                    "FID_PERIOD_DIV_CODE": "D"
                }
            )

            if "output" in futures_data:
                for row in futures_data["output"]:
                    frgn_qty = row.get("frgn_ntby_qty", "0")
                    result["futures_net_buy"] = int(frgn_qty)
                    break

            # 임계값 검사: 현물 매수 > 1000억 AND 선물 매도 > 5000계약
            if (result["spot_net_buy"] > SPOT_NET_BUY_THRESHOLD and
                    result["futures_net_buy"] < FUTURES_NET_SELL_THRESHOLD):
                result["alert"] = True
                logging.warning(
                    f"🚨 [수급 불균형] 외국인 현물 순매수: {result['spot_net_buy']:,}원, "
                    f"선물 순매수: {result['futures_net_buy']:,}계약"
                )

        except Exception as e:
            result["error"] = str(e)
            logging.error(f"❌ [MarketSentinel] 수급 불균형 감시 실패: {e}")

        return result

    # =========================================================================
    # (2) 포지션 역전 감시 (Cross-Over Logic)
    # =========================================================================

    async def check_position_crossover(self, foreign_flow: dict) -> dict:
        """
        현물 매수액(금액) 대비 선물 매도액(환산금액)이 더 커지는 시점을 포착합니다.
        선물 환산금액 = |선물 순매도 수량| x KOSPI200 지수 x 승수(250,000원)

        5분 이상 지속 시 "외국인 양매도/헤지 구간 진입" 경고 알림.
        """
        result = {
            "is_crossover": False,
            "duration_sec": 0,
            "spot_amount": 0,
            "futures_amount": 0,
            "alert": False,
            "error": None
        }

        try:
            spot_buy = foreign_flow.get("spot_net_buy", 0)
            futures_qty = foreign_flow.get("futures_net_buy", 0)

            # KOSPI200 지수 조회 (TR: FHPUP02100000)
            index_data = await asyncio.to_thread(
                self._kis_get,
                "/uapi/domestic-stock/v1/quotations/inquire-index-price",
                "FHPUP02100000",
                {
                    "FID_COND_MRKT_DIV_CODE": "U",
                    "FID_INPUT_ISCD": "0101"  # KOSPI200
                }
            )

            kospi200_price = 350.0  # 폴백 기본값
            if "output" in index_data:
                price_str = index_data["output"].get("bstp_nmix_prpr", "350.0")
                kospi200_price = float(price_str)

            # 선물 매도 환산금액 계산 (순매도이므로 음수 -> abs)
            futures_sell_amount = abs(futures_qty) * kospi200_price * FUTURES_MULTIPLIER if futures_qty < 0 else 0

            result["spot_amount"] = spot_buy
            result["futures_amount"] = int(futures_sell_amount)

            # 역전 판단: 선물 매도 환산금액 > 현물 매수액
            if futures_sell_amount > spot_buy and spot_buy > 0:
                result["is_crossover"] = True

                if self._crossover_start_time is None:
                    self._crossover_start_time = time.time()
                    logging.info("⚠️ [포지션 역전] 선물 매도 환산금액이 현물 매수액을 초과하기 시작했습니다.")

                duration = time.time() - self._crossover_start_time
                result["duration_sec"] = int(duration)

                if duration >= CROSSOVER_DURATION_THRESHOLD:
                    result["alert"] = True
                    logging.warning(
                        f"🚨 [포지션 역전 경고] {int(duration)}초 지속! "
                        f"현물: {spot_buy:,}원 vs 선물 환산: {int(futures_sell_amount):,}원"
                    )
            else:
                # 역전 해소
                if self._crossover_start_time is not None:
                    logging.info("✅ [포지션 역전 해소] 정상 상태로 복귀했습니다.")
                self._crossover_start_time = None

        except Exception as e:
            result["error"] = str(e)
            logging.error(f"❌ [MarketSentinel] 포지션 역전 감시 실패: {e}")

        return result

    # =========================================================================
    # (3) 프로그램 매도 폭탄 감지 (HFT Counter-Measure)
    # =========================================================================

    async def check_program_selling_bomb(self) -> dict:
        """
        프로그램 매매동향에서 '비차익 순매도' 수치를 트래킹합니다.
        TR: FHPPG04650100 (프로그램 매매동향)

        Panic-Sell 방지 로직:
        - KOSPI200 지수가 1% 이상 급락할 때
        - 비차익 순매도가 전체 하락 거래량의 60% 이상이면 '기계적 매도'로 판단
        """
        result = {
            "is_mechanical_sell": False,
            "non_arb_net_sell": 0,      # 비차익 순매도 금액
            "total_sell_amount": 0,     # 전체 매도 금액
            "index_change_pct": 0.0,
            "error": None
        }

        try:
            # KOSPI200 지수 조회 및 변동률 확인
            index_data = await asyncio.to_thread(
                self._kis_get,
                "/uapi/domestic-stock/v1/quotations/inquire-index-price",
                "FHPUP02100000",
                {
                    "FID_COND_MRKT_DIV_CODE": "U",
                    "FID_INPUT_ISCD": "0101"
                }
            )

            if "output" in index_data:
                change_rate_str = index_data["output"].get("bstp_nmix_prdy_ctrt", "0")
                result["index_change_pct"] = float(change_rate_str)

            # 지수가 1% 이상 급락하지 않았으면 조기 종료
            if result["index_change_pct"] > INDEX_DROP_THRESHOLD:
                return result

            # 프로그램 매매동향 조회
            program_data = await asyncio.to_thread(
                self._kis_get,
                "/uapi/domestic-stock/v1/quotations/program-trade-by-stock",
                "FHPPG04650100",
                {
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": "0001"
                }
            )

            if "output" in program_data:
                for row in program_data["output"]:
                    # 비차익 순매도 금액
                    non_arb = int(row.get("ntby_sll_tr_pbmn", row.get("narb_ntby_tr_pbmn", "0")))
                    total_sell = int(row.get("whol_sll_tr_pbmn", row.get("total_sll_tr_pbmn", "1")))

                    result["non_arb_net_sell"] = abs(non_arb)
                    result["total_sell_amount"] = abs(total_sell) if total_sell != 0 else 1
                    break

            # 기계적 매도 판단
            if result["total_sell_amount"] > 0:
                ratio = result["non_arb_net_sell"] / result["total_sell_amount"]
                if ratio >= PROGRAM_SELL_RATIO_THRESHOLD:
                    result["is_mechanical_sell"] = True
                    logging.critical(
                        f"🚨 [프로그램 매도 폭탄] 기계적 매도 감지! "
                        f"비차익 순매도 비중: {ratio:.1%}, 지수 변동: {result['index_change_pct']:.2f}%"
                    )

        except Exception as e:
            result["error"] = str(e)
            logging.error(f"❌ [MarketSentinel] 프로그램 매도 감시 실패: {e}")

        return result

    # =========================================================================
    # (4) 만기일 스케줄러 (Expiration Day Alert)
    # =========================================================================

    @staticmethod
    def get_next_expiry_dates(months_ahead=3) -> list:
        """
        매월 둘째 주 목요일(선물/옵션 만기일)을 자동 계산합니다.
        3, 6, 9, 12월은 [쿼드러플 위칭데이] 태그를 추가합니다.

        반환: [{"date": datetime, "is_quadruple": bool, "label": str}, ...]
        """
        kst = pytz.timezone('Asia/Seoul')
        today = datetime.datetime.now(kst).date()
        results = []

        for month_offset in range(months_ahead + 1):
            # 대상 월 계산
            year = today.year
            month = today.month + month_offset
            while month > 12:
                month -= 12
                year += 1

            # 해당 월의 첫 번째 날
            first_day = datetime.date(year, month, 1)

            # 첫째 주 목요일 찾기
            # weekday(): 월=0, 화=1, 수=2, 목=3, 금=4
            days_until_thursday = (3 - first_day.weekday()) % 7
            first_thursday = first_day + timedelta(days=days_until_thursday)

            # 둘째 주 목요일 = 첫째 주 목요일 + 7일
            second_thursday = first_thursday + timedelta(days=7)

            # 이미 지난 날짜는 스킵
            if second_thursday < today:
                continue

            is_quadruple = month in [3, 6, 9, 12]
            label = f"{year}년 {month}월 만기일"
            if is_quadruple:
                label += " [쿼드러플 위칭데이]"

            results.append({
                "date": second_thursday,
                "is_quadruple": is_quadruple,
                "label": label
            })

        return results

    @staticmethod
    def format_expiry_alert(expiry_info, days_before) -> str:
        """만기일 알림 메시지를 포맷합니다."""
        date_str = expiry_info["date"].strftime("%Y년 %m월 %d일 (목)")

        if expiry_info["is_quadruple"]:
            tag = "🔴🔴🔴 [쿼드러플 위칭데이]"
            urgency = "선물, 옵션, 주가지수선물, 주가지수옵션이 동시에 만기되는 초고위험일입니다!"
        else:
            tag = "🟡 [선물/옵션 만기일]"
            urgency = "선물/옵션 만기에 따른 변동성 확대에 유의하세요."

        if days_before == 0:
            timing = "⚡ 오늘이 바로 만기일입니다!"
        elif days_before == 1:
            timing = "⏰ 내일이 만기일입니다!"
        else:
            timing = f"📅 만기일 {days_before}일 전입니다."

        return (
            f"{tag}\n\n"
            f"{timing}\n"
            f"📍 만기일: {date_str}\n\n"
            f"💡 {urgency}\n"
            f"프로그램 매매 및 외국인 선물 포지션 청산으로 인한 급변동에 대비하세요."
        )

    # =========================================================================
    # (5) 가변 현금 비중 (Dynamic Exposure)
    # =========================================================================

    async def calculate_dynamic_exposure(self, state_manager) -> dict:
        """
        외국인 선물 누적 순매도(최근 10영업일) 기준으로
        현물 포지션 최대 한도를 10% 단위로 축소합니다.

        - 기본: 100%
        - 누적 순매도 10,000계약 초과 시: 90%
        - 누적 순매도 20,000계약 초과 시: 80%
        - ... (최저 50%)
        """
        result = {
            "max_exposure_pct": 100,
            "cumulative_futures_net_sell": 0,
            "reduction_steps": 0,
            "error": None
        }

        try:
            kst = pytz.timezone('Asia/Seoul')
            today = datetime.datetime.now(kst)

            # 최근 10영업일 데이터 수집
            cumulative = 0
            history = state_manager.state.get("foreign_futures_history", [])

            # 당일 데이터를 API로 가져와서 히스토리에 추가
            today_str = today.strftime("%Y%m%d")
            futures_data = await asyncio.to_thread(
                self._kis_get,
                "/uapi/domestic-stock/v1/quotations/inquire-investor",
                "FHKST01010900",
                {
                    "FID_COND_MRKT_DIV_CODE": "F",
                    "FID_INPUT_ISCD": "0001",
                    "FID_INPUT_DATE_1": today_str,
                    "FID_INPUT_DATE_2": today_str,
                    "FID_PERIOD_DIV_CODE": "D"
                }
            )

            today_net = 0
            if "output" in futures_data:
                for row in futures_data["output"]:
                    today_net = int(row.get("frgn_ntby_qty", "0"))
                    break

            # 히스토리에 오늘 데이터 업데이트 (중복 방지)
            date_key = today.strftime("%Y-%m-%d")
            # 기존 오늘 데이터가 있으면 교체, 없으면 추가
            history = [h for h in history if h.get("date") != date_key]
            history.append({"date": date_key, "net_buy": today_net})

            # 최근 10일만 유지
            history = sorted(history, key=lambda x: x["date"])[-10:]
            state_manager.state["foreign_futures_history"] = history
            state_manager.save_state()

            # 누적 순매도 계산 (순매도 = 음수)
            for h in history:
                net = h.get("net_buy", 0)
                if net < 0:
                    cumulative += abs(net)

            result["cumulative_futures_net_sell"] = cumulative

            # 축소 단계 계산
            steps = cumulative // EXPOSURE_STEP_CONTRACTS
            result["reduction_steps"] = int(steps)
            result["max_exposure_pct"] = max(
                MIN_EXPOSURE_PCT,
                100 - (int(steps) * EXPOSURE_STEP_REDUCTION)
            )

            # 상태에 현재 비중 레벨 저장
            state_manager.state["max_exposure_pct"] = result["max_exposure_pct"]
            state_manager.save_state()

            if result["max_exposure_pct"] < 100:
                logging.warning(
                    f"⚠️ [가변 비중] 외국인 선물 10일 누적 순매도: {cumulative:,}계약 → "
                    f"현물 포지션 최대 한도: {result['max_exposure_pct']}%"
                )

        except Exception as e:
            result["error"] = str(e)
            logging.error(f"❌ [MarketSentinel] 가변 비중 계산 실패: {e}")

        return result

    # =========================================================================
    # 통합 스캔 메서드 (main.py에서 호출)
    # =========================================================================

    async def run_full_scan(self, state_manager, bot) -> dict:
        """
        모든 감시 로직을 순차적으로 실행하고 결과를 통합합니다.
        텔레그램 알림이 필요한 경우 즉시 발송합니다.
        """
        scan_result = {
            "foreign_flow": None,
            "crossover": None,
            "program_bomb": None,
            "dynamic_exposure": None,
            "alerts_sent": 0
        }

        try:
            # (1) 수급 불균형
            flow = await self.check_foreign_flow()
            scan_result["foreign_flow"] = flow
            if flow.get("alert"):
                await bot.send_notification(
                    f"🚨 **[외국인 수급 불균형 경보]**\n\n"
                    f"외국인 현물 순매수: **{flow['spot_net_buy']:,}원** (> 1,000억)\n"
                    f"외국인 선물 순매도: **{abs(flow['futures_net_buy']):,}계약** (> 5,000계약)\n\n"
                    f"💡 대규모 현물 매수 + 선물 매도 조합은 차익거래(Arbitrage) 또는 헤지 수요를 의미합니다.\n"
                    f"장 후반 프로그램 매도 폭탄에 유의하세요!"
                )
                scan_result["alerts_sent"] += 1

            # (2) 포지션 역전
            crossover = await self.check_position_crossover(flow)
            scan_result["crossover"] = crossover
            if crossover.get("alert"):
                await bot.send_notification(
                    f"⚠️ **[외국인 양매도/헤지 구간 진입]**\n\n"
                    f"선물 매도 환산금액이 현물 매수액을 **{crossover['duration_sec']}초** 이상 초과 중입니다!\n\n"
                    f"📊 현물 매수액: {crossover['spot_amount']:,}원\n"
                    f"📉 선물 매도 환산: {crossover['futures_amount']:,}원\n\n"
                    f"💡 외국인이 현물과 선물 양쪽에서 동시에 매도 압력을 가하는 구간입니다. 추가 하락에 유의하세요."
                )
                scan_result["alerts_sent"] += 1

            # (3) 프로그램 매도 폭탄
            bomb = await self.check_program_selling_bomb()
            scan_result["program_bomb"] = bomb
            if bomb.get("is_mechanical_sell"):
                await bot.send_notification(
                    f"🚨🚨 **[프로그램 매도 폭탄 감지 - 기계적 매도]**\n\n"
                    f"KOSPI200 지수 변동: **{bomb['index_change_pct']:.2f}%**\n"
                    f"비차익 순매도: **{bomb['non_arb_net_sell']:,}원**\n"
                    f"전체 매도 대비 비중: **{bomb['non_arb_net_sell']/bomb['total_sell_amount']:.1%}**\n\n"
                    f"🛡️ 현재 HFT/기관의 기계적 투매로 판단하여, "
                    f"양봉이의 손절 로직을 일시 정지하고 **재매수(Bottom Fishing) 타점 탐색 모드**로 전환합니다."
                )
                # Bottom Fishing 모드 활성화 (state_manager를 통해)
                state_manager.state["is_bottom_fishing_mode"] = True
                state_manager.save_state()
                scan_result["alerts_sent"] += 1

            # (4) 가변 현금 비중
            exposure = await self.calculate_dynamic_exposure(state_manager)
            scan_result["dynamic_exposure"] = exposure

        except Exception as e:
            logging.error(f"❌ [MarketSentinel] 통합 스캔 중 오류: {e}")

        return scan_result
