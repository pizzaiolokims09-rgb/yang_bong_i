import json
import logging
import asyncio
import requests

class TrendHunter:
    def __init__(self, api_key, state_manager=None):
        self.api_key = api_key
        self.state_manager = state_manager
        self.model_flash = 'gemini-3-flash-preview'
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/models"

    def _call_api_sync(self, prompt, use_json_mode=True):
        """Gemini API 동기 호출 + 쿨다운/할당량 사전 차단 + 지수 백오프"""
        # [패치] Gemini API 쿨다운 및 일일 할당량 사전 체크
        if self.state_manager:
            if self.state_manager.is_gemini_in_cooldown():
                logging.warning("[TrendHunter 쿨다운] Gemini API 냉각 중. 호출 스킵.")
                return ""
            if not self.state_manager.check_and_increment_gemini_call():
                logging.warning("[TrendHunter 할당량 초과] 일일 호출 한도 초과. 호출 스킵.")
                return ""
        
        url = f"{self.base_url}/{self.model_flash}:generateContent?key={self.api_key}"
        headers = {'Content-Type': 'application/json'}
        
        gen_config = {"temperature": 0.3}
        if use_json_mode:
            gen_config["response_mime_type"] = "application/json"
        
        payload = {
            "system_instruction": {"parts": [{"text": "당신은 글로벌 퀀트 투자 분석가이자 '트렌드 헌터' 에이전트입니다. 모든 답변은 한국어로 작성하십시오."}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": gen_config
        }
        
        max_retries = 3
        import time
        for attempt in range(max_retries):
            try:
                response = requests.post(url, headers=headers, json=payload, timeout=60)
                
                # [패치] 재시도 불가 클라이언트 오류 - 즉시 중단 + 쿨다운
                if response.status_code in [400, 401, 403]:
                    logging.error(f"TrendHunter API 인증/권한 오류 ({response.status_code}). 1시간 쿨다운.")
                    if self.state_manager:
                        self.state_manager.set_gemini_cooldown(3600)
                    return ""
                
                if response.status_code == 429:
                    logging.warning(f"TrendHunter API Rate Limit (429). 대기 후 재시도... (시도 {attempt+1})")
                    if attempt >= max_retries - 1:
                        if self.state_manager:
                            self.state_manager.set_gemini_cooldown(3600)
                        return ""
                    time.sleep(10 * (attempt + 1))
                    continue
                    
                if response.status_code >= 500:
                    logging.warning(f"TrendHunter API 서버 오류 ({response.status_code}). 대기 후 재시도... (시도 {attempt+1})")
                    if attempt < max_retries - 1:
                        time.sleep(10 * (attempt + 1))
                        continue
                    return ""
                    
                response.raise_for_status()
                data = response.json()
                
                if "candidates" in data and data["candidates"]:
                    return data["candidates"][0]["content"]["parts"][0]["text"].strip()
                return ""
                
            except requests.exceptions.RequestException as e:
                logging.warning(f"TrendHunter API 통신 오류 발생 (시도 {attempt+1}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(10 * (attempt + 1))
                else:
                    logging.error("TrendHunter API 최대 재시도 횟수 초과.")
                    return ""  # [패치] raise 대신 안전한 반환
        return ""

    async def _call_gemini(self, prompt, use_json_mode=True):
        """비동기 래퍼"""
        return await asyncio.to_thread(self._call_api_sync, prompt, use_json_mode)

    async def generate_monthly_recommendation(self, current_config):
        """현재 포트폴리오를 기반으로 새로운 ETF 유망 종목을 추천합니다."""
        logging.info("월간 알파 종목 발굴 중 (Trend Hunter)...")
        
        prompt = f"""[시장 및 통화 제한 - CRUCIAL]
네가 추천하는 모든 종목은 반드시 한국 거래소(KRX)에 상장되어 원화(KRW)로 거래되는 국내 운용사의 ETF여야 한다.
- 미국 직상장 ETF (예: SPY, QQQ, TLT 등)는 절대 추천하지 마라.
- 종목명과 함께 국내 주식용 6자리 숫자 종목코드를 'ticker' 필드에 명시하라.

[현재 포트폴리오 설정]
{json.dumps(current_config, ensure_ascii=False, indent=2)}

[추천 규칙]
1. 현재 포트폴리오의 사각지대를 보완할 수 있는 새로운 ETF를 1~3개 추천하라.
2. 기존 자산과 상관관계가 낮아야 한다.
3. 편입 논리적 이유를 각 2줄 이내로 명확히 작성하라.

[응답 형식 - JSON]
{{
  "recommendations": [
    {{
      "name": "종목명 (예: TIGER 글로벌우라늄&원자력)",
      "ticker": "6자리 숫자 (예: 476830)",
      "reason": "편입 논리 (2줄 이내 설명)"
    }}
  ]
}}"""
        try:
            text = await self._call_gemini(prompt, use_json_mode=True)
            return self._parse_json(text)
        except Exception as e:
            logging.error(f"TrendHunter 분석 오류: {e}")
            return None

    def _parse_json(self, text):
        if text.startswith("```json"): text = text[7:]
        if text.startswith("```"): text = text[3:]
        if text.endswith("```"): text = text[:-3]
        start = text.find('{')
        end = text.rfind('}') + 1
        return json.loads(text[start:end])

    def format_for_telegram(self, recommendation_json):
        """추천된 JSON 데이터를 텔레그램 메세지 포맷으로 변환합니다."""
        if not recommendation_json or "recommendations" not in recommendation_json:
            return "❌ 추천 종목을 발굴하는 데 실패했습니다. 잠시 후 다시 시도해주세요."
            
        text = "🔭 **[양봉이의 이달의 알파 추천 (Trend Hunter)]**\n\n"
        
        for idx, rec in enumerate(recommendation_json["recommendations"], 1):
            text += f"**{idx}. {rec['name']} ({rec['ticker']})**\n"
            text += f"💡 이유: {rec['reason']}\n\n"
            
        text += "👉 편입을 원하시면 채팅으로 *" + "'1번 추가해서 비중 맞춰줘'" + "* 나 *" + "'[종목명] 편입해줘'" + "* 라고 말씀해 주세요!"
        return text
