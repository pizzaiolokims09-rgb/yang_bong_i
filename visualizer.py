import matplotlib.pyplot as plt
import seaborn as sns
import numpy as np
import os
import platform

# 한글 폰트 깨짐 방지 설정
if platform.system() == 'Windows':
    plt.rcParams['font.family'] = 'Malgun Gothic'
elif platform.system() == 'Darwin':
    plt.rcParams['font.family'] = 'AppleGothic'
else:
    # Linux (AWS 환경 대비)
    plt.rcParams['font.family'] = 'NanumGothic'

plt.rcParams['axes.unicode_minus'] = False 

class PortfolioVisualizer:
    @staticmethod
    def generate_report(balance_data, history_data=None, filename="portfolio_report.png"):
        """
        차트 2개(파이차트, 라인차트)를 그려서 png로 저장합니다.
        """
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))
        
        # 1. 파이 차트 (자산별 비중)
        labels = []
        sizes = []
        
        if "assets" in balance_data and balance_data["assets"]:
            for ticker, info in balance_data["assets"].items():
                labels.append(info["name"])
                # KIS API 잔고는 quantity와 price로 들어옴
                val = info.get("quantity", 0) * info.get("price", 0)
                sizes.append(val)
        else:
            labels = ["현금"]
            sizes = [balance_data.get("cash", 1)]
            
        # 가용 현금도 비중에 포함
        cash = balance_data.get("cash", 0)
        if cash > 0 and len(labels) > 0 and labels[0] != "현금":
            labels.append("가용 현금")
            sizes.append(cash)

        # 차트 A 그리기
        ax1.pie(sizes, labels=labels, autopct='%1.1f%%', startangle=90, colors=sns.color_palette("pastel"))
        ax1.axis('equal')  
        ax1.set_title('현재 포트폴리오 비중')

        # 2. 라인 차트 (계좌 누적 수익률 추이)
        if not history_data:
            history_data = [0.0]
            
        x = np.arange(len(history_data))
        ax2.plot(x, history_data, marker='o', linestyle='-', color='r', linewidth=2)
        ax2.set_title('계좌 누적 수익률 추이 (%)')
        ax2.set_xlabel('기간 (최근 리밸런싱 회차)')
        ax2.set_ylabel('누적 수익률 (%)')
        ax2.grid(True, linestyle='--', alpha=0.6)

        plt.tight_layout()
        plt.savefig(filename, dpi=150)
        plt.clf()
        plt.close('all')
        
        return filename
