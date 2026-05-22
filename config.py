import os
import subprocess
import getpass
from dotenv import load_dotenv
from io import StringIO

def _decrypt_and_load_env():
    """암호화된 .secret/env.enc를 복호화하여 환경변수로 로드 (평문 파일을 디스크에 남기지 않음)"""
    enc_path = os.path.join(os.path.dirname(__file__), ".secret", "env.enc")
    
    if not os.path.exists(enc_path):
        # 암호화 파일이 없으면 기존 .env 방식으로 폴백
        load_dotenv()
        return
    
    # 비밀번호: 환경변수 우선 → 없으면 대화형 입력
    password = os.environ.get("ENV_PASSWORD")
    if not password:
        password = getpass.getpass("🔐 양봉이 ENV 비밀번호를 입력하세요: ")
    
    try:
        result = subprocess.run(
            ["openssl", "enc", "-aes-256-cbc", "-d", "-pbkdf2", "-iter", "100000",
             "-in", enc_path, "-pass", f"pass:{password}"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            raise RuntimeError(f"복호화 실패: {result.stderr.strip()}")
        
        # 복호화된 평문을 메모리에서 직접 파싱하여 os.environ에 주입
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip()
        
    except Exception as e:
        raise RuntimeError(f"🚨 ENV 복호화 오류: {e}")

_decrypt_and_load_env()

# KIS Config
KIS_API_KEY = os.getenv("KIS_API_KEY")
KIS_API_SECRET = os.getenv("KIS_API_SECRET")
KIS_CANO = os.getenv("KIS_CANO")
KIS_ACNT_PRDT_CD = os.getenv("KIS_ACNT_PRDT_CD", "01")
KIS_IS_PAPER = os.getenv("KIS_IS_PAPER", "True").lower() == "true"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Portfolios & Initial Weights
# 관리 종목 및 기초 비중
# 일본 니케이225 (8.5%), 차이나CSI300 (8.5%), 200TR (8.5%)
# 미국나스닥100 (8.5%), 인도Nifty (8.5%), 미국배당다우존스 (8.5%)
# ACE KRX금현물 (19.0%), 국고채30년스트립 (15.0%), 미국채30년액티브 (15.0%)

INITIAL_WEIGHTS = {
    "NIKKEI225": 8.5,
    "CSI300": 8.5,
    "K200TR": 8.5,
    "NASDAQ100": 8.5,
    "NIFTY50": 8.5,
    "DIVIDEND_DOW": 8.5,
    "ACE_KRX_GOLD": 19.0,
    "K_TREASURY_30Y": 15.0,
    "US_TREASURY_30Y": 15.0
}

# Mapping names to KIS Ticker Symbols (Short codes)
ASSET_TICKERS = {
    # 1. 일본 주식 (니케이225)
    "일본니케이225": "241180", 
    "일본 니케이225": "241180",
    "NIKKEI225": "241180",

    # 2. 중국 주식 (차이나CSI300)
    "차이나CSI300": "192090",       
    "CSI300": "192090",

    # 3. 한국 주식 (200TR)
    "200TR": "278530",       
    "K200TR": "278530",

    # 4. 미국 주식 (나스닥100)
    "미국나스닥100": "133690",    
    "NASDAQ100": "133690",

    # 5. 인도 주식 (인도Nifty50)
    "인도Nifty": "453810",      
    "인도Nifty50": "453810",
    "NIFTY50": "453810",

    # 6. 미국 배당 (배당다우존스)
    "미국배당다우존스": "446770", 
    "배당다우존스": "446770",
    "DIVIDEND_DOW": "446770",

    # 7. 안전 자산 (금 현물)
    "ACE KRX금현물": "411060", 
    "금 현물": "411060",

    # 8. 국내 채권 (국고채30년)
    "국고채30년스트립": "451530", 
    "국고채30년": "451530",
    "K_TREASURY_30Y": "451530",

    # 9. 미국 채권 (미국채30년) - TIGER 미국30년국채커버드콜액티브(H)
    "미국채30년프리미엄": "476550",
    "미국채30년액티브": "476550",
    "미국채30년": "476550",
    "US_TREASURY_30Y": "476550",

    # 옵션 및 기타
    "KODEX 200": "069500",
    "국고채30년액티브": "439870"
}

