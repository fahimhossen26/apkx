import logging
import sqlite3
import httpx
import phonenumbers
from datetime import datetime, timedelta
from phonenumbers import geocoder
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, 
    ContextTypes, 
    CommandHandler, 
    MessageHandler, 
    CallbackQueryHandler, 
    ConversationHandler,
    filters
)

# ------------------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------------------
BOT_TOKEN = "8056264478:AAG5DGsG-1Tjp_e3pUClNHgWC8kxVJxyOOs"
ADMIN_ID = 5372825497
# REPLACE WITH YOUR ADMIN GROUP ID
WITHDRAWAL_GROUP_ID = -1001234567890 
OTP_GROUP_LINK = "https://t.me/your_otp_channel_link"

# REWARDS
REWARD_PER_SMS = 0.005
REWARD_REFERRAL = 0.001
REWARD_COMMISSION = 0.0005

# DATABASE FILE (SQLite - Low RAM Usage)
DB_NAME = "inventory.db"

# API CONFIGURATION
API_SOURCES = [
    {
        "url": "http://51.77.216.195/crapi/dgroup/viewstats",
        "key": "Q1ZUSDRSQkeAf4hXQ1CVg19kk0lIl2hYeZZWhHdllXZGd4ZzW2OH"
    },
    {
        "url": "http://147.135.212.197/crapi/had/viewstats",
        "key": "RVBYRzRSQlt6g2eJfoiGdUFPhGdzg22FXE"
    }
]

# ------------------------------------------------------------------------
# STATES FOR CONVERSATIONS
# ------------------------------------------------------------------------
ASK_TAG = 0
DELETE_SELECT, DELETE_AMOUNT = range(1, 3)
BROADCAST_MSG = 3
WITHDRAW_AMOUNT, WITHDRAW_METHOD = range(4, 6)

# ------------------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------------
# DATABASE FUNCTIONS (SQLite Optimized)
# ------------------------------------------------------------------------

def get_db_connection():
    """Creates a database connection."""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initialize the SQL database tables."""
    conn = get_db_connection()
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS numbers (
        phone TEXT PRIMARY KEY, 
        country TEXT, 
        tags TEXT, 
        status TEXT, 
        user_id INTEGER, 
        last_msg TEXT, 
        message_id INTEGER,
        assigned_at TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS profiles (
        user_id INTEGER PRIMARY KEY,
        balance REAL DEFAULT 0.0,
        referrer_id INTEGER,
        referrals_count INTEGER DEFAULT 0,
        total_earned REAL DEFAULT 0.0
    )''')

    # Migrations
    columns_to_check = {
        'numbers': [('last_msg', 'TEXT'), ('message_id', 'INTEGER'), ('assigned_at', 'TEXT')],
        'profiles': [('total_earned', 'REAL DEFAULT 0.0')]
    }
    
    for table, cols in columns_to_check.items():
        try:
            c.execute(f"PRAGMA table_info({table})")
            existing_cols = [row['name'] for row in c.fetchall()]
            for col_name, col_type in cols:
                if col_name not in existing_cols:
                    try:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
                    except: pass
        except: pass

    conn.commit()
    conn.close()

def get_profile(user_id):
    """Get or create user profile."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM profiles WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    
    if row is None:
        c.execute("INSERT INTO profiles (user_id, balance, referrals_count, total_earned) VALUES (?, 0.0, 0, 0.0)", (user_id,))
        conn.commit()
        c.execute("SELECT * FROM profiles WHERE user_id = ?", (user_id,))
        row = c.fetchone()
    
    conn.close()
    return dict(row)

def add_balance(user_id, amount):
    """Adds balance safely."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO profiles (user_id) VALUES (?)", (user_id,))
    c.execute("UPDATE profiles SET balance = balance + ?, total_earned = total_earned + ? WHERE user_id = ?", 
              (amount, amount if amount > 0 else 0, user_id))
    conn.commit()
    conn.close()

def process_referral(new_user_id, referrer_id):
    if new_user_id == referrer_id: return False
    
    conn = get_db_connection()
    c = conn.cursor()
    
    c.execute("SELECT 1 FROM profiles WHERE user_id = ?", (new_user_id,))
    if c.fetchone():
        conn.close()
        return False
        
    c.execute("SELECT 1 FROM profiles WHERE user_id = ?", (referrer_id,))
    if not c.fetchone():
        c.execute("INSERT INTO profiles (user_id) VALUES (?)", (referrer_id,))
    
    c.execute("INSERT INTO profiles (user_id, referrer_id) VALUES (?, ?)", (new_user_id, referrer_id))
    c.execute("UPDATE profiles SET balance = balance + ?, total_earned = total_earned + ?, referrals_count = referrals_count + 1 WHERE user_id = ?", 
              (REWARD_REFERRAL, REWARD_REFERRAL, referrer_id))
    
    conn.commit()
    conn.close()
    return True

def get_referrer_id(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT referrer_id FROM profiles WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result['referrer_id'] if result else None

def get_all_users():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id FROM profiles")
    rows = c.fetchall()
    conn.close()
    return [r['user_id'] for r in rows]

def add_numbers_to_db(number_list, tag):
    conn = get_db_connection()
    c = conn.cursor()
    count = 0
    countries_found = set()
    
    for phone_raw in number_list:
        phone = phone_raw if phone_raw.startswith('+') else f"+{phone_raw}"
        country_label = detect_country_label(phone)
        countries_found.add(country_label)
        
        try:
            c.execute("""INSERT INTO numbers 
                         (phone, country, tags, status, user_id, last_msg, message_id, assigned_at) 
                         VALUES (?, ?, ?, 'available', NULL, NULL, NULL, NULL)""",
                      (phone, country_label, tag))
            count += 1
        except sqlite3.IntegrityError:
            pass

    conn.commit()
    conn.close()
    return count, countries_found

def get_inventory_stats():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT country, tags, COUNT(*) as cnt FROM numbers WHERE status='available' GROUP BY country, tags")
    rows = c.fetchall()
    conn.close()
    stats = {}
    for r in rows:
        stats[(r['country'], r['tags'])] = r['cnt']
    return stats

def get_stock_counts(country, tag):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM numbers WHERE country=? AND tags=? AND status='available'", (country, tag))
    left = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM numbers WHERE country=? AND tags=? AND (status='assigned' OR status='used')", (country, tag))
    used = c.fetchone()[0]
    conn.close()
    return left, used

def assign_number(user_id, country, tag):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT phone FROM numbers WHERE country=? AND tags=? AND status='available' ORDER BY RANDOM() LIMIT 1", (country, tag))
    row = c.fetchone()
    
    if row:
        phone = row['phone']
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute("""UPDATE numbers SET status='assigned', user_id=?, last_msg=NULL, message_id=NULL, assigned_at=? 
                     WHERE phone=?""", (user_id, now_str, phone))
        conn.commit()
        conn.close()
        return {'number': phone, 'country': country, 'tags': tag, 'last_msg': None}
    
    conn.close()
    return None

def get_user_active_number(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM numbers WHERE user_id=? AND status='assigned'", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return dict(row)
    return None

def update_number_msg(phone, msg):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE numbers SET last_msg=? WHERE phone=?", (msg, phone))
    conn.commit()
    conn.close()

def update_number_message_id(phone, message_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE numbers SET message_id=? WHERE phone=?", (message_id, phone))
    conn.commit()
    conn.close()

def replace_user_number(user_id):
    current = get_user_active_number(user_id)
    if not current: return None

    country = current['country']
    tag = current['tags']
    old_phone = current['phone']

    conn = get_db_connection()
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) FROM numbers WHERE country=? AND tags=? AND status='available'", (country, tag))
    stock = c.fetchone()[0]
    
    if stock == 0:
        conn.close()
        return "NO_STOCK"
        
    c.execute("UPDATE numbers SET status='used', user_id=NULL WHERE phone=?", (old_phone,))
    conn.commit()
    conn.close()
    
    return assign_number(user_id, country, tag)

def delete_stock_db(country, tag, amount_str):
    conn = get_db_connection()
    c = conn.cursor()
    
    if amount_str == 'ALL':
        c.execute("DELETE FROM numbers WHERE country=? AND tags=? AND status='available'", (country, tag))
        deleted = c.rowcount
    else:
        limit = int(amount_str)
        c.execute("SELECT phone FROM numbers WHERE country=? AND tags=? AND status='available' LIMIT ?", (country, tag, limit))
        rows = c.fetchall()
        deleted = len(rows)
        if rows:
            phones = [r['phone'] for r in rows]
            placeholders = ','.join('?' * len(phones))
            c.execute(f"DELETE FROM numbers WHERE phone IN ({placeholders})", phones)
            
    conn.commit()
    conn.close()
    return deleted

# ------------------------------------------------------------------------
# API HELPER FUNCTIONS
# ------------------------------------------------------------------------

async def fetch_otp_from_api(phone_number, start_time_str=None):
    clean_number = phone_number.replace('+', '')
    now = datetime.now()
    
    if start_time_str:
        dt1 = start_time_str
    else:
        dt1 = (now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    
    dt2 = now.strftime("%Y-%m-%d %H:%M:%S")
    
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    
    # Increased Timeout to 30.0 seconds to prevent "Time out error"
    timeout_config = httpx.Timeout(30.0, connect=15.0)

    async with httpx.AsyncClient(timeout=timeout_config) as client:
        for source in API_SOURCES:
            params = {
                'token': source['key'],
                'filternum': clean_number,
                'records': 5,
                'dt1': dt1,
                'dt2': dt2
            }
            try:
                response = await client.get(source['url'], params=params, headers=headers)
                if response.status_code == 200:
                    try:
                        data = response.json()
                        if data.get('status') == 'success' and data.get('total', 0) > 0:
                            latest_sms = data['data'][0] 
                            return latest_sms.get('message')
                    except ValueError: pass
            except httpx.TimeoutException:
                logger.warning(f"API Timeout for {source['url']}")
            except Exception as e:
                logger.error(f"API Error ({source['url']}): {e}")
    return None

async def check_active_otps_job(context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM numbers WHERE status='assigned'")
    active_assignments = c.fetchall()
    conn.close()

    for row in active_assignments:
        phone = row['phone']
        user_id = row['user_id']
        message_id = row['message_id']
        country = row['country']
        tags = row['tags']
        last_msg = row['last_msg']
        assigned_at = row['assigned_at']
        
        if not message_id:
            continue
            
        new_msg_text = await fetch_otp_from_api(phone, start_time_str=assigned_at)
        
        if new_msg_text and new_msg_text != last_msg:
            update_number_msg(phone, new_msg_text)
            
            add_balance(user_id, REWARD_PER_SMS)
            referrer_id = get_referrer_id(user_id)
            if referrer_id:
                add_balance(referrer_id, REWARD_COMMISSION)
                try:
                    await context.bot.send_message(chat_id=referrer_id, text=f"💰 **Commission Earned!**\nRef SMS. Added: ${REWARD_COMMISSION}")
                except: pass

            try:
                await context.bot.send_message(chat_id=user_id, text=f"💰 **Reward:** +${REWARD_PER_SMS}")
            except: pass

            parts = country.split()
            flag = parts[-1] if len(parts) >= 2 and parts[0].startswith('+') else ""
            name = " ".join(parts[1:-1]) if len(parts) >= 2 else country

            new_text = (
                f"{flag} {name} {tags} Number Assigned:\n"
                f"`{phone}`\n\n"
                f"📩 **Message:**\n`{new_msg_text}`"
            )
            
            keyboard = [
                [InlineKeyboardButton("🔄 Change Number", callback_data='change_number')],
                [InlineKeyboardButton("🌍 Change Country", callback_data='main_menu')],
                [InlineKeyboardButton("👥 OTP Group", url=OTP_GROUP_LINK)]
            ]
            
            try:
                await context.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=message_id,
                    text=new_text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='Markdown'
                )
            except Exception as e:
                pass

# ------------------------------------------------------------------------
# HELPER FUNCTIONS
# ------------------------------------------------------------------------

def detect_country_label(phone_number):
    try:
        parsed_number = phonenumbers.parse(phone_number, None)
        region_code = phonenumbers.region_code_for_number(parsed_number)
        if region_code:
            country_name = geocoder.description_for_number(parsed_number, "en")
            country_code = parsed_number.country_code
            flag = "".join([chr(ord(c) + 127397) for c in region_code.upper()])
            return f"+{country_code} {country_name} {flag}"
    except: pass
    return "Unknown 🌍"

async def broadcast_to_all(context, text):
    user_ids = get_all_users()
    count = 0
    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=text, parse_mode='Markdown')
            count += 1
        except Exception: pass
    return count

# ------------------------------------------------------------------------
# ADMIN & HANDLERS
# ------------------------------------------------------------------------

async def delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    stats = get_inventory_stats()
    if not stats:
        await update.message.reply_text("🗑️ **Stock is empty.**", parse_mode='Markdown')
        return ConversationHandler.END
    text = "🗑️ **Delete Mode**\nSelect a category:\n"
    keyboard = []
    for (country, tag), count in sorted(stats.items()):
        btn_text = f"🗑️ {country} {tag} ({count})"
        callback_data = f"{country}||{tag}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback_data)])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data='cancel')])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return DELETE_SELECT

async def delete_select_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'cancel':
        await query.edit_message_text("❌ Cancelled.")
        return ConversationHandler.END
    context.user_data['delete_target'] = query.data
    country, tag = query.data.split('||')
    await query.edit_message_text(f"Selected: **{country}** - **{tag}**\n\nType amount or `ALL`.", parse_mode='Markdown')
    return DELETE_AMOUNT

async def delete_perform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().upper()
    target_data = context.user_data.get('delete_target')
    if not target_data: return ConversationHandler.END
    target_country, target_tag = target_data.split('||')
    try:
        deleted_count = delete_stock_db(target_country, target_tag, text)
        await update.message.reply_text(f"✅ **Deleted {deleted_count} numbers.**")
    except ValueError:
        await update.message.reply_text("⚠️ Invalid number.")
        return DELETE_AMOUNT
    context.user_data.clear()
    return ConversationHandler.END

async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    await update.message.reply_text("📢 **Broadcast Mode**\nType message:", parse_mode='Markdown')
    return BROADCAST_MSG

async def broadcast_perform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message.text
    status_msg = await update.message.reply_text("⏳ Sending broadcast...")
    count = await broadcast_to_all(context, message)
    await status_msg.edit_text(f"✅ **Broadcast Sent!**\n\n📨 Delivered to: {count} users.")
    return ConversationHandler.END

async def admin_upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    document = update.message.document
    if not document or not document.file_name.endswith('.txt'):
        await update.message.reply_text("⚠️ Upload a .txt file.")
        return ConversationHandler.END
    file = await context.bot.get_file(document.file_id)
    byte_array = await file.download_as_bytearray()
    content = byte_array.decode('utf-8')
    lines = [l.strip() for l in content.split('\n') if l.strip()]
    if not lines:
        await update.message.reply_text("⚠️ Empty file.")
        return ConversationHandler.END
    context.user_data['upload_lines'] = lines
    await update.message.reply_text(f"📂 **Received {len(lines)} numbers.**\n\n1️⃣ **Enter Tag:** (e.g., `WS+FB`)", parse_mode='Markdown')
    return ASK_TAG

async def admin_receive_tag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tag = update.message.text.strip()
    lines = context.user_data.get('upload_lines')
    added, countries = add_numbers_to_db(lines, tag)
    countries_str = "\n".join(countries)
    await update.message.reply_text(f"✅ **Done!**\n📥 Added: {added}\n🏷️ Tag: {tag}\n🌍 **Regions:**\n{countries_str}", parse_mode='Markdown')
    notification_text = (f"New Number Added!!\nCountry: {countries_str}\nCapacity: {added}\n\nAll Numbers are New and Fresh.")
    await broadcast_to_all(context, notification_text)
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_op(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    profile = get_profile(user_id)
    balance = profile['balance']
    if balance < 0.5:
        await update.callback_query.answer(f"❌ Min withdrawal: $0.50 (You have ${balance:.4f})", show_alert=True)
        return ConversationHandler.END
    await update.callback_query.edit_message_text(f"💰 **Withdrawal**\n\nYour Balance: `${balance:.4f}`\n\nPlease type the **Amount**:", parse_mode='Markdown')
    return WITHDRAW_AMOUNT

async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    profile = get_profile(user_id)
    try: amount = float(text)
    except ValueError:
        await update.message.reply_text("⚠️ Invalid number.")
        return WITHDRAW_AMOUNT
    if amount > profile['balance']:
        await update.message.reply_text("❌ Insufficient balance.")
        return WITHDRAW_AMOUNT
    context.user_data['withdraw_amount'] = amount
    await update.message.reply_text("💳 Type **Address/Method**:")
    return WITHDRAW_METHOD

async def withdraw_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    method_info = update.message.text.strip()
    amount = context.user_data['withdraw_amount']
    add_balance(user_id, -amount)
    admin_text = (f"💸 **Withdrawal Request**\n\n👤 {update.effective_user.mention_html()}\n"
                  f"🆔 `{user_id}`\n💰 `${amount:.4f}`\n🏦 `{method_info}`\n\n⚠️ Balance deducted.")
    try:
        await context.bot.send_message(chat_id=WITHDRAWAL_GROUP_ID, text=admin_text, parse_mode='HTML')
        await update.message.reply_text("✅ **Request Sent!**")
    except:
        await update.message.reply_text("❌ Error. Refunded.")
        add_balance(user_id, amount)
    return ConversationHandler.END

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if args and args[0].isdigit():
        referrer_id = int(args[0])
        if process_referral(user_id, referrer_id):
            try: await context.bot.send_message(chat_id=referrer_id, text=f"🎉 **New Referral!**\nYou earned ${REWARD_REFERRAL}!", parse_mode='Markdown')
            except: pass
    get_profile(user_id)
    await show_inventory_menu(update, context)

async def account_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    profile = get_profile(user.id)
    ref_link = f"https://t.me/{context.bot.username}?start={user.id}"
    text = (f"👤 **My Account**\n\n🆔 ID: `{user.id}`\n💰 Balance: `${profile['balance']:.4f}`\n"
            f"💸 Total Earned: `${profile['total_earned']:.4f}`\n\n👥 **Referrals**\nTotal: {profile['referrals_count']}\n"
            f"🔗 Link: `{ref_link}`")
    keyboard = [[InlineKeyboardButton("💰 Withdraw", callback_data='withdraw_start')],
                [InlineKeyboardButton("🔙 Back to Menu", callback_data='main_menu')]]
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def show_inventory_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, is_callback=False):
    stats = get_inventory_stats()
    keyboard = []
    if not stats:
        text = "🚫 No numbers available right now."
    else:
        text = "🌍 **Select your country:**"
        for (country, tag), count in sorted(stats.items()):
            parts = country.split()
            if len(parts) >= 2 and parts[0].startswith('+'):
                code = parts[0]
                flag = parts[-1]
                name = " ".join(parts[1:-1])
                btn_text = f"{flag} {name} {tag} {code} ({count})"
            else:
                btn_text = f"{country} {tag} ({count})"
            callback_data = f"buy||{country}||{tag}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback_data)])
    reply_markup = InlineKeyboardMarkup(keyboard)
    if is_callback:
        try: await update.callback_query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode='Markdown')
        except: pass
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup, parse_mode='Markdown')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data.startswith('buy||'):
        _, country_req, tag_req = data.split('||')
        active = get_user_active_number(user_id)
        if active:
            left, _ = get_stock_counts(country_req, tag_req)
            if left == 0:
                await query.answer("❌ That package is sold out!", show_alert=True)
                return
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("UPDATE numbers SET status='used', user_id=NULL WHERE phone=?", (active['phone'],))
            conn.commit()
            conn.close()
        entry = assign_number(user_id, country_req, tag_req)
        if entry:
            await show_number_panel(update, context, entry, is_edit=True)
        else:
            await query.answer("❌ Sold out just now!", show_alert=True)
            await show_inventory_menu(update, context, is_callback=True)
    elif data == 'change_number':
        result = replace_user_number(user_id)
        if result == "NO_STOCK":
            await query.answer("🚫 No replacement numbers available!", show_alert=True)
        elif result == "ERROR" or result is None:
            await query.answer("⚠️ Error finding number.", show_alert=True)
        else:
            await show_number_panel(update, context, result, is_edit=True)
    elif data == 'main_menu':
        await show_inventory_menu(update, context, is_callback=True)
    elif data == 'withdraw_start':
        await withdraw_start(update, context)

async def show_number_panel(update, context, entry, is_edit=False, extra_info=None):
    parts = entry['country'].split()
    flag = parts[-1] if len(parts) >= 2 and parts[0].startswith('+') else ""
    name = " ".join(parts[1:-1]) if len(parts) >= 2 else entry['country']
    if entry.get('last_msg'):
        otp_display = f"📩 **Message:**\n`{entry['last_msg']}`"
    else:
        otp_display = extra_info if extra_info else "Waiting for OTP..."
    text = (f"{flag} {name} {entry['tags']} Number Assigned:\n`{entry['number']}`\n\n{otp_display}")
    keyboard = [[InlineKeyboardButton("🔄 Change Number", callback_data='change_number')],
                [InlineKeyboardButton("🌍 Change Country", callback_data='main_menu')],
                [InlineKeyboardButton("👥 OTP Group", url=OTP_GROUP_LINK)]]
    msg = None
    if is_edit:
        try: msg = await update.callback_query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        except: pass
    else:
        msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    if msg: update_number_message_id(entry['number'], msg.message_id)
    elif is_edit and update.callback_query.message: update_number_message_id(entry['number'], update.callback_query.message.message_id)

if __name__ == '__main__':
    init_db()
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    job_queue = application.job_queue
    job_queue.run_repeating(check_active_otps_job, interval=5, first=2)
    upload_conv = ConversationHandler(entry_points=[MessageHandler(filters.Document.FileExtension("txt"), admin_upload_start)], states={ ASK_TAG: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_receive_tag)] }, fallbacks=[CommandHandler('cancel', cancel_op)], per_message=False)
    delete_conv = ConversationHandler(entry_points=[CommandHandler('delete', delete_start)], states={ DELETE_SELECT: [CallbackQueryHandler(delete_select_category)], DELETE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_perform)] }, fallbacks=[CommandHandler('cancel', cancel_op)], per_message=False)
    broadcast_conv = ConversationHandler(entry_points=[CommandHandler('broadcast', broadcast_start)], states={ BROADCAST_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_perform)] }, fallbacks=[CommandHandler('cancel', cancel_op)], per_message=False)
    withdraw_conv = ConversationHandler(entry_points=[CallbackQueryHandler(withdraw_start, pattern='^withdraw_start$')], states={ WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount)], WITHDRAW_METHOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_method)] }, fallbacks=[CommandHandler('cancel', cancel_op)], per_message=False)
    application.add_handler(upload_conv)
    application.add_handler(delete_conv)
    application.add_handler(broadcast_conv)
    application.add_handler(withdraw_conv)
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('account', account_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    print("Bot is running with SQLite & Low RAM Optimization...")
    application.run_polling()
