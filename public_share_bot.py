import os
import logging
import secrets
import re
import asyncio
import functools
from typing import Dict, List, Optional
from dotenv import load_dotenv

import psycopg2
from psycopg2 import pool

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TimedOut, BadRequest, NetworkError
from telegram.ext import (
    Application,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    Defaults
)
from telegram.request import HTTPXRequest

# 加载 .env 文件中的环境变量
load_dotenv()

# --- 环境变量读取 ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
PRIVATE_CHANNEL_ID = os.getenv("PRIVATE_CHANNEL_ID")
DATABASE_URL = os.getenv("DATABASE_URL")
REQUIRED_GROUP_ID = os.getenv("REQUIRED_GROUP_ID")
GROUP_INVITE_LINK = os.getenv("GROUP_INVITE_LINK")
PROXY_URL = os.getenv("PROXY_URL")

# --- 日志记录配置 ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- 常量 ---
UPLOAD_BUTTON_TEXT = "📤 上传文件"
FINISH_UPLOAD_BUTTON_TEXT = "✅ 完成上传"
FILES_PER_PAGE = 10

# --- 适配数据库 SSL 连接 ---
if DATABASE_URL and 'sslmode' not in DATABASE_URL and 'localhost' not in DATABASE_URL:
    if '?' in DATABASE_URL:
        DATABASE_URL += '&sslmode=require'
    else:
        DATABASE_URL += '?sslmode=require'

# --- 检查所有必要的环境变量 ---
if not all([BOT_TOKEN, PRIVATE_CHANNEL_ID, DATABASE_URL, REQUIRED_GROUP_ID, GROUP_INVITE_LINK]):
    raise ValueError("错误：请确保所有必需的环境变量都已设置。")

# --- 数据库连接池 ---
try:
    # 增加连接池大小，防止高并发下连接耗尽
    db_pool = psycopg2.pool.SimpleConnectionPool(
        1, 20,
        dsn=DATABASE_URL,
        connect_timeout=10
    )
    logger.info("数据库连接池初始化成功。")
except psycopg2.OperationalError as e:
    logger.error(f"无法连接到数据库: {e}")
    raise e

# ============================================================================
# ★★★ 核心修复：异步数据库操作包装器 ★★★
# 将同步的 psycopg2 操作放入线程池运行，防止阻塞 Telegram 的异步事件循环
# ============================================================================
async def execute_db(query, params=None, fetch_one=False, fetch_all=False, commit=False):
    def _run():
        conn = None
        res = None
        try:
            conn = db_pool.getconn()
            cursor = conn.cursor()
            cursor.execute(query, params)
            
            if fetch_one:
                res = cursor.fetchone()
            elif fetch_all:
                res = cursor.fetchall()
            
            if commit:
                conn.commit()
            
            cursor.close()
            return res
        except Exception as e:
            logger.error(f"DB Error: {e}")
            if conn: conn.rollback()
            raise e
        finally:
            if conn: db_pool.putconn(conn)

    return await asyncio.to_thread(_run)

# --- 数据库初始化函数 ---
def setup_database():
    conn = None
    try:
        conn = db_pool.getconn()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS files (
                share_id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
                uploader_id BIGINT NOT NULL, timestamp TIMESTAMPTZ DEFAULT NOW()
            )
        ''')
        # 检查并添加字段
        cursor.execute("SELECT 1 FROM information_schema.columns WHERE table_name='files' AND column_name='file_caption'")
        if cursor.fetchone() is None:
            cursor.execute("ALTER TABLE files ADD COLUMN file_caption TEXT DEFAULT '未命名文件'")
        
        cursor.execute("SELECT 1 FROM information_schema.columns WHERE table_name='files' AND column_name='file_type'")
        if cursor.fetchone() is None:
            cursor.execute("ALTER TABLE files ADD COLUMN file_type VARCHAR(50) DEFAULT '文件'")
            
        cursor.execute("SELECT 1 FROM pg_class WHERE relname = 'idx_uploader_id'")
        if cursor.fetchone() is None:
            cursor.execute("CREATE INDEX idx_uploader_id ON files(uploader_id)")
            
        conn.commit()
        cursor.close()
        logger.info("数据库表结构检查完毕。")
    except Exception as e:
        logger.error(f"数据库初始化失败: {e}")
        if conn: conn.rollback()
    finally:
        if conn: db_pool.putconn(conn)

# --- 检查用户是否在指定群组 ---
async def is_user_in_group(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=REQUIRED_GROUP_ID, user_id=user_id)
        return member.status not in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]
    except Exception as e:
        logger.error(f"无法检查用户 {user_id} 的成员资格: {e}")
        return False

# --- 群组验证装饰器 ---
def require_group_membership(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not update.effective_user:
            return 
        user_id = update.effective_user.id
        if await is_user_in_group(user_id, context):
            return await func(update, context, *args, **kwargs)
        else:
            BOT_START_LINK = "t.me/jisou?start=a_8438438776" #https://t.me/sogoaibot?start=8438438776
            if update.callback_query:
                try:
                    await update.callback_query.answer("⚠️ 请先加入官方群组。", show_alert=True)
                except BadRequest:
                    pass
            else:
                await update.message.reply_text(
                    f"⚠️ **操作受限**\n\n"
                    f"您需要启动机器人然后加入我们的官方群组才能使用此功能。\n\n"
                    f"🚀 [启动机器人]({BOT_START_LINK})\n" 
                    f"👉 [点击这里加入群组]({GROUP_INVITE_LINK})",
                    parse_mode="Markdown",
                    disable_web_page_preview=True
                )
            return None
    return wrapper

# --- 分页键盘生成 ---
def create_pagination_keyboard(current_page: int, total_pages: int, callback_prefix: str, share_id: Optional[str] = None) -> List[List[InlineKeyboardButton]]:
    keyboard = []
    if total_pages > 1:
        page_buttons = []
        start_page = max(1, current_page - 2)
        end_page = min(total_pages, start_page + 4)
        start_page = max(1, end_page - 4)
        for p in range(start_page, end_page + 1):
            text = f"· {p} ·" if p == current_page else str(p)
            callback_data = "noop" if p == current_page else f"{callback_prefix}:{p}"
            if share_id:
                callback_data += f":{share_id}"
            page_buttons.append(InlineKeyboardButton(text, callback_data=callback_data))
        keyboard.append(page_buttons)
    
    nav_row, ends_row = [], []
    if current_page > 1:
        prev_callback = f"{callback_prefix}:{current_page - 1}"
        first_callback = f"{callback_prefix}:1"
        if share_id:
            prev_callback += f":{share_id}"
            first_callback += f":{share_id}"
        nav_row.insert(0, InlineKeyboardButton("‹ 上一页", callback_data=prev_callback))
        ends_row.insert(0, InlineKeyboardButton("« 首页", callback_data=first_callback))
    if current_page < total_pages:
        next_callback = f"{callback_prefix}:{current_page + 1}"
        last_callback = f"{callback_prefix}:{total_pages}"
        if share_id:
            next_callback += f":{share_id}"
            last_callback += f":{share_id}"
        nav_row.append(InlineKeyboardButton("下一页 ›", callback_data=next_callback))
        ends_row.append(InlineKeyboardButton("末页 »", callback_data=last_callback))
    
    if nav_row: keyboard.append(nav_row)
    if ends_row: keyboard.append(ends_row)
    return keyboard

# --- 分页显示分享文件 ---
async def show_shared_files_page(update: Update, context: ContextTypes.DEFAULT_TYPE, share_id: str, page: int = 1):
    try:
        # ★ 改为异步 DB 查询
        result = await execute_db(
            "SELECT message_id, file_caption FROM files WHERE share_id = %s", 
            (share_id,), 
            fetch_one=True
        )

        if not result:
            await update.effective_message.reply_text("❌ 抱歉，这个分享链接无效或文件已被移除。")
            return

        message_ids_str, file_caption = result
        all_ids = [int(i) for i in message_ids_str.split(',')]
        total_files = len(all_ids)

        if total_files == 0:
            await update.effective_message.reply_text(f"ℹ️ “{file_caption}”中没有文件。")
            return

        total_pages = (total_files + FILES_PER_PAGE - 1) // FILES_PER_PAGE
        page = max(1, min(page, total_pages))
        offset = (page - 1) * FILES_PER_PAGE
        ids_to_send = all_ids[offset : offset + FILES_PER_PAGE]
        
        # 发送文件
        sent_messages = await context.bot.copy_messages(chat_id=update.effective_chat.id, from_chat_id=PRIVATE_CHANNEL_ID, message_ids=ids_to_send)
        context.user_data['last_page_file_ids'] = [msg.message_id for msg in sent_messages]

        # 稍微延迟
        await asyncio.sleep(0.5)

        keyboard = create_pagination_keyboard(page, total_pages, "spage", share_id)
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        AD_TEXT = "看片资源免费无限搜索" 
        AD_LINK = "https://t.me/xbso1?start=a_8438438776" 

        text = (
            f"▶️ **正在查看:** {file_caption}\n"
            f"💎广告:  [{AD_TEXT}]({AD_LINK})\n"
            f"📑 第 {page} 页 / 共 {total_pages} 页 (总计 {total_files} 个文件)"
        )
        
        new_panel = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            reply_markup=reply_markup,
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
        context.user_data['last_control_panel_id'] = new_panel.message_id

    except Exception as e:
        logger.error(f"显示文件页错误 share_id={share_id}: {e}")
        await update.effective_message.reply_text("❌ 处理文件时出错，请稍后再试。")

# --- /start 命令 ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user: return
    user = update.effective_user
    context.user_data.clear()
    
    target_share_id = context.args[0] if context.args else None
    if not target_share_id:
        target_share_id = context.user_data.get('pending_share_id')
    
    if update.effective_chat.type != ChatType.PRIVATE and target_share_id:
        bot_username = context.bot_data.get('bot_username', '')
        private_start_url = f"https://t.me/{bot_username}?start={target_share_id}"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔒 点击私聊获取文件", url=private_start_url)]])
        await update.message.reply_text("请在与我的私聊中获取文件，以保护您的隐私。", reply_markup=keyboard, quote=True)
        return

    if target_share_id:
        if await is_user_in_group(user.id, context):
            verification_message = await update.message.reply_text("✅ 验证通过！正在为您准备文件...")
            context.user_data.pop('pending_share_id', None)
            await show_shared_files_page(update, context, share_id=target_share_id, page=1)
            await verification_message.delete()
        else:
            context.user_data['pending_share_id'] = target_share_id
            bot_username = context.bot_data.get('bot_username', '')
            retry_url = f"https://t.me/{bot_username}?start={target_share_id}"
            EXTERNAL_BOT_LINK = "t.me/jisou?start=a_849529159" #https://t.me/sogoaibot?start=8438438776
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🚀 启动机器人", url=EXTERNAL_BOT_LINK)],      
                [InlineKeyboardButton("👉 点击这里加入群组", url=GROUP_INVITE_LINK)], 
                [InlineKeyboardButton("✅ 我已加入，点此获取文件", url=retry_url)]    
            ])
            reply_text = (
                "⚠️ **访问受限**\n\n"
                "您需要先启动机器人然后成为我们官方群组的成员，才能获取此文件。\n\n"
                "1. 先点击上方按钮启动然后点中间按钮加入群组。\n"
                "2. 加入成功后，点击最下方的“我已加入”按钮获取文件。"
            )
            await update.message.reply_text(reply_text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        context.user_data['state'] = 'default'
        keyboard = ReplyKeyboardMarkup([[KeyboardButton(text=UPLOAD_BUTTON_TEXT)]], resize_keyboard=True, one_time_keyboard=False)
        await update.message.reply_text("欢迎使用文件分享机器人！点击下方按钮上传文件或相册。\n\n使用 /help 查看更多指令。", reply_markup=keyboard)

# --- /help 命令 ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user: return
    help_text = (
        "👋 <b>你好！我是一个文件分享机器人。</b>\n\n"
        "<b>用法一：上传文件 (仅限私聊)</b>\n"
        "1. 点击 <b>'📤 上传文件'</b> 按钮进入上传模式。\n"
        "2. 发送任意数量的文件、视频、图片或相册。\n"
        "3. 全部发送完毕后，点击 <b>'✅ 完成上传'</b> 按钮，即可获得一个包含所有文件的分享链接。\n\n"
        "<b>用法二：获取文件</b>\n"
        "▪️ 点击朋友分享给你的链接，文件将会分页显示。\n"
        "▪️ 如果机器人提示，请先按要求加入群组。\n\n"
        "<b>文件管理 (仅限私聊)</b>\n"
        "▪️ 使用 /myfiles 命令来查看和管理您上传过的文件。\n\n"
        "⚠️ <b>使用条件:</b>\n"
        "为防止滥用，您必须先加入我们的官方群组才能使用机器人。"
    )
    await update.message.reply_text(help_text, parse_mode="HTML")

# --- /myfiles 分页 ---
async def show_my_files_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 1):
    user_id = update.effective_user.id
    try:
        # ★ 改为异步 DB 查询
        count_res = await execute_db("SELECT COUNT(*) FROM files WHERE uploader_id = %s", (user_id,), fetch_one=True)
        total_files = count_res[0]

        if total_files == 0:
            text = "您还没有上传过任何文件。使用 '上传文件' 按钮来分享您的第一个文件吧！"
            if update.callback_query: await update.callback_query.edit_message_text(text, reply_markup=None)
            else: await update.message.reply_text(text)
            return
        
        total_pages = (total_files + FILES_PER_PAGE - 1) // FILES_PER_PAGE
        page = max(1, min(page, total_pages))
        offset = (page - 1) * FILES_PER_PAGE
        
        # ★ 改为异步 DB 查询
        files_on_page = await execute_db(
            "SELECT share_id, file_caption FROM files WHERE uploader_id = %s ORDER BY timestamp DESC LIMIT %s OFFSET %s",
            (user_id, FILES_PER_PAGE, offset),
            fetch_all=True
        )

        file_keyboard = []
        for sid, cap in files_on_page:
             # 防止 caption 太长破坏布局
            short_cap = cap[:20] + "..." if len(cap) > 20 else cap
            file_keyboard.append([
                InlineKeyboardButton(f"📄 {short_cap}", callback_data=f"info:{sid}"), 
                InlineKeyboardButton("🗑️ 删除", callback_data=f"delete:{sid}:{page}")
            ])
            
        pagination_keyboard = create_pagination_keyboard(page, total_pages, "page")
        full_keyboard = file_keyboard + pagination_keyboard
        reply_markup = InlineKeyboardMarkup(full_keyboard)
        text = f"这是您上传的文件列表 (第 {page} 页 / 共 {total_pages} 页):"

        if update.callback_query:
            # 忽略未修改错误
            try: await update.callback_query.edit_message_text(text=text, reply_markup=reply_markup)
            except BadRequest as e:
                if "Message is not modified" not in str(e): raise e
        else:
            await update.message.reply_text(text=text, reply_markup=reply_markup)

    except Exception as e:
        logger.error(f"显示我的文件列表失败: {e}")

@require_group_membership
async def my_files_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("请在与我的私聊中使用此命令来管理您的文件。", quote=True)
        return
    await show_my_files_page(update, context, page=1)

# --- 按钮回调处理器 ---
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    
    # ★ 修复：捕获 Query is too old 错误
    try:
        await query.answer()
    except BadRequest as e:
        if "Query is too old" in str(e):
            logger.warning("回调查询已过期，忽略。")
        else:
            logger.error(f"Callback answer error: {e}")
    except Exception as e:
        logger.error(f"Callback unexpected error: {e}")

    parts = query.data.split(":", 2)
    action = parts[0]
    
    # 清理旧消息
    if action == "spage":
        last_page_ids = context.user_data.pop('last_page_file_ids', [])
        if last_page_ids:
            try: await context.bot.delete_messages(chat_id=query.message.chat_id, message_ids=last_page_ids)
            except: pass
        
        last_panel_id = context.user_data.pop('last_control_panel_id', None)
        if last_panel_id:
            try: await context.bot.delete_message(chat_id=query.message.chat_id, message_id=last_panel_id)
            except: pass

    if action == "page":
        await show_my_files_page(update, context, page=int(parts[1]))
    elif action == "spage":
        if len(parts) >= 3:
            await show_shared_files_page(update, context, share_id=parts[2], page=int(parts[1]))
    elif action == "delete":
        if len(parts) < 3: return
        share_id, current_page = parts[1], int(parts[2])
        user_id = query.from_user.id
        
        try:
            # ★ 改为异步 DB 查询
            result = await execute_db("SELECT uploader_id, message_id FROM files WHERE share_id = %s", (share_id,), fetch_one=True)

            if not result:
                try: await query.message.reply_text("🤔 文件好像已经被删除了。")
                except: pass
                await show_my_files_page(update, context, page=current_page) # 刷新
                return

            uploader_id, message_ids_str = result
            if uploader_id != user_id:
                try: await query.message.reply_text("🚫 您没有权限删除此文件。")
                except: pass
                return

            # ★ 改为异步 DB 删除
            await execute_db("DELETE FROM files WHERE share_id = %s", (share_id,), commit=True)
            
            # 删除后刷新页面
            await show_my_files_page(update, context, page=current_page)

            # 尝试删除频道消息
            try:
                message_ids = [int(i) for i in message_ids_str.split(',')]
                await context.bot.delete_messages(chat_id=PRIVATE_CHANNEL_ID, message_ids=message_ids)
            except Exception as e:
                logger.warning(f"从频道删除消息失败 (可能太旧): {e}")

        except Exception as e:
            logger.error(f"删除操作失败: {e}")
            
    elif action == "info":
        share_id = parts[1]
        bot_username = context.bot_data.get('bot_username', '')
        link = f"https://t.me/{bot_username}?start={share_id}"
        await query.message.reply_text(f"这是您选择的文件的分享链接：\n`{link}`", parse_mode="Markdown")
    elif action == "noop":
        return

def escape_markdown_v2(text: str) -> str:
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

@require_group_membership
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE: return
    context.user_data.clear()
    context.user_data['state'] = 'awaiting_file'
    context.user_data['session_message_ids'] = []
    context.user_data['session_file_count'] = 0
    keyboard = ReplyKeyboardMarkup([[KeyboardButton(text=FINISH_UPLOAD_BUTTON_TEXT)]], resize_keyboard=True, one_time_keyboard=False)
    await update.message.reply_text("好的，请发送文件或相册。\n发送完毕后，点击“完成上传”生成链接。", reply_markup=keyboard)

async def finish_upload_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user: return
    if update.effective_chat.type != ChatType.PRIVATE: return
    
    # 等待后台处理
    wait_count = 0
    if context.user_data.get('is_processing'):
        wait_msg = await update.message.reply_text("⏳ 正在处理接收到的文件，请稍候...")
        while context.user_data.get('is_processing') and wait_count < 20:
            await asyncio.sleep(0.5)
            wait_count += 1
        try: await wait_msg.delete()
        except: pass
    
    processing_message = await update.message.reply_text("🔄 正在生成分享链接...")
    user = update.effective_user
    user_id = user.id
    
    session_message_ids = context.user_data.pop('session_message_ids', [])
    total_files = context.user_data.pop('session_file_count', 0)
    context.user_data['is_processing'] = False

    if session_message_ids:
        try:
            share_id = secrets.token_urlsafe(8)
            bot_username = context.bot_data.get('bot_username')
            final_link = f"https://t.me/{bot_username}?start={share_id}"
            ids_str = ",".join(map(str, session_message_ids))
            caption = f"批量上传 (共 {total_files} 个文件)"
            file_type = "合集"
            
            # ★ 改为异步 DB 插入
            await execute_db(
                "INSERT INTO files (share_id, message_id, uploader_id, file_caption, file_type) VALUES (%s, %s, %s, %s, %s)",
                (share_id, ids_str, user_id, caption, file_type),
                commit=True
            )
            
            user_message = (f"🎉 **上传完成！**\n\n文件数: {total_files}\n🔗 **分享链接：**\n`{final_link}`")
            await processing_message.edit_text(text=user_message, parse_mode="Markdown", disable_web_page_preview=True)
            
            # 日志
            escaped_link = escape_markdown_v2(final_link)
            escaped_name = escape_markdown_v2(user.full_name)
            log_msg = f"*New Upload*\nUser: {escaped_name}\nFiles: {total_files}\nLink: {escaped_link}"
            await context.bot.send_message(chat_id=PRIVATE_CHANNEL_ID, text=log_msg, parse_mode="MarkdownV2", disable_web_page_preview=True)
            
        except Exception as e:
            logger.error(f"生成链接失败: {e}")
            await processing_message.edit_text(text="❌ 生成链接时发生错误，请稍后再试。")
    else:
        await processing_message.edit_text(text="⚠️ 本次没有上传文件。")
        
    context.user_data.clear()
    context.user_data['state'] = 'default'
    keyboard = ReplyKeyboardMarkup([[KeyboardButton(text=UPLOAD_BUTTON_TEXT)]], resize_keyboard=True, one_time_keyboard=False)
    await update.message.reply_text("会话结束。再次点击按钮开始新上传。", reply_markup=keyboard)

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user: return
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("请在私聊中使用。", quote=True)
        return
    context.user_data.clear()
    await finish_upload_handler(update, context)

async def process_and_collect_files_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    user_id, chat_id, message_ids = job.data
    try:
        forwarded = await context.bot.forward_messages(PRIVATE_CHANNEL_ID, chat_id, message_ids)
        forwarded_ids = [msg.message_id for msg in forwarded]
        
        if user_id not in context.application.user_data:
            context.application.user_data[user_id] = {}
        
        user_data = context.application.user_data[user_id]
        if 'session_message_ids' not in user_data: user_data['session_message_ids'] = []
        if 'session_file_count' not in user_data: user_data['session_file_count'] = 0
            
        user_data['session_message_ids'].extend(forwarded_ids)
        user_data['session_file_count'] += len(forwarded_ids)
        user_data['is_processing'] = False 
        
    except Exception as e:
        logger.error(f"处理文件Job失败: {e}")
        if user_id in context.application.user_data:
            context.application.user_data[user_id]['is_processing'] = False
    finally:
        media_group_id = job.name
        if media_group_id and media_group_id in context.bot_data: 
            del context.bot_data[media_group_id]

@require_group_membership
async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE: return
    if context.user_data.get('state') != 'awaiting_file':
        await update.message.reply_text("请先点击 '📤 上传文件' 按钮。")
        return
    
    user = update.effective_user
    context.user_data['is_processing'] = True 
    
    media_group_id = update.message.media_group_id
    if media_group_id:
        job_name = str(media_group_id)
        group_context = context.bot_data.setdefault(job_name, {})
        is_first_in_group = not group_context.get('message_ids')
        group_context.setdefault('message_ids', []).append(update.message.message_id)
        if is_first_in_group:
            try: await update.message.reply_text("收到相册，正在处理... 全部发完请点完成。", quote=True)
            except: pass
        
        for job in context.job_queue.get_jobs_by_name(job_name): 
            job.schedule_removal()
        
        context.job_queue.run_once(
            process_and_collect_files_job, 
            1.5, 
            data=[user.id, update.effective_chat.id, group_context['message_ids']], 
            name=job_name
        )
    else: 
        try: await update.message.reply_text("收到文件...", quote=True)
        except: pass
        context.job_queue.run_once(
            process_and_collect_files_job, 
            0, 
            data=[user.id, update.effective_chat.id, [update.message.id]]
        )

async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type == ChatType.CHANNEL: return
    if not update.effective_user: return
    if update.effective_chat.type != ChatType.PRIVATE: return
    bot_username = context.bot_data.get('bot_username', '')
    user_text = update.message.text
    pattern = re.compile(rf"https?://t\.me/{bot_username}\?start=([A-Za-z0-9_-]+)")
    match = pattern.match(user_text)
    if match:
        context.args = [match.group(1)]
        await start(update, context)
    elif context.user_data.get('state') == 'awaiting_file':
        await update.message.reply_text("请发送文件，或者点击“完成上传”。")
    else:
        await update.message.reply_text("请点击 '📤 上传文件' 按钮开始。")

# --- ★★★ 必须添加：初始化后回调 ★★★ ---
async def post_init(application: Application) -> None:
    bot_info = await application.bot.get_me()
    application.bot_data['bot_username'] = bot_info.username
    logger.info(f"机器人 {bot_info.username} 已成功初始化。")

def main() -> None:
    # 1. 数据库建表
    setup_database()

    # 2. ★ 优化网络请求 (去除报错参数，强制 HTTP 1.1)
    trequest = HTTPXRequest(
        connection_pool_size=20,
        read_timeout=60.0,
        write_timeout=60.0,
        connect_timeout=30.0,
        pool_timeout=30.0,
        http_version="1.1", 
    )

    # 3. 构建应用
    builder = Application.builder().token(BOT_TOKEN).post_init(post_init).request(trequest)
    
    if PROXY_URL:
        builder.proxy_url(PROXY_URL)
        logger.info(f"正在使用代理: {PROXY_URL}")
        
    application = builder.build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("myfiles", my_files_command))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(f'^{UPLOAD_BUTTON_TEXT}$'), button_handler))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(f'^{FINISH_UPLOAD_BUTTON_TEXT}$'), finish_upload_handler))
    application.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.Document.ALL & ~filters.ChatType.CHANNEL, file_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))
    application.add_handler(CallbackQueryHandler(button_callback_handler))
    
    logger.info(">>> 机器人正在启动... <<<")
    
    # ★ 启动时丢弃积压更新，防止死锁
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    main()
