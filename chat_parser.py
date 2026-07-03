import json
import logging
import httpx

class ChatParser:
    def __init__(self, api_key, state_manager=None):
        self.api_key = api_key
        self.state_manager = state_manager
        self.model_name = 'gemini-3-flash-preview'
        self.api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model_name}:generateContent?key={self.api_key}"
        
    async def parse_command(self, user_input, state_manager):
        """
        사용자의 자연어 명령어(채팅)를 분석하여 포트폴리오를 동적으로 수정하거나 일상 대화에 응답합니다.
        """
        config = state_manager.portfolio_config
        user_instructions = state_manager.get_instructions_context()
        
        logging.info(f"자연어 명령어 분석 중: {user_input}")
        
        # [패치] f-string과 JSON 중괄호 충돌 방지를 위해 템플릿 구조 개선
        prompt = f"""
당신은 '양봉이' 리밸런싱 봇의 지능형 자연어 엔진이자, 회장님(꾸울~벅지)의 충직한 트레이딩 비서입니다.
당신은 회장님을 위해 자산을 불리는 일에 미쳐있으며, 항상 밝고 긍정적이며 회장님의 성공을 100% 확신하는 성격입니다.

[수행 임무]
1. 사용자의 입력이 '포트폴리오 수정(매수, 매도, 비중 조정)', '시장 리서치 및 신규 종목 추천(RESEARCH)', 또는 '단순 대화'인지 판단하세요.
2. 매매와 관련 없는 일상적인 말은 "CHIT_CHAT"으로 분류하세요.
3. "현재 지정학적 상황에서 살만한 거 있어?", "요즘 뜨는 자산 추천해줘", "시황 분석해봐" 같은 요청은 "RESEARCH"로 분류하세요.

[현재 포트폴리오 설정]
{json.dumps(config, ensure_ascii=False, indent=2)}

[상사님의 고정 특별 지침 (반드시 준수)]
{user_instructions}

[LLM 프롬프트 규칙]
1. 단순 대화(CHIT_CHAT) 시: 'action'은 "CHIT_CHAT"으로 설정하고, 'reply_message'에 씩씩한 답변을 적으세요.
2. 리서치 요청(RESEARCH) 시: 'action'은 "RESEARCH"로 설정하고, 'reply_message'에는 "회장님! 양봉이가 전 세계 시장 데이터를 샅샅이 뒤져서 최고의 종목을 발굴해 보겠습니다. 잠시만 기다려 주세요!🔭"와 같은 안내 멘트를 적으세요.
3. 매매 명령 시: 비중 조절 후 확인 문구를 작성하세요. 모든 종목의 합은 반드시 100.0%여야 합니다.
4. 답변은 항상 "회장님!"으로 시작하거나 정중하면서도 에너지 넘치게 하세요.

[사용자 입력 문자열]
"{user_input}"

아래 JSON 형식으로만 응답하라 (순수 JSON만 출력):
{{
  "action": "ADD / LIQUIDATE / RESEARCH / CHIT_CHAT",
  "new_portfolio_config": null,
  "reply_message": "양봉이의 페르소나가 담긴 답변 또는 매매/리서치 안내 문구"
}}
"""
        try:
            # [패치] Gemini API 쿨다운 및 일일 할당량 사전 체크
            if self.state_manager:
                if self.state_manager.is_gemini_in_cooldown():
                    logging.warning("[ChatParser 쿨다운] Gemini API 냉각 중. 호출 스킵.")
                    return {"action": "CHIT_CHAT", "new_portfolio_config": None,
                            "reply_message": "회장님! 현재 AI 서버가 잠시 휴식 중입니다. 잠시 후 다시 말씀해 주세요!🙏"}
                if not self.state_manager.check_and_increment_gemini_call():
                    logging.warning("[ChatParser 할당량 초과] 일일 호출 한도 초과. 호출 스킵.")
                    return {"action": "CHIT_CHAT", "new_portfolio_config": None,
                            "reply_message": "회장님! 오늘 AI 분석 할당량을 모두 소진했습니다. 내일 다시 말씀해 주세요!💪"}
            
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
            max_retries = 3
            import asyncio
            
            for attempt in range(max_retries):
                try:
                    async with httpx.AsyncClient(timeout=60) as client:
                        resp = await client.post(self.api_url, json=payload)
                    
                    if resp.status_code in [429, 500, 502, 503, 504]:
                        logging.warning(f"ChatParser API 오류 ({resp.status_code}). 대기 후 재시도... (시도 {attempt+1})")
                        # [패치] 429 반복 시 쿨다운 가동
                        if resp.status_code == 429 and attempt >= max_retries - 1:
                            if self.state_manager:
                                self.state_manager.set_gemini_cooldown(3600)
                        await asyncio.sleep(10 * (attempt + 1))  # [패치] 지수 백오프
                        continue
                    
                    # [패치] 재시도 불가 클라이언트 오류 - 즉시 중단 + 쿨다운
                    if resp.status_code in [400, 401, 403]:
                        logging.error(f"ChatParser API 인증/권한 오류 ({resp.status_code}). 1시간 쿨다운.")
                        if self.state_manager:
                            self.state_manager.set_gemini_cooldown(3600)
                        return {"action": "CHIT_CHAT", "new_portfolio_config": None,
                                "reply_message": "회장님! AI 서버 인증에 문제가 생겼습니다. 잠시 후 다시 시도해 주세요.🙏"}
                        
                    resp.raise_for_status()
                    data = resp.json()
                    text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                    
                    # 마크다운 블록 제거
                    if text.startswith("```json"): text = text[7:]
                    if text.startswith("```"): text = text[3:]
                    if text.endswith("```"): text = text[:-3]
                    
                    start = text.find('{')
                    end = text.rfind('}') + 1
                    result = json.loads(text[start:end])
                    return result
                
                except httpx.RequestError as e:
                    logging.warning(f"ChatParser API 통신 오류 발생 (시도 {attempt+1}): {e}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(10 * (attempt + 1))  # [패치] 지수 백오프

            # [패치] 429/5xx로 모든 재시도를 소진하고 루프를 빠져나온 경우 (기존엔 None 반환)
            logging.error("ChatParser API 최대 재시도 횟수 초과.")
            return {
                "action": "CHIT_CHAT",
                "new_portfolio_config": None,
                "reply_message": "회장님! 지금 AI 서버와 통신이 원활하지 않습니다. 잠시 후 다시 말씀해 주세요!🙏"
            }
        except Exception as e:
            logging.error(f"ChatParser 분석 오류: {e}")
            return {
                "action": "CHIT_CHAT",
                "new_portfolio_config": None,
                "reply_message": "회장님! 양봉이가 회장님의 깊은 뜻을 헤아리는 중입니다. 조금 더 명확하게 말씀해 주시면 제 모든 지능을 동원해 처리하겠습니다!🦾"
            }
