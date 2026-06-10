import requests
import json
import logging
import time
import asyncio
import pytz
import datetime

class KISAPIError(Exception):
    """KIS API 통신 에러 및 서버 점검 시 발생하는 커스텀 예외"""
    pass

class KISBroker:
    def __init__(self, api_key, api_secret, account_no, is_paper=True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.account_no = account_no
        self.is_paper = is_paper
        self.base_url = "https://openapivts.koreainvestment.com:29443" if is_paper else "https://openapi.koreainvestment.com:9443"
        self.token = None
        self.token_exp = 0
        self.is_emergency_stop = False # 킬스위치 감지용
        self.state_manager = None # Rate Limiter 연동용
        self.bot = None # 긴급 텔레그램 연동용

    def _safe_request(self, method, url, **kwargs):
        """네트워크 오류 및 서버 측 HTTP 오류를 한꺼번에 잡아서 커스텀 예외 발생 + 자동 재시도(Max 3회)"""
        # [패치] KIS API 쿨다운 중이면 불필요한 재시도 없이 즉시 차단
        if self.state_manager and self.state_manager.is_kis_in_cooldown():
            raise KISAPIError("KIS API 쿨다운 중입니다. 잠시 후 자동으로 재개됩니다.")
        
        max_retries = 3  # [패치] 5회 → 3회로 축소
        retry_delay = 2  # [패치] 1초 → 2초 (지수 백오프 기본값)
        
        for attempt in range(max_retries):
            try:
                if method == "GET":
                    res = requests.get(url, timeout=10, **kwargs)
                else:
                    res = requests.post(url, timeout=10, **kwargs)
                    
                # 성공 응답 (200 OK)
                if res.status_code == 200:
                    # 200이더라도 내부 rt_cd가 1이고 만료 에러일 수 있으므로 체크 (가끔 200으로 내려옴)
                    try:
                        data = res.json()
                        if data.get("msg_cd") == "EGW00123" or "만료된 token" in data.get("msg1", ""):
                            logging.info("🔄 [토큰 만료 감지] API 응답에서 토큰 만료를 감지했습니다. 즉시 재발급을 시도합니다.")
                            self.auth()
                            if "headers" in kwargs and "authorization" in kwargs["headers"]:
                                kwargs["headers"]["authorization"] = f"Bearer {self.token}"
                            continue
                            
                        # KIS API 특성상 200 OK라도 rt_cd가 1이고 초과 메시지가 올 수 있음
                        if data.get("rt_cd") == "1" and ("초과" in data.get("msg1", "") or "초당" in data.get("msg1", "")):
                            import random
                            jitter = random.uniform(0.1, 0.5)
                            logging.warning(f"⚠️ [KIS API 일시적 오류] 초당 거래건수 초과 감지. {attempt + 1}/{max_retries}회차 재시도 중... (MSG: {data.get('msg1')})")
                            if attempt < max_retries - 1:
                                time.sleep(retry_delay + jitter)
                                continue
                            raise KISAPIError(f"Rate Limit Exceeded: {data.get('msg1')}")
                    except Exception as e:
                        if isinstance(e, KISAPIError):
                            raise e
                        pass
                    return res
                
                # [패치] HTTP 403 접근 차단(EGW00133 등) 즉시 쿨다운 가동, 재시도 없음
                if res.status_code == 403:
                    error_text = res.text[:200]
                    logging.error(f"🚫 [KIS API 403 차단] 접근이 거부되었습니다. 15분 쿨다운 가동. 응답: {error_text}")
                    if self.state_manager:
                        self.state_manager.set_kis_cooldown(900)
                        self.state_manager.record_kis_failure()
                    raise KISAPIError(f"HTTP 403 Forbidden: {error_text}")
                
                # 토큰 만료 에러 감지 (HTTP 500으로 내려올 경우)
                if res.status_code == 500 and ("EGW00123" in res.text or "만료된 token" in res.text):
                    logging.info("🔄 [토큰 만료 감지] HTTP 500 상태에서 토큰 만료를 감지했습니다. 즉시 재발급을 시도합니다.")
                    self.auth()
                    if "headers" in kwargs and "authorization" in kwargs["headers"]:
                        kwargs["headers"]["authorization"] = f"Bearer {self.token}"
                    continue

                # 재시도 대상 에러 코드 (429: Too Many Requests, 500-504: Server Errors)
                if res.status_code in [429, 500, 502, 503, 504]:
                    import random
                    jitter = random.uniform(0.1, 0.5)
                    logging.warning(f"⚠️ [KIS API 일시적 오류] ({res.status_code}) 서버 부하 감지. {attempt + 1}/{max_retries}회차 재시도 중... (URL: {url}, MSG: {res.text[:100]})")
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay * (2 ** attempt) + jitter)  # [패치] 지수 백오프 + Jitter
                        continue
                        
                # 재시도 불가능한 에러이거나 최대 횟수 초과 시 예외 발생
                raise KISAPIError(f"HTTP {res.status_code} Error: KIS 서버 과부하 또는 점검 중. 최종 응답: {res.text[:100]}")
                
            except requests.exceptions.RequestException as e:
                logging.warning(f"🌐 [네트워크 오류] ({str(e)}) 연결 실패. {attempt + 1}/{max_retries}회차 재시도 중...")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay * (2 ** attempt))  # [패치] 지수 백오프
                    continue
                if self.state_manager:
                    self.state_manager.record_kis_failure()
                raise KISAPIError(f"Network Error: KIS API 연결 실패 ({str(e)})")
        
        # 이론적으로 여기 도달하면 안 됨 (최종 에러가 raise되어야 함)
        raise KISAPIError("Unexpected flow in _safe_request")

    def auth(self):
        """인증 토큰 발급 (P/S 토큰 TR: /oauth2/tokenP)"""
        url = f"{self.base_url}/oauth2/tokenP"
        data = {
            "grant_type": "client_credentials",
            "appkey": self.api_key,
            "appsecret": self.api_secret
        }
        res = self._safe_request("POST", url, data=json.dumps(data))
        res_data = res.json()
        self.token = res_data["access_token"]
        self.token_exp = time.time() + res_data["expires_in"] - 60
        # [패치] 토큰 발급 성공 시 연속 실패 카운터 초기화
        if self.state_manager:
            self.state_manager.record_kis_success()
        logging.info("📢 KIS API 인증 토큰 발급 성공")

    def _get_headers(self, tr_id):
        headers = {
            "Content-Type": "application/json",
            "authorization": f"Bearer {self.token}",
            "appkey": self.api_key,
            "appsecret": self.api_secret,
            "tr_id": tr_id,
            "custtype": "P"
        }
        return headers

    def get_balance(self):
        """계좌 잔고 조회 (주식잔고조회 TR: TTTC8434R / VTTC8434R)
        [고도화] 연속조회(페이징) 처리: 보유 종목이 많을 경우 한 번의 API 호출로
        전부 가져오지 못하므로, CTX_AREA_FK100/NK100 키를 이용해 다음 페이지를 반복 조회합니다.
        """
        if time.time() > self.token_exp:
            logging.info("🔄 [토큰 만료] 잔고 조회 전 토큰 재발급을 시도합니다.")
            self.auth()
        
        tr_id = "VTTC8434R" if self.is_paper else "TTTC8434R"
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        
        # 전체 종목 데이터를 누적할 리스트
        all_output1 = []
        summary_data = None
        
        ctx_area_fk100 = ""
        ctx_area_nk100 = ""
        page = 0
        max_pages = 10  # 무한 루프 방지 (종목 200개까지 대응)
        
        while page < max_pages:
            params = {
                "CANO": self.account_no[:8],
                "ACNT_PRDT_CD": self.account_no[8:],
                "AFHR_FLG": "N",
                "OFRT_BLAM_OTIC_FLG": "N",
                "PRCS_DVSN": "01",
                "UNPR_DVSN": "01",
                "CTX_AREA_FK100": ctx_area_fk100,
                "CTX_AREA_NK100": ctx_area_nk100
            }
            
            res = self._safe_request("GET", url, headers=self._get_headers(tr_id), params=params)
            data = res.json()
            
            if page == 0:
                if "output2" not in data or len(data["output2"]) == 0:
                    error_msg = data.get("msg1", "Unknown Error")
                    logging.error(f"❌ [잔고 조회 실패] API 응답에 'output2'가 없습니다. 사유: {error_msg} (rt_cd: {data.get('rt_cd')})")
                    return None
                summary_data = data["output2"][0]
            
            # output1(종목 목록)을 누적
            page_items = data.get("output1", [])
            all_output1.extend(page_items)
            
            # 연속조회 키 확인: 값이 비어있으면 마지막 페이지
            next_fk = data.get("ctx_area_fk100", "").strip()
            next_nk = data.get("ctx_area_nk100", "").strip()
            
            if not next_fk and not next_nk:
                break
            if next_fk == ctx_area_fk100 and next_nk == ctx_area_nk100:
                break  # 키가 안 바뀌면 무한루프 방지
            
            ctx_area_fk100 = next_fk
            ctx_area_nk100 = next_nk
            page += 1
            
            if page > 1:
                logging.info(f"📄 [잔고 연속조회] {page}페이지 추가 조회 중... (누적 {len(all_output1)}건)")
            time.sleep(1.0)  # [패치] API Rate Limit 2 TPS 준수를 위해 0.3초 -> 1.0초로 상향 조정
        
        if page > 0:
            logging.info(f"📄 [잔고 조회 완료] 총 {page + 1}페이지, {len(all_output1)}개 종목 데이터 수집 완료")
        
        # [수정] 잔고 조회 성공 시 연속 실패 카운터 초기화 (쿨다운 해제 후 카운터 쌓임 방지)
        if self.state_manager:
            self.state_manager.record_kis_success()
        
        try:
            # [output2] 계좌 전체 요약 정보
            # KIS API 필드 참고:
            # - dnca_tot_amt: 예수금 총액 (주문 가능 현금) ★
            # - scts_evlu_amt: 유가증권 평가금액 (주식만)
            # - tot_evlu_amt: 총 평가 = 주식평가 + 예수금 (≒ 순자산)
            # - nass_amt: 순자산 (= tot_evlu_amt)
            
            # 1. 총 자산 (순자산 = 주식 + 현금)
            total_value = int(summary_data.get("nass_amt", 0))
            if total_value == 0:
                total_value = int(summary_data.get("tot_evlu_amt", 0))
            
            # 실전(TTTC8434R) vs 모의(VTTC8434R) 필드명 차이 대응
            total_pnl = int(summary_data.get("evlu_pfls_smtl_amt", summary_data.get("tot_evlu_pfls_amt", 0)))
            total_pft_rt = float(summary_data.get("evlu_erng_rt", summary_data.get("tot_pft_rt", 0.0)))
            
            # 2. 종목별 보유 내역 (페이징 중복 방지: pdno 키 기준 dict 사용)
            assets = {}
            for item in all_output1:
                qty = int(item.get("hldg_qty", 0))
                if qty > 0:
                    pdno = item["pdno"]
                    price = int(item["prpr"])
                    assets[pdno] = {
                        "name": item["prdt_name"],
                        "quantity": qty,
                        "price": price,
                        "profit_rate": float(item["evlu_pfls_rt"]),
                        "pnl_amount": int(item.get("evlu_pfls_amt", 0))
                    }
            
            # 3. stocks_value를 중복 없는 assets dict에서 계산 (페이징 중복 합산 버그 방지)
            stocks_value = sum(info["quantity"] * info["price"] for info in assets.values())
                    
            # 4. 현금 = KIS 예수금 필드 직접 사용 (가장 정확)
            api_cash = int(summary_data.get("dnca_tot_amt", 0))
            
            # 백업: API 예수금 필드가 0이면 역산
            if api_cash == 0:
                api_cash = max(total_value - stocks_value, 0)
            
            cash = api_cash
            
            logging.info(f"[잔고] 총자산: {total_value:,}원, 주식: {stocks_value:,}원, 현금(예수금): {cash:,}원, 보유종목: {len(assets)}개")
            
            # [수정] 수익률이 0으로 찍히는 버그 대응: 시드 대비 직접 계산
            if total_pft_rt == 0.0:
                seed = self.state_manager.state.get("seed_amount", 0)
                if seed > 0:
                    total_pft_rt = (total_pnl / seed) * 100
            
            return {
                "total_value": total_value, 
                "total_pnl": total_pnl, 
                "total_pft_rt": total_pft_rt,
                "cash": cash, 
                "assets": assets
            }
        except (KeyError, IndexError, TypeError) as e:
            logging.error(f"🚨 [잔고 파싱 에러] 데이터 구조가 예상과 다릅니다: {e}")
            return None

    def place_order(self, ticker, quantity, side="BUY", order_type="01", price=0):
        """
        주문 집행 (주식주문 TR: TTTC0802U / VTTC0802U)
        order_type: "01"(시장가), "00"(지정가)
        """
        if self.is_emergency_stop:
            logging.error(f"🚨 [긴급 차단] 킬스위치 작동 중. {ticker} {side} 시장가 주문 전송이 차단되었습니다.")
            return {"rt_cd": "1", "msg1": "EMERGENCY_STOP"}

        # Rate Limiter 검사
        if self.state_manager:
            price_val = self.get_price(ticker)
            trade_amount = price_val * quantity
            ok, reason = self.state_manager.check_and_update_rate_limit(trade_amount)
            if not ok:
                msg = f"⛔ [치명적 오류] 일일 최대 매매 횟수(200회) 또는 금액(시드 400%)을 초과하여 주문을 전면 차단합니다. 로직 무한 루프 가능성을 점검하세요!\n(사유: {reason}, 종목: {ticker})"
                logging.error(msg)
                self.is_emergency_stop = True
                self.state_manager.trigger_panic()
                if self.bot:
                    import asyncio
                    try:
                        asyncio.create_task(self.bot.send_notification(msg))
                    except Exception:
                        pass
                return {"rt_cd": "1", "msg1": reason}

        if time.time() > self.token_exp: self.auth()
        
        tr_id = ""
        if side == "BUY":
            tr_id = "VTTC0802U" if self.is_paper else "TTTC0802U"
        else:
            tr_id = "VTTC0801U" if self.is_paper else "TTTC0801U"
            
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        data = {
            "CANO": self.account_no[:8],
            "ACNT_PRDT_CD": self.account_no[8:],
            "PDNO": ticker,
            "ORD_DVSN": order_type,
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(price) if order_type == "00" else "0"
        }
        res = self._safe_request("POST", url, headers=self._get_headers(tr_id), data=json.dumps(data))
        return res.json()

    def get_price(self, ticker):
        """현재가 조회 (주식현재가 시세 TR: FHKST01010100)"""
        if time.time() > self.token_exp: self.auth()
        
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        headers = self._get_headers("FHKST01010100")
        params = {
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": ticker
        }
        res = self._safe_request("GET", url, headers=headers, params=params)
        data = res.json()
        return int(data["output"]["stck_prpr"])

    def cancel_order(self, odno, org_no):
        """주문 취소 (주식주문취소 TR: TTTC0803U / VTTC0803U)"""
        if time.time() > self.token_exp: self.auth()
        
        tr_id = "VTTC0803U" if self.is_paper else "TTTC0803U"
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-rvsecncl"
        data = {
            "CANO": self.account_no[:8],
            "ACNT_PRDT_CD": self.account_no[8:],
            "KRX_FWDG_ORD_ORG_NO": org_no,
            "ORGN_ORD_NO": odno,
            "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": "0",
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_FLG": "Y"
        }
        res = self._safe_request("POST", url, headers=self._get_headers(tr_id), data=json.dumps(data))
        return res.json()

    def get_unexecuted_orders(self):
        """미체결내역 조회 (주식정정취소가능주문조회 TR: TTTC0084R / VTTC8011R)"""
        if time.time() > self.token_exp: self.auth()
        
        # [수정] 실전투자 404 에러 해결: 실전용 엔드포인트와 TR ID 적용
        tr_id = "VTTC8011R" if self.is_paper else "TTTC0084R"
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl"
        params = {
            "CANO": self.account_no[:8],
            "ACNT_PRDT_CD": self.account_no[8:],
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
            "INQR_DVSN_1": "01",
            "INQR_DVSN_2": "00"
        }
        res = self._safe_request("GET", url, headers=self._get_headers(tr_id), params=params)
        data = res.json()
        if "output" in data:
            return data["output"]
        return []

    def get_today_orders_stats(self):
        """오늘치 체결내역을 조회하여 체결 횟수와 누적 거래대금 반환 (TTTC8001R / VTTC8001R)"""
        if time.time() > self.token_exp: self.auth()
        kst = pytz.timezone('Asia/Seoul')
        today = datetime.datetime.now(kst).strftime("%Y%m%d")
        
        tr_id = "VTTC8001R" if self.is_paper else "TTTC8001R"
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
        params = {
            "CANO": self.account_no[:8],
            "ACNT_PRDT_CD": self.account_no[8:],
            "INQR_STRT_DT": today,
            "INQR_END_DT": today,
            "SLL_BUY_DVSN_CD": "00",
            "INQR_DVSN": "01",
            "PDNO": "",
            "CCLD_DVSN": "01",
            "ORD_GNO_BRNO": "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": ""
        }
        res = self._safe_request("GET", url, headers=self._get_headers(tr_id), params=params)
        data = res.json()
        
        count = 0
        volume = 0
        if "output1" in data:
            for item in data["output1"]:
                count += 1
                volume += int(item.get("tot_ccld_amt", 0))
        return count, volume
