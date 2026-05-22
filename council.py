import json
import logging
import asyncio
import requests

class MultiAssetCouncil:
    def __init__(self, api_key):
        self.api_key = api_key
        # [파이널봇 패턴 적용] 상황별 모델 분리
        self.model_thinking = 'gemini-3.1-pro-preview'    # 전략 회의 (정밀 판단)
        self.model_flash = 'gemini-3-flash-preview'       # 경량 작업 (초고속)
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/models"
        
        self.personas = {
            "macro": "[거시경제 분석가] 금리, 인플레이션, 통화 정책 분석 전문.",
            "geopolitics": "[지정학적 감시관] 글로벌 리스크 및 선거 감시 전문.",
            "asset_analyst": "[개별 자산 분석가] 종목별 모멘텀 및 펀더멘털 분석 전문.",
            "liquidity": "[유동성 감시관] 달러 인덱스, M2 등 자금 흐름 추적 전문.",
            "risk_manager": "[리스크 관리자] 자산 간 동조화 및 하락장 관리 전략 전문.",
            "sentiment": "[시장 심리 분석가] 공포/탐욕 지수 및 과열 판별 전문."
        }
        
        # 시스템 지침 (system_instruction으로 분리 - 파이널봇 패턴)
        self.system_instruction = """[시스템 역할: 양봉이 AI 투자 심의회]
당신은 포트폴리오를 관리하는 '양봉이' AI 투자 심의회입니다.
6명의 전문 에이전트(거시경제, 지정학, 자산분석, 유동성, 리스크관리, 시장심리)가 회의를 통해 동적 비중 조절을 결정합니다.

[핵심 행동 규칙]
1. 너는 6인 전문가 위원회다. 제공된 과거 회의록을 복기하여 이전 판단의 성공/실패를 학습하라.
2. 각 자산의 현재 비중이 목표 대비 ±5%p 이상 벗어났을 때만 조정을 지시하라.
3. 특정 종목 수익률이 10% 초과 시, 추세적 상승인지 단기 과열인지 판단하라.
4. 리밸런싱이 불필요하면 "action": "HOLD"를, 필요하면 "action": "REBALANCE"를 반환하라.
5. 위기 상태(CRISIS_MODE) 종목의 변동성이 잦아들면 "action": "CRISIS_RESOLVED"를 반환하라.
6. 리밸런싱 시 포트폴리오 비중 합계는 반드시 100%여야 한다.
7. 모든 분석 결과는 반드시 한국어로 작성하라.
"""

    def _call_api_sync(self, prompt, model_type="flash", use_json_mode=True):
        """파이널봇 패턴: requests 동기 호출 + system_instruction + response_mime_type + 재시도/폴백 로직"""
        if not self.api_key:
            return {}
        
        target_model = self.model_thinking if model_type == "thinking" else self.model_flash
        fallback_model = self.model_flash  # 503/Timeout 지속 시 빠르고 가벼운 모델로 우회
        
        headers = {'Content-Type': 'application/json'}
        gen_config = {"temperature": 0.1}
        if use_json_mode:
            gen_config["response_mime_type"] = "application/json"
        
        payload = {
            "system_instruction": {"parts": [{"text": self.system_instruction}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": gen_config
        }
        
        timeout_val = 120 if model_type == "thinking" else 60
        max_retries = 3
        
        import time
        for attempt in range(max_retries):
            # 1, 2회차는 원래 타겟 모델, 3회차(마지막)는 Fallback 모델 사용
            current_model = target_model if attempt < 2 else fallback_model
            url = f"{self.base_url}/{current_model}:generateContent?key={self.api_key}"
            
            logging.info(f"Gemini API 호출 ({current_model}) - 시도 {attempt+1}/{max_retries}, timeout: {timeout_val}s")
            
            try:
                response = requests.post(url, headers=headers, json=payload, timeout=timeout_val)
                
                if response.status_code == 429:
                    logging.warning(f"Gemini API Rate Limit (429). 15초 대기 후 재시도... (시도 {attempt+1})")
                    time.sleep(15)
                    continue
                
                if response.status_code >= 500:
                    logging.warning(f"Gemini API 서버 오류 ({response.status_code}). 10초 대기 후 재시도... (시도 {attempt+1})")
                    time.sleep(10)
                    continue
                    
                response.raise_for_status()
                data = response.json()
                
                if "candidates" in data and data["candidates"]:
                    return data["candidates"][0]["content"]["parts"][0]["text"].strip()
                return ""
                
            except requests.exceptions.RequestException as e:
                logging.warning(f"Gemini API 네트워크 오류 또는 타임아웃 발생 (시도 {attempt+1}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(10)
                else:
                    logging.error("Gemini API 최대 재시도 횟수 초과. 분석 실패.")
                    raise e
        return ""

    async def _call_gemini(self, prompt, model_type="flash", use_json_mode=True):
        """비동기 래퍼: 동기 requests 호출을 이벤트 루프 차단 없이 실행"""
        return await asyncio.to_thread(self._call_api_sync, prompt, model_type, use_json_mode)

    def _parse_json_response(self, text):
        """응답 텍스트에서 JSON 추출"""
        if text.startswith("```json"): text = text[7:]
        if text.startswith("```"): text = text[3:]
        if text.endswith("```"): text = text[:-3]
        start = text.find('{')
        end = text.rfind('}') + 1
        return json.loads(text[start:end])

    async def generate_rebalance_decision(self, state_manager, market_data, balance_data):
        """
        과거 회의록과 동적 설정을 참조하여 리밸런싱을 결정합니다.
        gemini-3.1-pro-preview의 Thinking 능력을 활용합니다.
        """
        logging.info("Gemini AI 위원회 전략 회의 시작 (고도화 버전 - Thinking 모델)...")
        
        config = state_manager.portfolio_config
        recent_memory = state_manager.get_recent_memory(limit=3)
        user_instructions = state_manager.get_instructions_context()
        
        current_status = json.dumps(balance_data, indent=2, ensure_ascii=False)
        target_weights = json.dumps(config, indent=2, ensure_ascii=False)
        
        prompt = f"""[6인 에이전트 구성]
1. {self.personas['macro']}
2. {self.personas['geopolitics']}
3. {self.personas['asset_analyst']}
4. {self.personas['liquidity']}
5. {self.personas['risk_manager']}
6. {self.personas['sentiment']}

[기본 하드코딩 관리 종목 및 기초 비중]
- 지수: 일본니케이225(8.5%), 차이나CSI300(8.5%), K200TR(8.5%), 미국나스닥100(8.5%), 인도Nifty50(8.5%), 미국배당다우존스(8.5%)
- 금: ACE KRX금현물(19.0%)
- 채권: 국고채30년스트립(15.0%), 미국채30년액티브(15.0%)
* 주의: 위 기본 종목 중 현재 포트폴리오 설정에 없는 종목은 사용자가 텔레그램으로 제외한 것이며, 반대로 위 목록에 없는 종목이 있다면 사용자가 텔레그램을 통해 신규 편입시킨 것입니다.

[현재 목표 비중 설정 (동적 포트폴리오 설정)]
{target_weights}

[현재 계좌 잔고 및 수익률 현황]
{current_status}

[현재 시장 지표 (참고용 데이터)]
{json.dumps(market_data, indent=2, ensure_ascii=False)}

[과거 회의록 (Memory)]
{recent_memory}

[상사님의 특별 지침 (최우선 순위)]
{user_instructions}
* 주의: 위 지침은 리밸런싱 결정 시 시스템의 기본 규칙보다 우선하여 반드시 준수해야 하는 '특별법'입니다.

[응답 형식 - JSON]
{{
  "action": "REBALANCE" | "HOLD" | "CRISIS_RESOLVED",
  "target_weights": {{
    "미국나스닥100": 8.5,
    "ACE KRX금현물": 19.0
  }},
  "new_meeting_minutes": "과거 회의 복기 및 현재 시장 상황에 따른 위원회의 주요 결정 사항 및 이유 (한국어 요약본)"
}}"""
        try:
            text = await self._call_gemini(prompt, model_type="thinking", use_json_mode=True)
            result = self._parse_json_response(text)
            
            return {
                "action": result.get("action", "HOLD"),
                "weights": result.get("target_weights", {}),
                "minutes": result.get("new_meeting_minutes", "AI 분석 결과 요약 없음")
            }
        except Exception as e:
            logging.error(f"Gemini API 파싱 오류: {e}")
            return {
                "action": "ERROR",
                "weights": {},
                "minutes": f"분석 중 오류 발생: {str(e)}"
            }

    async def generate_drip_decision(self, state_manager, balance_data, dividend_amount):
        """배당금(여유 현금) 발생 시 최적 매수 종목 선정 (Flash 모델 사용)"""
        logging.info("배당금 타겟 스나이핑 (Smart DRIP) 분석 시작...")
        
        prompt = f"""당신은 글로벌 자산 포트폴리오의 자본 효율성을 극대화하는 AI 매니저 '양봉이'입니다.
최근 계좌에 {dividend_amount:,}원의 새로운 현금(배당금 등)이 입금되었습니다.

[현재 포트폴리오 설정 및 비중]
{json.dumps(state_manager.portfolio_config, ensure_ascii=False, indent=2)}

[현재 계좌 잔고 현황 (수익률 포함)]
{json.dumps(balance_data, ensure_ascii=False, indent=2)}

[스마트 스나이핑 규칙]
포트폴리오 종목 중 목표 비중 대비 가장 많이 뒤처져 있는 단 1개의 기회 종목을 선정하라.

[응답 형식 - JSON]
{{
  "target_asset": "선정된 종목명 (반드시 config에 있는 한글 종목명)",
  "reason": "선정 이유 (한국어 1~2줄)"
}}"""
        try:
            text = await self._call_gemini(prompt, model_type="flash", use_json_mode=True)
            return self._parse_json_response(text)
        except Exception as e:
            logging.error(f"DRIP 전략 생성 오류: {e}")
            return None

    async def summarize_instructions(self, instruction_list, is_merge=False):
        """다수의 지침을 하나의 요약된 영구 지침으로 압축 (Flash 모델 사용)"""
        logging.info(f"지침 {'병합' if is_merge else '요약'} 프로세스 시작...")
        
        prompt = f"""당신은 '양봉이' AI 위원회의 서기이자 요약 전문가입니다.
아래 리스트업 된 사용자의 개별 지침들을 분석하여, 핵심 의도와 수치, 규칙이 훼손되지 않도록 하나의 '통합 지침'으로 요약하세요.

[요약 대상 지침들]
{json.dumps(instruction_list, ensure_ascii=False, indent=2)}

[요약 규칙]
1. 불필요한 인사말이나 수식어는 생략하고 핵심 '투자 규칙' 위주로 기술하세요.
2. 특정 종목 티커나 비중(%) 수치가 있다면 절대 누락하지 마세요.
3. 결과는 순수 텍스트 1~3줄로만 출력하세요.

결과:"""
        try:
            text = await self._call_gemini(prompt, model_type="flash", use_json_mode=False)
            return text
        except Exception as e:
            logging.error(f"지침 요약 중 오류 발생: {e}")
            return None
