import json
import os
import logging
import gc
from datetime import datetime
import pytz

class StateManager:
    DAILY_MAX_ORDERS = 200
    DAILY_MAX_VOLUME_RATIO = 4.0
    GEMINI_DAILY_MAX_CALLS = 100  # Gemini API 일일 최대 호출 한도

    def __init__(self, state_file="state.json", config_file="portfolio_config.json", history_file="meeting_history.json"):
        self.state_file = state_file
        self.config_file = config_file
        self.history_file = history_file
        
        self.state = self.load_state()
        self.portfolio_config = self.load_portfolio_config()
        self.meeting_history = self.load_meeting_history()

    # --- 기존 상태 관리 로직 ---
    def load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if "is_emergency_stop" not in data:
                        data["is_emergency_stop"] = False
                    if "today_orders_count" not in data: data["today_orders_count"] = 0
                    if "today_volume" not in data: data["today_volume"] = 0.0
                    if "last_order_date" not in data: data["last_order_date"] = ""
                    if "crisis_tickers" not in data: data["crisis_tickers"] = []
                    if "recent_instructions" not in data: data["recent_instructions"] = []
                    if "permanent_instructions" not in data: data["permanent_instructions"] = []
                    # Market Sentinel 신규 필드
                    if "is_bottom_fishing_mode" not in data: data["is_bottom_fishing_mode"] = False
                    if "bottom_fishing_until" not in data: data["bottom_fishing_until"] = ""
                    if "max_exposure_pct" not in data: data["max_exposure_pct"] = 100
                    if "foreign_futures_history" not in data: data["foreign_futures_history"] = []
                    # [패치] API 과금 방지용 쿨다운/할당량 필드
                    if "gemini_cooldown_until" not in data: data["gemini_cooldown_until"] = ""
                    if "kis_cooldown_until" not in data: data["kis_cooldown_until"] = ""
                    if "gemini_daily_call_count" not in data: data["gemini_daily_call_count"] = 0
                    if "gemini_daily_call_date" not in data: data["gemini_daily_call_date"] = ""
                    if "kis_consecutive_failures" not in data: data["kis_consecutive_failures"] = 0
                    return data
            except:
                pass
        return {
            "seed_amount": 0,
            "protected_amount": 0,
            "is_auto_mode": False,
            "last_cash_balance": 0,
            "last_rebalancing_plan": None,
            "is_emergency_stop": False,
            "today_orders_count": 0,
            "today_volume": 0.0,
            "last_order_date": "",
            "crisis_tickers": [],
            "recent_instructions": [],
            "permanent_instructions": [],
            "profit_history": [0.0],
            "pending_retry_orders": [],
            "is_bottom_fishing_mode": False,
            "bottom_fishing_until": "",
            "max_exposure_pct": 100,
            "foreign_futures_history": [],
            "gemini_cooldown_until": "",
            "kis_cooldown_until": "",
            "gemini_daily_call_count": 0,
            "gemini_daily_call_date": "",
            "kis_consecutive_failures": 0
        }

    def save_state(self):
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)

    def trigger_panic(self):
        self.state["is_emergency_stop"] = True
        self.save_state()
        
    def add_profit_history(self, profit_rate):
        history = self.state.get("profit_history", [0.0])
        history.append(profit_rate)
        # 차트 가독성을 위해 최근 30회차까지만 유지
        self.state["profit_history"] = history[-30:]
        self.save_state()

    def get_retry_orders(self):
        return self.state.get("pending_retry_orders", [])
        
    def add_retry_order(self, order):
        orders = self.get_retry_orders()
        # 중복 방지를 위해 티커 기준 필터링
        orders = [o for o in orders if o["ticker"] != order["ticker"]]
        
        # [신규] 주문 시각 및 메타데이터 자동 추가 (KST 강제)
        kst = pytz.timezone('Asia/Seoul')
        order["placed_at"] = datetime.now(kst).isoformat()
        if "order_type" not in order:
            order["order_type"] = "01" # 기본 시장가
            
        orders.append(order)
        self.state["pending_retry_orders"] = orders
        self.save_state()
        
    def remove_retry_order(self, ticker):
        orders = self.get_retry_orders()
        self.state["pending_retry_orders"] = [o for o in orders if o["ticker"] != ticker]
        self.save_state()
        
    def clear_retry_orders(self):
        self.state["pending_retry_orders"] = []
        self.save_state()

    def check_and_update_rate_limit(self, trade_amount):
        """일일 주문 횟수 및 거래 대금 제한을 확인하고 상태를 업데이트함"""
        kst = pytz.timezone('Asia/Seoul')
        today_str = datetime.now(kst).strftime("%Y-%m-%d")
        
        if self.state.get("last_order_date") != today_str:
            self.state["last_order_date"] = today_str
            self.state["today_orders_count"] = 0
            self.state["today_volume"] = 0.0
            
        current_count = self.state.get("today_orders_count", 0)
        current_vol = self.state.get("today_volume", 0.0)
        seed = self.state.get("seed_amount", 0)
        
        if current_count >= self.DAILY_MAX_ORDERS:
            return False, "MAX_ORDERS_EXCEEDED"
            
        if seed > 0 and (current_vol + trade_amount) > (seed * self.DAILY_MAX_VOLUME_RATIO):
            return False, "MAX_VOLUME_EXCEEDED"
            
        self.state["today_orders_count"] = current_count + 1
        self.state["today_volume"] = current_vol + trade_amount
        self.save_state()
        return True, "OK"

    def set_seed(self, amount):
        self.state["seed_amount"] = amount
        self.save_state()

    def set_protected(self, amount):
        self.state["protected_amount"] = amount
        self.save_state()

    def toggle_auto(self):
        self.state["is_auto_mode"] = not self.state["is_auto_mode"]
        self.save_state()
        return self.state["is_auto_mode"]

    def update_plan(self, plan):
        self.state["last_rebalancing_plan"] = plan
        self.save_state()

    def set_crisis_mode(self, ticker, is_crisis):
        """특정 종목의 위기 모드(추가 매수 차단) 설정/해제"""
        crisis_list = self.state.get("crisis_tickers", [])
        if is_crisis:
            if ticker not in crisis_list:
                crisis_list.append(ticker)
        else:
            if ticker in crisis_list:
                crisis_list.remove(ticker)
        self.state["crisis_tickers"] = crisis_list
        self.save_state()

    def is_ticker_in_crisis(self, ticker):
        return ticker in self.state.get("crisis_tickers", [])

    # --- 1. Dynamic Config 로더/세이버 ---
    def load_portfolio_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                pass
        
        # 파일이 없을 시 기본 9개 종목 설정 생성 ({name: target_weight} 형식 - portfolio_config의 실제 런타임 포맷)
        default_config = {
            "assets": {
                "일본니케이225": 8.5,
                "차이나CSI300": 8.5,
                "200TR": 8.5,
                "미국나스닥100": 8.5,
                "인도Nifty": 8.5,
                "미국배당다우존스": 8.5,
                "ACE KRX금현물": 19.0,
                "국고채30년스트립": 15.0,
                "미국채30년액티브": 15.0
            }
        }
        self.save_portfolio_config(default_config)
        return default_config

    def save_portfolio_config(self, config_data=None):
        if config_data is not None:
            self.portfolio_config = config_data
        with open(self.config_file, 'w', encoding='utf-8') as f:
            json.dump(self.portfolio_config, f, indent=2, ensure_ascii=False)

    def update_portfolio_config(self, new_config):
        """텔레그램 등에서 수정 지시 시 덮어쓰기"""
        self.save_portfolio_config(new_config)

    # --- 2. 회의록(Memory) 로더/세이버 ---
    def load_meeting_history(self):
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                pass
        return []

    def save_meeting_history(self):
        with open(self.history_file, 'w', encoding='utf-8') as f:
            json.dump(self.meeting_history, f, indent=2, ensure_ascii=False)

    async def add_meeting_record(self, minutes, summarizer_fn=None):
        """리밸런싱 종료 후 AI 위원회의 요약(minutes)을 저장 및 자동 압축"""
        kst = pytz.timezone('Asia/Seoul')
        record = {
            "timestamp": datetime.now(kst).strftime("%Y-%m-%d %H:%M:%S"),
            "minutes": minutes
        }
        self.meeting_history.append(record)
        
        # 1. 회의록이 20개가 넘으면 오래된 10개를 핵심 학습 포인트로 요약하여 압축
        if len(self.meeting_history) > 20 and summarizer_fn:
            to_summarize = [rec["minutes"] for rec in self.meeting_history[:10]]
            summary = await summarizer_fn(to_summarize)
            if summary:
                # 요약본을 하나로 뭉치고 나머지는 삭제
                compressed_record = {
                    "timestamp": f"{self.meeting_history[0]['timestamp']} ~ {self.meeting_history[9]['timestamp']} (Compressed)",
                    "minutes": f"[과거 회의록 요약 학습 데이터]\n{summary}"
                }
                self.meeting_history = [compressed_record] + self.meeting_history[10:]
        
        self.save_meeting_history()

    def optimize_memory(self):
        """불필요한 캐시 및 메모리 자원 강제 해제"""
        gc.collect()

    def get_recent_memory(self, limit=3):
        """가장 최근 limit개의 회의록 묶어서 텍스트로 반환"""
        recent_records = self.meeting_history[-limit:]
        if not recent_records:
            return "이전 회의록(Memory)이 없습니다."
            
        memory_texts = []
        for idx, rec in enumerate(reversed(recent_records), 1):
            memory_texts.append(f"[{rec['timestamp']}]\n{rec['minutes']}")
            
        return "\n\n---\n\n".join(memory_texts)

    # --- 3. 사용자 지침(Instruction) 관리 ---
    async def add_user_instruction(self, text, summarizer_fn):
        """사용자 지침 추가 및 자동 요약/압축 로직"""
        kst = pytz.timezone('Asia/Seoul')
        today_str = datetime.now(kst).strftime("%Y-%m-%d %H:%M:%S")
        self.state["recent_instructions"].append(f"[{today_str}] {text}")
        
        # 1. 최근 지침이 10개가 넘어가면 오래된 5개를 요약하여 영구 지침으로 이동
        if len(self.state["recent_instructions"]) > 10:
            to_summarize = self.state["recent_instructions"][:5]
            summary = await summarizer_fn(to_summarize)
            if summary:
                self.state["permanent_instructions"].append(summary)
                self.state["recent_instructions"] = self.state["recent_instructions"][5:]
        
        # 2. 영구 지침이 5개가 쌓이면 오래된 3개를 취합/요약하여 1개로 병합
        if len(self.state["permanent_instructions"]) > 5:
            to_merge = self.state["permanent_instructions"][:3]
            merged_summary = await summarizer_fn(to_merge, is_merge=True)
            if merged_summary:
                new_permanent = [merged_summary] + self.state["permanent_instructions"][3:]
                self.state["permanent_instructions"] = new_permanent
        
        self.save_state()

    def remove_instruction(self, ins_type, index):
        """특정 지침 삭제 (ins_type: 'P' 또는 'R', index: 1-based)"""
        key = "permanent_instructions" if ins_type == "P" else "recent_instructions"
        if key in self.state and 0 <= index < len(self.state[key]):
            removed = self.state[key].pop(index)
            self.save_state()
            return removed
        return None

    def clear_instructions(self):
        """모든 지침 초기화"""
        self.state["recent_instructions"] = []
        self.state["permanent_instructions"] = []
        self.save_state()

    def get_instructions_context(self):
        """AI 프롬프트에 주입할 지침 컨텍스트 생성"""
        p_ins = self.state.get("permanent_instructions", [])
        r_ins = self.state.get("recent_instructions", [])
        
        if not p_ins and not r_ins:
            return "등록된 사용자 특별 지침이 없습니다."
            
        context = "[영구 통합 지침]\n" + "\n".join(p_ins) if p_ins else ""
        context += "\n\n[최근 추가 지침]\n" + "\n".join(r_ins) if r_ins else ""
        return context

    # --- 4. API 과금 방지: 쿨다운 및 할당량 제어 ---
    def is_gemini_in_cooldown(self):
        """Gemini API 쿨다운 중인지 확인"""
        cooldown_str = self.state.get("gemini_cooldown_until", "")
        if not cooldown_str:
            return False
        kst = pytz.timezone('Asia/Seoul')
        try:
            cooldown_until = datetime.fromisoformat(cooldown_str)
            if cooldown_until.tzinfo is None:
                cooldown_until = kst.localize(cooldown_until)
            return datetime.now(kst) < cooldown_until
        except Exception:
            return False

    def is_kis_in_cooldown(self):
        """KIS API 쿨다운 중인지 확인"""
        cooldown_str = self.state.get("kis_cooldown_until", "")
        if not cooldown_str:
            return False
        kst = pytz.timezone('Asia/Seoul')
        try:
            cooldown_until = datetime.fromisoformat(cooldown_str)
            if cooldown_until.tzinfo is None:
                cooldown_until = kst.localize(cooldown_until)
            return datetime.now(kst) < cooldown_until
        except Exception:
            return False

    def set_gemini_cooldown(self, seconds):
        """Gemini API 쿨다운 기간 설정 (초 단위)"""
        from datetime import timedelta
        kst = pytz.timezone('Asia/Seoul')
        until = datetime.now(kst) + timedelta(seconds=seconds)
        self.state["gemini_cooldown_until"] = until.isoformat()
        self.save_state()
        logging.warning(f"[Gemini 쿨다운 설정] {seconds}초 동안 API 호출을 중단합니다. (해제: {until.strftime('%H:%M:%S')})")

    def set_kis_cooldown(self, seconds):
        """KIS API 쿨다운 기간 설정 (초 단위)"""
        from datetime import timedelta
        kst = pytz.timezone('Asia/Seoul')
        until = datetime.now(kst) + timedelta(seconds=seconds)
        self.state["kis_cooldown_until"] = until.isoformat()
        self.save_state()
        logging.warning(f"[KIS 쿨다운 설정] {seconds}초 동안 API 호출을 중단합니다. (해제: {until.strftime('%H:%M:%S')})")

    def check_and_increment_gemini_call(self):
        """Gemini API 일일 호출 횟수 확인 및 증가. 한도 초과 시 False 반환"""
        kst = pytz.timezone('Asia/Seoul')
        today_str = datetime.now(kst).strftime("%Y-%m-%d")
        if self.state.get("gemini_daily_call_date") != today_str:
            self.state["gemini_daily_call_date"] = today_str
            self.state["gemini_daily_call_count"] = 0
        if self.state["gemini_daily_call_count"] >= self.GEMINI_DAILY_MAX_CALLS:
            return False
        self.state["gemini_daily_call_count"] += 1
        self.save_state()
        return True

    def record_kis_failure(self):
        """KIS API 연속 실패 횟수 기록. 반환값: 현재 연속 실패 횟수"""
        self.state["kis_consecutive_failures"] = self.state.get("kis_consecutive_failures", 0) + 1
        self.save_state()
        return self.state["kis_consecutive_failures"]

    def record_kis_success(self):
        """KIS API 성공 시 연속 실패 횟수 초기화"""
        if self.state.get("kis_consecutive_failures", 0) > 0:
            self.state["kis_consecutive_failures"] = 0
            self.save_state()

    # --- 5. MarketSentinel 연동: Bottom Fishing 모드 (자동 만료 포함) ---
    def set_bottom_fishing_mode(self, active, duration_seconds=86400):
        """프로그램 매도 폭탄 감지 시 활성화. 자동 만료 시각을 함께 기록하여 무한 지속을 방지."""
        from datetime import timedelta
        kst = pytz.timezone('Asia/Seoul')
        self.state["is_bottom_fishing_mode"] = active
        if active:
            self.state["bottom_fishing_until"] = (datetime.now(kst) + timedelta(seconds=duration_seconds)).isoformat()
        else:
            self.state["bottom_fishing_until"] = ""
        self.save_state()

    def is_bottom_fishing_active(self):
        """Bottom Fishing 모드가 켜져 있고 만료되지 않았는지 확인. 만료됐으면 자동으로 꺼줌."""
        if not self.state.get("is_bottom_fishing_mode", False):
            return False
        until_str = self.state.get("bottom_fishing_until", "")
        if not until_str:
            return True
        kst = pytz.timezone('Asia/Seoul')
        try:
            until = datetime.fromisoformat(until_str)
            if until.tzinfo is None:
                until = kst.localize(until)
            if datetime.now(kst) >= until:
                self.state["is_bottom_fishing_mode"] = False
                self.state["bottom_fishing_until"] = ""
                self.save_state()
                return False
            return True
        except Exception:
            return True

