import os
import logging
from visualizer import PortfolioVisualizer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

class YangbongBot:
    def __init__(self, token, state_manager, broker, council, data_provider, chat_parser, rebalancer, trend_hunter):
        self.token = token
        self.state_manager = state_manager
        self.broker = broker
        self.council = council
        self.data_provider = data_provider
        self.chat_parser = chat_parser
        self.rebalancer = rebalancer
        self.trend_hunter = trend_hunter
        self.is_processing = False  # 동시 실행 방지 락
        
        self.app = ApplicationBuilder().token(token).connect_timeout(30.0).read_timeout(30.0).write_timeout(30.0).build()
        self.app.add_handler(CommandHandler('start', self.start, block=False))
        self.app.add_handler(CommandHandler('status', self.cmd_status, block=False))
        self.app.add_handler(CommandHandler('panic', self.cmd_panic, block=False))
        self.app.add_handler(CommandHandler('report', self.cmd_report, block=False))
        self.app.add_handler(CommandHandler('cmd', self.cmd_instruction, block=False))
        self.app.add_handler(CommandHandler('instruction', self.cmd_instruction, block=False))
        self.app.add_handler(CommandHandler('list', self.cmd_list_instructions, block=False))
        self.app.add_handler(CommandHandler('del', self.cmd_delete_instruction, block=False))
        self.app.add_handler(CommandHandler('clear', self.cmd_clear_instructions, block=False))
        self.app.add_handler(CallbackQueryHandler(self.handle_callback, block=False))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message, block=False))

    def _is_authorized(self, update: Update) -> bool:
        from config import TELEGRAM_CHAT_ID
        if not TELEGRAM_CHAT_ID:
            return True
        if not update.effective_chat:
            return False
        return str(update.effective_chat.id) == str(TELEGRAM_CHAT_ID)

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log the error and send a telegram message to notify the developer."""
        logging.error("Exception while handling an update:", exc_info=context.error)
        
        try:
            from broker_kis import KISAPIError
            error_msg = "⚠️ 봇 내부에서 예기치 않은 오류가 발생했습니다."
            if isinstance(context.error, KISAPIError):
                error_msg = f"⚠️ KIS API 오류가 발생했습니다.\n상세: {context.error}"
                
            if isinstance(update, Update):
                if update.message:
                    await update.message.reply_text(error_msg)
                elif update.callback_query:
                    # 메시지 수정 시도, 실패하면 알림 팝업
                    try:
                        await update.callback_query.edit_message_text(error_msg)
                    except:
                        await update.callback_query.answer(error_msg, show_alert=True)
        except Exception as e:
            logging.error(f"Error handler itself failed: {e}")

    async def start_polling(self):
        logging.info("텔레그램 봇 Polling을 시작합니다...")
        await self.app.initialize()
        
        # 텔레그램 대화 입력창 옆 메뉴(명령어 버튼) 세팅
        commands = [
            BotCommand("start", "메인 메뉴 및 주요 버튼 열기"),
            BotCommand("status", "현재 계좌 상태 및 수익률 확인"),
            BotCommand("panic", "🚨 긴급 정지 (패닉 킬스위치)"),
            BotCommand("report", "포트폴리오 비중 시각화 리포트"),
            BotCommand("report", "포트폴리오 비중 시각화 리포트"),
            BotCommand("cmd", "💡 특별 투자 지침 하달 (/cmd 내용)"),
            BotCommand("list", "📋 현재 등록된 지침 목록 확인"),
            BotCommand("del", "🗑️ 특정 지침 삭제 (/del R1)"),
            BotCommand("clear", "🧹 모든 지침 일괄 삭제"),
        ]
        await self.app.bot.set_my_commands(commands)
        
        await self.app.start()
        await self.app.updater.start_polling()

    async def send_notification(self, text):
        from config import TELEGRAM_CHAT_ID
        if TELEGRAM_CHAT_ID:
            await self.app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode='Markdown')

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        await self.show_balance_info(update.message.reply_text)

    async def cmd_panic(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        await self.ask_panic(update.message.reply_text)

    async def cmd_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        await self.send_visual_report(update.message.reply_photo, update.message.reply_text)

    async def cmd_instruction(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        if not context.args:
            await update.message.reply_text("💡 **사용법**: `/cmd [내용]` 형태로 입력해주세요.\n예: `/cmd 금 비중은 20% 이하로 줄이지 마`", parse_mode='Markdown')
            return
            
        instruction = " ".join(context.args)
        await self._process_instruction(update, instruction)

    async def _process_instruction(self, update, instruction):
        await self.state_manager.add_user_instruction(instruction, self.council.summarize_instructions)
        await update.message.reply_text(
            f"🫡 **알겠습니다, 상사님!**\n\n하달하신 지침을 양봉이의 핵심 매매 원칙에 최우선으로 반영하겠습니다.\n\n📝 **지침 내용**: {instruction}",
            parse_mode='Markdown'
        )

    async def cmd_list_instructions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        p_ins = self.state_manager.state.get("permanent_instructions", [])
        r_ins = self.state_manager.state.get("recent_instructions", [])
        
        if not p_ins and not r_ins:
            await update.message.reply_text("📭 **현재 등록된 지침이 없습니다.**", parse_mode='Markdown')
            return
            
        text = "📋 **양봉이 지침 현황 목록**\n\n"
        if p_ins:
            text += "**[영구 통합 지침]**\n"
            for i, ins in enumerate(p_ins):
                text += f"P{i+1}: {ins}\n"
            text += "\n"
        
        if r_ins:
            text += "**[최근 개별 지침]**\n"
            for i, ins in enumerate(r_ins):
                text += f"R{i+1}: {ins}\n"
        
        text += "\n💡 삭제하려면 `/del R1` 처럼 입력하세요."
        await update.message.reply_text(text, parse_mode='Markdown')

    async def cmd_delete_instruction(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        if not context.args:
            await update.message.reply_text("💡 **사용법**: `/del [코드]` 형태로 입력해주세요.\n예: `/del R1` (최근 지침 1번 삭제)")
            return
            
        code = context.args[0].upper()
        await self._process_delete_instruction(update, code)

    async def _process_delete_instruction(self, update, code):
        try:
            ins_type = code[0] # 'P' or 'R'
            idx = int(code[1:]) - 1
            
            if ins_type not in ['P', 'R']: raise ValueError
            
            removed = self.state_manager.remove_instruction(ins_type, idx)
            if removed:
                await update.message.reply_text(f"🗑️ **삭제 완료**: 해당 지침을 양봉이의 기억에서 제거했습니다.\n\n`{removed}`", parse_mode='Markdown')
            else:
                await update.message.reply_text("❌ 해당 번호의 지침을 찾을 수 없습니다. `/list`로 확인해 주세요.")
        except:
            await update.message.reply_text("❌ 잘못된 형식입니다. `/del R1`과 같이 입력해 주세요.")

    async def cmd_clear_instructions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        # 실수를 방지하기 위해 인자 없이 입력 시 안내만
        if not context.args or context.args[0] != '확정':
            await update.message.reply_text("🧹 **모든 지침을 초기화하시겠습니까?**\n(모든 영구/최근 지침이 즉시 삭제됩니다.)\n\n확정하시려면 `/clear 확정` 이라고 입력해주세요.")
            return
            
        self.state_manager.clear_instructions()
        await update.message.reply_text("🧹 **초기화 완료.** 양봉이의 모든 특별 지침이 비워졌습니다. 이제 백지상태에서 다시 교육을 시작합니다!")

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        keyboard = [
            [
                InlineKeyboardButton("📊 리밸런싱 종목 분석", callback_data='rebalance_info'),
                InlineKeyboardButton("📸 시각화 리포트", callback_data='view_report'),
            ],
            [
                InlineKeyboardButton("🔭 이달의 알파 종목 추천", callback_data='trend_hunter'),
                InlineKeyboardButton("💰 현재 계좌 상태", callback_data='balance_info'),
            ],
            [
                InlineKeyboardButton("🚀 수동 종목 즉시실행", callback_data='ask_execute_now'),
                InlineKeyboardButton("🚨 양봉이 긴급 패닉", callback_data='ask_panic'),
            ],
            [
                InlineKeyboardButton("🌱 시드 자금 추가", callback_data='add_seed'),
                InlineKeyboardButton("🛡️ 수익금 인출(보전)", callback_data='protect_profit'),
            ],
            [
                InlineKeyboardButton("🤖 능동적 리밸런싱 시작/정지", callback_data='toggle_auto'),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            '안녕하세요! 9개 자산 전략적 리밸런싱 봇 **양봉이**입니다.\n'
            f'현재 모드: {"🤖 자동" if self.state_manager.state["is_auto_mode"] else "👤 수동"}\n'
            f'설정 시드: {self.state_manager.state["seed_amount"]:,}원\n\n'
            '아래 버튼을 누르시거나, *"나스닥 팔고 금 10% 추가해줘"* 처럼 채팅으로 지시해 보세요!', 
            reply_markup=reply_markup
        )

    async def ask_panic(self, reply_func):
        keyboard = [
            [InlineKeyboardButton("🚨 네, 정말 긴급 정지합니다", callback_data='confirm_panic')],
            [InlineKeyboardButton("❌ 아니오(취소)", callback_data='cancel_action')]
        ]
        await reply_func("⚠️ **[긴급 패닉]**\n\n봇의 모든 매매와 판단 로직을 강제로 셧다운하시겠습니까?\n(진행 중인 스케줄 및 분할 주문이 파기됩니다.)", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    async def send_visual_report(self, photo_func, text_func):
        await text_func("📊 리포트를 생성 중입니다. 잠시만 기다려주세요...")
        balance = self.broker.get_balance()
        history = self.state_manager.state.get("profit_history", [])
        report_path = PortfolioVisualizer.generate_report(balance, history)
        
        if os.path.exists(report_path):
            with open(report_path, 'rb') as photo:
                await photo_func(photo=photo, caption="📸 현재 포트폴리오 비중 및 수익률 추이 리포트입니다.")
        else:
            await text_func("❌ 리포트 생성에 실패했습니다.")

    async def show_balance_info(self, text_func):
        balance = self.broker.get_balance()
        if not balance:
            await text_func("❌ 잔고 조회에 실패했습니다.")
            return

        total_eval = balance.get('total_value', 0)
        total_pnl = balance.get('total_pnl', 0)
        total_pft_rt = balance.get('total_pft_rt', 0.0)
        cash = balance.get('cash', 0)
        seed = self.state_manager.state.get('seed_amount', 0)
        
        text = (f"🏦 **[실시간 계좌 종합 현황]**\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"💰 **총 자산 가치**: `{total_eval:,.0f}원`\n"
                f"📈 **전체 손익**: `{total_pnl:+,.0f}원` (`{total_pft_rt:+.2f}%`)\n"
                f"💵 **가용 현금**: `{cash:,.0f}원`\n"
                f"🌱 **설정 시드**: `{seed:,.0f}원`\n"
                f"━━━━━━━━━━━━━━━━━━\n\n")

        text += "📊 **보유 비중 및 종목별 손익**\n"
        
        # 주식 항목 출력
        assets = balance.get("assets", {})
        if not assets:
            text += "보유 중인 종목이 없습니다.\n"
        else:
            for ticker, info in assets.items():
                eval_val = info.get("quantity", 0) * info.get("price", 0)
                weight = (eval_val / total_eval) * 100 if total_eval > 0 else 0
                pnl = info.get("pnl_amount", 0)
                rate = info.get("profit_rate", 0.0)
                
                text += (f"🔹 **{info['name']}**: `{weight:.1f}%` ({eval_val:,.0f}원)\n"
                         f"   ┗ 손익: `{pnl:+,.0f}원` (`{rate:+.2f}%`)\n")
        
        # 현금 비중 (한 번 더 표시)
        if total_eval > 0:
            cash_weight = (cash / total_eval) * 100
            text += f"\n💵 **현금 비중**: {cash_weight:.1f}%"
                
        config = self.state_manager.portfolio_config
        asset_names = list(config.get("assets", {}).keys())
        
        has_tr = any("TR" in name for name in asset_names)
        has_strip = any("스트립" in name for name in asset_names)
        
        keyboard = []
        if has_tr or has_strip:
            if has_tr:
                text += "💡 [TR vs 배당형] 보유 중인 '200TR'은 배당 세금(15.4%) 없이 자동 재투자하여 차익에 유리합니다. 직접 현금 배당을 받아 스나이핑 투자를 원하시면 'KODEX 200'으로 교체할 수 있습니다.\n\n"
            if has_strip:
                text += "💡 [스트립 vs 이자 지급형] '스트립 채권'은 금리 인하 시 차익이 극대화됩니다. 반면 매월 이자 발생을 원하시면 '국고채30년액티브'로 교체가능합니다.\n\n"
                
            keyboard = [
                [InlineKeyboardButton("🔄 배당/이자 받는 종목으로 교체", callback_data='nudge_change')],
                [InlineKeyboardButton("⏳ 기존 세팅 유지하기", callback_data='nudge_keep')]
            ]
        
        if keyboard:
            await text_func(text=text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await text_func(text=text, parse_mode='Markdown')

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        query = update.callback_query
        data = query.data
        if data in ['rebalance_info', 'execute_now', 'trend_hunter']:
            if self.is_processing:
                await query.answer("⚠️ 다른 복잡한 작업을 처리 중입니다! 잠시만 기다려주세요.", show_alert=True)
                return
            self.is_processing = True

        try:
            await self._inner_handle_callback(update, context)
        except Exception as e:
            import traceback
            from broker_kis import KISAPIError
            logging.error(f"Telegram Callback Error: {e}\n{traceback.format_exc()}")
            error_msg = "⚠️ 서버 통신 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
            if isinstance(e, KISAPIError):
                error_msg = f"⚠️ KIS API 오류가 발생했습니다.\n상세: {e}"
            await query.edit_message_text(text=error_msg)
        finally:
            if data in ['rebalance_info', 'execute_now', 'trend_hunter']:
                self.is_processing = False

    async def _inner_handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data
        
        if data == 'rebalance_info':
            await query.edit_message_text(text="⏳ 데이터를 수집 및 검증하고 있습니다. (최대 2분이 소요될 수 있습니다...)")
            market_data = await self.data_provider.get_latest_data(self)
            balance_data = self.broker.get_balance()
            decision = await self.council.generate_rebalance_decision(self.state_manager, market_data, balance_data)
            
            # [고도화] 이번 분석의 상세 회의록을 세션(chat_data)에 임시 저장
            context.chat_data['last_rebalance_decision'] = decision
            
            text = f"**[최근 회의록 (Memory)]**\n{self.state_manager.get_recent_memory(limit=1)}\n\n"
            text += f"**[AI 위원회 최신 분석 결과]**\nAction: {decision['action']}\n\n{decision['minutes']}\n\n"
            text += "**추천 비중:**\n"
            for asset, weight in decision['weights'].items():
                text += f"- {asset}: {weight}%\n"
            
            await query.edit_message_text(text=text, parse_mode='Markdown')
            self.state_manager.optimize_memory()
            
        elif data == 'view_report':
            await self.send_visual_report(update.callback_query.message.reply_photo, update.callback_query.edit_message_text)

        elif data == 'balance_info':
            await self.show_balance_info(update.callback_query.edit_message_text)
                
        elif data == 'nudge_change':
            config = self.state_manager.portfolio_config
            assets_dict = config.get("assets", {})
            new_assets = {}
            for name, weight in assets_dict.items():
                new_name = name
                if "TR" in new_name: new_name = "KODEX 200"
                if "스트립" in new_name: new_name = "국고채30년액티브"
                new_assets[new_name] = weight
            self.state_manager.update_portfolio_config({"assets": new_assets})
            await query.edit_message_text(text="✅ 반영되었습니다! 다음 매매부터 현금이 들어오는 종목으로 우선 거래하겠습니다.", reply_markup=None)

        elif data == 'nudge_keep':
            await query.edit_message_text(text="✅ 확인했습니다. 기존 세팅을 안전하게 쭉 유지합니다.", reply_markup=None)
            
        elif data == 'trend_hunter':
            await query.edit_message_text(text="🔭 삐빅... 양봉이가 글로벌 ETF 사각지대를 탐색 중입니다. (잠시만 기다려주세요!)")
            current_config = self.state_manager.portfolio_config
            recommendationJson = await self.trend_hunter.generate_monthly_recommendation(current_config)
            result_text = self.trend_hunter.format_for_telegram(recommendationJson)
            await context.bot.send_message(chat_id=query.message.chat_id, text=result_text, parse_mode='Markdown')
            
        elif data == 'add_seed':
            await query.edit_message_text(text="새로 반영할 전체 시드 금액을 숫자로 입력해주세요. (예: 1000000)")
            context.user_data['expecting'] = 'seed'
            
        elif data == 'protect_profit':
            await query.edit_message_text(text="수익금 보전(인출)할 금액을 숫자로 입력해주세요. (예: 500000)")
            context.user_data['expecting'] = 'protect'
            
        elif data == 'toggle_auto':
            is_auto = self.state_manager.toggle_auto()
            await query.edit_message_text(text=f"모드가 변경되었습니다: {'🤖 자동' if is_auto else '👤 수동'}")
            
        elif data == 'ask_panic':
            keyboard = [
                [InlineKeyboardButton("🚨 네, 모든 작동을 긴급 정지합니다", callback_data='confirm_panic')],
                [InlineKeyboardButton("❌ 취소", callback_data='cancel_action')]
            ]
            await query.edit_message_text(text="⚠️ **[긴급 패닉]**\n\n봇의 모든 매매와 알고리즘 추론을 완전히 다운시킵니다. 확정하시겠습니까?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

        elif data == 'confirm_panic':
            self.state_manager.trigger_panic()
            self.broker.is_emergency_stop = True
            await query.edit_message_text(
                text="🚨 **[긴급] 패닉 버튼 가동 완료.**\n\n모든 예약 매매와 시스템 추론이 강제 정지되었습니다. 서버 재구동 전까지 대기합니다.",
                parse_mode="Markdown"
            )

        elif data == 'ask_execute_now':
            keyboard = [
                [InlineKeyboardButton("🚀 네, 수동 강제 리밸런싱을 시작합니다", callback_data='execute_now')],
                [InlineKeyboardButton("❌ 취소", callback_data='cancel_action')]
            ]
            await query.edit_message_text(text="⚙️ **[수동 리밸런싱 확정]**\n\n현재 저장된 포트폴리오 비중으로 즉각 시장에서 매매를 가동하시겠습니까?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

        elif data == 'execute_now':
            await query.edit_message_text(text="🚀 수동 리밸런싱 엔진을 무력 가동합니다...")
            config = self.state_manager.portfolio_config
            target_weights_dict = config.get("assets", {})
            orders, tlh_assets, skipped_dust_assets, price_zero_assets = self.rebalancer.calculate_orders(target_weights_dict)
            
            if not orders and not tlh_assets:
                await query.message.reply_text(
                    "✅ [리밸런싱 불필요]\n\n"
                    "현재 계좌의 보유 자산이 목표 비중(황금 비율)과 이미 완벽하게 일치합니다.\n\n"
                    "💡 가용 현금이 남아있는 이유:\n"
                    "남은 현금으로 1주를 추가 매수할 경우, 목표 비중을 초과하여 황금 비율이 깨지게 됩니다. (예: 1주당 단가가 높은 나스닥/S&P500 등)\n"
                    "따라서 현재 남은 현금은 다음 투자 기회나 예치금 추가 시점까지 안전하게 보관됩니다.\n\n"
                    "(*참고: 만약 과거 매도 지시한 종목이 포트폴리오에 없다면, 이미 전량 매도되어 계좌에서 정리된 상태입니다.)"
                )
                self.is_processing = False
                return

            try:
                await self.rebalancer.execute_rebalancing(orders, tlh_assets)
            except Exception as exec_err:
                err_msg = (f"🚨 [리밸런싱 중단됨]\n\n{str(exec_err)}")
                await context.bot.send_message(chat_id=query.message.chat_id, text=err_msg)
                return
            
            # [신규] 더스트 패스 및 전체 시드 상황 안내 (항상 보고)
            try:
                min_seed_req = self.rebalancer.calculate_minimum_required_seed(target_weights_dict)
                bal = self.broker.get_balance()
                short_amount = max(0, min_seed_req - bal["total_value"])
                
                report_msg = ""
                if price_zero_assets:
                    pz_names = [f"{a['name']}({a['ticker']})" for a in price_zero_assets]
                    report_msg += (f"⚠️ **[가격 조회 실패: 매매 제외]**\n"
                                   f"다음 종목들은 현재가를 가져올 수 없어(거래소 통신 지연 등) 이번 리밸런싱에서 제외되었습니다:\n"
                                   f"👉 `{', '.join(pz_names)}` (종목 상태 재확인 요망)\n\n")

                if skipped_dust_assets:
                    # [개선] 보유 중인 종목과 신규 매수 종목을 구분하여 안내
                    keep_names = [a["name"] for a in skipped_dust_assets if a.get("is_held")]
                    skip_names = [a["name"] for a in skipped_dust_assets if not a.get("is_held")]
                    
                    if keep_names:
                        report_msg += (f"✅ **[적정 비중 유지(Keep) 안내]**\n"
                                       f"다음 종목들은 이미 최적 비중 근처에 도달해 있으며, 1주 미만의 미세한 차이만 존재하여 별도의 매매 없이 **현재 비중을 그대로 유지**합니다:\n"
                                       f"👉 `{', '.join(keep_names)}`\n\n")
                    
                    if skip_names:
                        report_msg += (f"💡 **[1주 단위 매매: 잔차(Dust) 스킵 안내]**\n"
                                       f"배분 금액이 ETF 1주 단가보다 모자라 다음 신규 종목들은 이번 리밸런싱에서 매수 보류 처리하였습니다:\n"
                                       f"👉 `{', '.join(skip_names)}`\n\n")
                
                report_msg += (f"💰 **[포트폴리오 필요 자금 현황]**\n"
                               f"가장 비싼 주가/가장 적은 비중을 기준으로, 모든 종목을 비율대로 최소 1주 이상 담기 위한 계좌 최소 규모는 약 **{min_seed_req:,.0f}원**입니다.\n")
                
                if short_amount > 0:
                    report_msg += f"👉 따라서 원활한 100% 리밸런싱을 원하신다면 약 **{short_amount:,.0f}원**의 시드머니를 주식 계좌에 추가로 투입하셔야 합니다!\n"
                else:
                    report_msg += "✅ **현재 계좌 자산 규모는 모든 리밸런싱을 수행하기에 충분히 여유롭습니다!**\n"
                
                # [신규] 리밸런싱 시점의 전체 수익 현황 추가
                total_pnl = bal.get("total_pnl", 0)
                total_pft_rt = bal.get("total_pft_rt", 0.0)
                report_msg += f"\n📈 **현재 계좌 전체 손익**: {total_pnl:+,.0f}원 ({total_pft_rt:+.2f}%)\n"
                
                # 매매 이후 새 수익률 기록 (seed가 설정되어 있어야 함)
                seed = self.state_manager.state.get("seed_amount", 0)
                if seed > 0:
                    p_rate = ((bal["total_value"] - seed) / seed) * 100
                    self.state_manager.add_profit_history(p_rate)
                    
                await context.bot.send_message(chat_id=query.message.chat_id, text=report_msg, parse_mode='Markdown')
                
                # [신규] 주문 앰뷸런스 작동 여부 안내
                pending_orders = self.state_manager.get_retry_orders()
                if pending_orders:
                    retry_msg = (f"🚑 **[주문 앰뷸런스 대기열 안내]**\n"
                                 f"현재 서버 지연 및 유동성 이유로 {len(pending_orders)}건의 주문이 대기실(Queue)에 입원 중입니다.\n\n"
                                 f"지정가 우회 혹은 시장가 재시도는 **15분 간격으로** 체결될 때까지 봇이 끈질기게 수행할 예정이니 안심하고 기다려주십시오! (자동 구출 로직 가동 중)")
                    await context.bot.send_message(chat_id=query.message.chat_id, text=retry_msg, parse_mode='Markdown')
                else:
                    await context.bot.send_message(chat_id=query.message.chat_id, text="🚀 **모든 계획된 리밸런싱 주문이 시장에 완벽히 전달되었습니다.**")
                    
            except Exception as e:
                err_msg = f"🚨 **[치명적 오류 발생]**\n리밸런싱을 진행하던 중 예기치 않은 시스템 에러가 발생하여 동작이 중단되었습니다:\n`{str(e)}`"
                try: await context.bot.send_message(chat_id=query.message.chat_id, text=err_msg, parse_mode='Markdown')
                except Exception: pass
            
            # [고도화] 분석된 실제 회의록이 있다면 저장, 없으면 수동 실행 기록
            last_decision = context.chat_data.get('last_rebalance_decision')
            if last_decision and last_decision.get('minutes'):
                await self.state_manager.add_meeting_record(last_decision['minutes'])
                context.chat_data['last_rebalance_decision'] = None # 소모 완료
            else:
                await self.state_manager.add_meeting_record("상사님의 수동 버튼 클릭에 의한 즉시 리밸런싱 집행 완료.")
            
            # 리밸런싱 직후 업데이트된 잔고 조회 (PNL 포함)
            balance = self.broker.get_balance()
            history = self.state_manager.state.get("profit_history", [0.0])
            report_path = PortfolioVisualizer.generate_report(balance, history)
            if os.path.exists(report_path):
                with open(report_path, 'rb') as photo:
                    await context.bot.send_photo(chat_id=query.message.chat_id, photo=photo, caption="✅ 리밸런싱 확정 완료! 최신 포트폴리오 배분 리포트입니다.")
            else:
                await context.bot.send_message(chat_id=query.message.chat_id, text="✅ 리밸런싱이 시장에 완벽히 집행되었습니다.")

        elif data.startswith('confirm_input_'):
            parts = data.split('_')
            action_type = parts[2]
            amount = int(parts[3])
            
            if action_type == 'seed':
                self.state_manager.set_seed(amount)
                await query.edit_message_text(text=f"✅ 시드 투입 금액이 **{amount:,}원**으로 안전하게 반영 확정되었습니다.", parse_mode='Markdown')
            elif action_type == 'protect':
                self.state_manager.set_protected(amount)
                await query.edit_message_text(text=f"✅ 수익금 인출 보전액이 **{amount:,}원**으로 안전하게 시스템에 할당되었습니다.", parse_mode='Markdown')
                
        elif data == 'cancel_action':
            await query.edit_message_text(text="❌ 작업이 안전하게 취소되었습니다. 아무것도 변경되거나 실행되지 않았습니다.")

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        expecting = context.user_data.get('expecting')
        user_text = update.message.text
        
        if self.is_processing and not (user_text.lower() in ['/panic', '/stop', '/status']):
            await update.message.reply_text("⚠️ 양봉이가 현재 다른 작업을 열심히 처리 중입니다! 끝나면 다시 말씀해 주세요.")
            return

        # 텍스트 파싱 등 무거운 작업에만 락 적용 (간단 명령어 제외)
        is_heavy_task = False
        if not expecting and not any(user_text.startswith(cmd) for cmd in ['/panic', '/stop', '/지침', '지침', '/status', '/report', '/list', '/del', '/clear']):
            is_heavy_task = True
            self.is_processing = True

        try:
            await self._inner_handle_message(update, context)
        finally:
            if is_heavy_task:
                self.is_processing = False

    async def _inner_handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        expecting = context.user_data.get('expecting')
        user_text = update.message.text
        
        # 텍스트 명령어로 들어온 /panic, /stop 처리
        if user_text.lower() in ['/panic', '/stop']:
            await self.ask_panic(update.message.reply_text)
            return
            
        # 한글 명령어 '지침' 처리 (표준 텔레그램 명령어인 /cmd 외에 편의용)
        if user_text.startswith('/지침 ') or user_text.startswith('지침 '):
            instruction = user_text.replace('/지침 ', '').replace('지침 ', '').strip()
            if instruction:
                await self._process_instruction(update, instruction)
            else:
                await update.message.reply_text("💡 지침 내용을 함께 입력해주세요. (예: /지침 금 비중 유지)")
            return

        if user_text.strip() in ['/지침목록', '지침목록']:
            await self.cmd_list_instructions(update, context)
            return
            
        if user_text.startswith('/지침삭제 ') or user_text.startswith('지침삭제 '):
            code = user_text.replace('/지침삭제 ', '').replace('지침삭제 ', '').strip().upper()
            if code:
                await self._process_delete_instruction(update, code)
            else:
                await update.message.reply_text("💡 삭제할 지침 코드를 입력해주세요. (예: /지침삭제 R1)")
            return
            
        if user_text.strip() in ['/지침초기화', '지침초기화']:
            await self.cmd_clear_instructions(update, context)
            return

        if expecting:
            try:
                amount = int(user_text)
                action_str = "시드 금액" if expecting == "seed" else "보전(인출) 지정 금액"
                keyboard = [
                    [InlineKeyboardButton("✅ 확정 적용합니다", callback_data=f"confirm_input_{expecting}_{amount}")],
                    [InlineKeyboardButton("❌ 취소", callback_data="cancel_action")]
                ]
                await update.message.reply_text(f"입력하신 **{action_str} {amount:,}원**을 저장하고 반영하시겠습니까?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
                context.user_data['expecting'] = None
                return
            except ValueError:
                await update.message.reply_text("❌ 단위나 쉼표 없이 숫자만 연속해서 입력해주세요.")
                return

        # 자연어 파싱 단계
        processing_msg = await update.message.reply_text("🤔 양봉이가 상사님의 말씀을 경청하고 있습니다...")
        
        current_config = self.state_manager.portfolio_config
        parsed = await self.chat_parser.parse_command(user_text, self.state_manager)
        if parsed:
            action = parsed.get("action")
            reply_text = parsed.get("reply_message", "성공적으로 처리되었습니다.")
            
            if action == "CHIT_CHAT":
                # 일반 대화인 경우 시뮬레이션 멘트 없이 답변만 출력
                await processing_msg.edit_text(reply_text)
                self.state_manager.optimize_memory()
            elif action == "RESEARCH":
                # 리서치 요청인 경우 TrendHunter 가동
                await processing_msg.edit_text(reply_text)
                current_config = self.state_manager.portfolio_config
                recommendationJson = await self.trend_hunter.generate_monthly_recommendation(current_config)
                result_text = self.trend_hunter.format_for_telegram(recommendationJson)
                await context.bot.send_message(chat_id=update.message.chat_id, text=result_text, parse_mode='Markdown')
                self.state_manager.optimize_memory()
            elif "new_portfolio_config" in parsed:
                # [고도화] 채팅 파서의 응답도 리밸런싱 기록용으로 임시 보관
                context.chat_data['last_rebalance_decision'] = {
                    "minutes": f"상사님 채팅 지시 대응: {reply_text}",
                    "weights": parsed["new_portfolio_config"].get("assets", {})
                }
                
                # 매매 명령인 경우에만 확인 버튼과 함께 시뮬레이션 결과 출력
                self.state_manager.update_portfolio_config(parsed["new_portfolio_config"])
                keyboard = [
                    [InlineKeyboardButton("🚀 네, 비중 조절을 실제 시장에 즉시 실행합니다", callback_data='execute_now')],
                    [InlineKeyboardButton("❌ 아니오 (비중 세팅만 저장함)", callback_data='cancel_action')]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                # 매매 명령임을 강조하는 멘트 추가 가능
                await processing_msg.edit_text(f"📊 **[리밸런싱 시뮬레이션 결과]**\n\n{reply_text}", reply_markup=reply_markup, parse_mode='Markdown')
            else:
                await processing_msg.edit_text("말씀하신 의도를 정확히 파악하지 못했습니다. 시장 명령어를 조금 더 쉽게 다시 말씀해 주세요.")
        else:
            await processing_msg.edit_text("양봉이가 잠시 생각에 빠졌나 봅니다. 조금 더 명확하게 말씀해 주시겠어요?🦾")

    def run(self):
        logging.info("Starting Telegram Bot...")
        app = ApplicationBuilder().token(self.token).connect_timeout(30.0).read_timeout(30.0).write_timeout(30.0).build()
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CallbackQueryHandler(self.handle_callback))
        app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), self.handle_message))
        
        # 전역 에러 핸들러 추가
        app.add_error_handler(self.error_handler)

        app.run_polling()
