import logging
import re
import os
import psycopg2
from datetime import datetime, timedelta
from telegram import Update, ChatPermissions, ChatMember
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    filters,
)

# --- КОНФИГУРАЦИЯ ---
# На Scalingo добавь переменную BOT_TOKEN в разделе Environment
TOKEN = os.environ.get('BOT_TOKEN', '8526680835:AAH3QFXOXhEk0l-RaQ1w-xtjwI5tXSGctCE')
DATABASE_URL = os.environ.get('DATABASE_URL')
logging.basicConfig(level=logging.INFO)

# --- ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ (PostgreSQL) ---
def db_query(query, params=(), fetchone=False):
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    cursor = conn.cursor()
    cursor.execute(query, params)
    res = cursor.fetchone() if fetchone else None
    conn.commit()
    conn.close()
    return res

def init_db():
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    cursor = conn.cursor()
    # Таблица настроек чатов
    cursor.execute('''CREATE TABLE IF NOT EXISTS settings 
        (chat_id BIGINT PRIMARY KEY, warn_limit INTEGER DEFAULT 3, 
         welcome_on INTEGER DEFAULT 0, welcome_msg TEXT DEFAULT 'Привет, {user}!', welcome_photo TEXT,
         goodbye_on INTEGER DEFAULT 0, goodbye_msg TEXT DEFAULT 'Пока, {user}!', goodbye_photo TEXT,
         antispam INTEGER DEFAULT 1)''')
    # Таблица варнов
    cursor.execute('''CREATE TABLE IF NOT EXISTS warns 
        (chat_id BIGINT, user_id BIGINT, count INTEGER DEFAULT 0, 
         PRIMARY KEY(chat_id, user_id))''')
    conn.commit()
    conn.close()

if DATABASE_URL:
    init_db()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
async def is_admin(update: Update):
    if update.effective_chat.type == 'private': return True
    m = await update.effective_chat.get_member(update.effective_user.id)
    return m.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]

def parse_time(args):
    for arg in args:
        match = re.search(r'(\d+)([smhd])', arg.lower())
        if match:
            val, unit = int(match.group(1)), match.group(2)
            return val * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[unit]
    return 3600

async def get_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.reply_to_message:
        user = update.message.reply_to_message.from_user
        return user.id, user.first_name
    if context.args:
        for arg in context.args:
            if arg.startswith('@'): return arg, arg
    return None, None

# --- КОМАНДЫ МОДЕРАЦИИ ---

async def restrict_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update): return
    uid, name = await get_target(update, context)
    cmd = update.message.text.split()[0][1:].lower()
    if not uid:
        return await update.message.reply_text(f"Чтобы использовать /{cmd}, ответьте на сообщение или тегните юзера.")

    sec = parse_time(context.args)
    until = datetime.now() + timedelta(seconds=sec)
    try:
        if cmd == "mute":
            await update.effective_chat.restrict_member(uid, ChatPermissions(can_send_messages=False), until_date=until)
            await update.message.reply_text(f"🔇 Пользователь {name} замучен. Какой негодяй...")
        elif cmd == "unmute":
            await update.effective_chat.restrict_member(uid, ChatPermissions(can_send_messages=True))
            await update.message.reply_text(f"🔊 Мут с {name} снят.")
        elif cmd in ["ban", "pban"]:
            await update.effective_chat.ban_member(uid, until_date=None if cmd == "pban" else until)
            await update.message.reply_text(f"🔨 Пользователь {name} забанен. Вот что будет, если нарушать закон.")
        elif cmd == "unban":
            await update.effective_chat.unban_member(uid)
            await update.message.reply_text(f"✅ Пользователь {name} разбанен.")
    except Exception: await update.message.reply_text("❌ Ошибка прав или юзер не найден.")

async def warn_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update): return
    uid, name = await get_target(update, context)
    if not uid: return await update.message.reply_text("❌ Укажите пользователя для варна.")
    chat_id = update.effective_chat.id
    cmd = update.message.text.split()[0][1:].lower()

    row = db_query("SELECT warn_limit FROM settings WHERE chat_id=%s", (chat_id,), True)
    limit = row[0] if row else 3

    if cmd == "warn":
        if not isinstance(uid, int): return await update.message.reply_text("❌ Нужен реплай.")
        res = db_query("SELECT count FROM warns WHERE chat_id=%s AND user_id=%s", (chat_id, uid), True)
        count = (res[0] if res else 0) + 1
        if count >= limit:
            await update.effective_chat.ban_member(uid)
            db_query("DELETE FROM warns WHERE chat_id=%s AND user_id=%s", (chat_id, uid))
            await update.message.reply_text(f"⛔ {name} забанен (лимит варнов {count}/{limit}). Как можно нарушать закон так часто?")
        else:
            db_query("INSERT INTO warns (chat_id, user_id, count) VALUES (%s, %s, %s) ON CONFLICT (chat_id, user_id) DO UPDATE SET count = EXCLUDED.count", (chat_id, uid, count))
            sec = parse_time(context.args)
            await update.effective_chat.restrict_member(uid, ChatPermissions(can_send_messages=False), until_date=datetime.now()+timedelta(seconds=sec))
            await update.message.reply_text(f"⚠️ {name}: варн {count}/{limit} + мут.")
    elif cmd == "unwarn":
        db_query("UPDATE warns SET count = MAX(0, count - 1) WHERE chat_id=%s AND user_id=%s", (chat_id, uid))
        await update.message.reply_text(f"✅ У {name} снят 1 варн.")
    elif cmd == "resetwarn":
        db_query("DELETE FROM warns WHERE chat_id=%s AND user_id=%s", (chat_id, uid))
        await update.message.reply_text(f"♻️ Варны пользователя {name} сброшены.")

async def set_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update) or not context.args: return
    db_query("INSERT INTO settings (chat_id, warn_limit) VALUES (%s, %s) ON CONFLICT (chat_id) DO UPDATE SET warn_limit = EXCLUDED.warn_limit", (update.effective_chat.id, int(context.args[0])))
    await update.message.reply_text(f"📏 Лимит варнов: {context.args[0]}")

# --- ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ---
spam_temp = {}

async def handle_everything(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg: return
    chat_id = update.effective_chat.id
    text = msg.text or msg.caption

    db_query("INSERT INTO settings (chat_id) VALUES (%s) ON CONFLICT DO NOTHING", (chat_id,))

    # 1. РЕПОРТ
    if text and text.startswith('/report'):
        if not msg.reply_to_message:
            await msg.reply_text("❌ Ошибка! Чтобы отправить жалобу, используйте команду /report в ответ на сообщение нарушителя.")
            return
        
        admins = await update.effective_chat.get_administrators()
        tags = ", ".join([f"[{a.user.first_name}](tg://user?id={a.user.id})" for a in admins if not a.user.is_bot])
        await msg.reply_text(f"📢 Циньи бдит...\nВас будут карать: {tags}", parse_mode='Markdown')
        return

    # 2. АНТИСПАМ
    if not await is_admin(update):
        uid = update.effective_user.id
        now = datetime.now()
        history = spam_temp.get(uid, [])
        history = [(t, m_id) for t, m_id in history if now - t < timedelta(seconds=10)]
        history.append((now, msg.message_id))
        spam_temp[uid] = history
        if len(history) >= 5:
            for _, m_id in history:
                try: await context.bot.delete_message(chat_id, m_id)
                except: pass
            await update.effective_chat.restrict_member(uid, ChatPermissions(can_send_messages=False), until_date=now + timedelta(minutes=5))
            await context.bot.send_message(chat_id, f"🚫 {update.effective_user.first_name} замучен на 5 мин за спам.")
            return

    # 3. НАСТРОЙКИ ПРИВЕТСТВИЙ
    if not await is_admin(update) or not text: return
    t = text.lower()
    if text.startswith('/setwelcome'):
        db_query("UPDATE settings SET welcome_msg=%s, welcome_photo=%s WHERE chat_id=%s", (text.replace('/setwelcome', '').strip(), msg.photo[-1].file_id if msg.photo else None, chat_id))
        await msg.reply_text("✅ Приветствие сохранено.")
    elif 'welcome on' in t: db_query("UPDATE settings SET welcome_on=1 WHERE chat_id=%s", (chat_id,)); await msg.reply_text("✅ Приветствие ВКЛ")
    elif 'welcome off' in t: db_query("UPDATE settings SET welcome_on=0 WHERE chat_id=%s", (chat_id,)); await msg.reply_text("❌ Приветствие ВЫКЛ")

async def join_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    res = update.chat_member
    chat_id = update.effective_chat.id
    row = db_query("SELECT welcome_on, welcome_msg, welcome_photo FROM settings WHERE chat_id=%s", (chat_id,), True)
    if not row or not row[0]: return
    txt = row[1].replace('{user}', res.new_chat_member.user.first_name)
    if row[2]: await context.bot.send_photo(chat_id, row[2], caption=txt)
    else: await context.bot.send_message(chat_id, txt)

if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler(["mute", "unmute", "ban", "pban", "unban"], restrict_handler))
    app.add_handler(CommandHandler(["warn", "unwarn", "resetwarn"], warn_handler))
    app.add_handler(CommandHandler("setwarnlimit", set_limit))
    app.add_handler(MessageHandler(filters.ALL, handle_everything))
    app.add_handler(ChatMemberHandler(join_event, ChatMemberHandler.CHAT_MEMBER))
    app.run_polling(allowed_updates=Update.ALL_TYPES)
