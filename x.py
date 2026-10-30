# -*- coding: utf-8 -*-
import os, sys, time, sqlite3, subprocess, tempfile, shutil, zipfile
import logging, atexit, threading, uuid
from datetime import datetime, timedelta
from threading import Thread
import telebot
from telebot import types
import psutil
from flask import Flask

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ───────────────────────── CONFIG ─────────────────────────
TOKEN = os.getenv('TOKEN', '8620671084:AAHi_Za_rDVYdPwhzH66pT-kj8E5wXD53gI')
OWNER_ID = int(os.getenv('OWNER_ID', '7986731915'))
ADMIN_ID = int(os.getenv('ADMIN_ID', '7986731915'))
YOUR_USERNAME = os.getenv('YOUR_USERNAME', 'King_X_vau')
UPDATE_CHANNEL = os.getenv('UPDATE_CHANNEL', 'King_X_vau')

FREE_USER_LIMIT = int(os.getenv('FREE_USER_LIMIT', 2))
SUBSCRIBED_USER_LIMIT = int(os.getenv('SUBSCRIBED_USER_LIMIT', 20000))
ADMIN_LIMIT = int(os.getenv('ADMIN_LIMIT', 999))
OWNER_LIMIT = float('inf')

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_BOTS_DIR = os.path.join(BASE_DIR, 'upload_bots')
IROTECH_DIR = os.path.join(BASE_DIR, 'inf')
DATABASE_PATH = os.path.join(IROTECH_DIR, 'bot_data.db')
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB

os.makedirs(UPLOAD_BOTS_DIR, exist_ok=True)
os.makedirs(IROTECH_DIR, exist_ok=True)

bot = telebot.TeleBot(TOKEN)
DB_LOCK = threading.Lock()

# ───────────────────────── STATE ─────────────────────────
bot_scripts = {}          # key: "{owner}_{file}" -> {process, log_file, ...}
pending_files = {}        # key: file_id -> {user_id, file_path, file_name, file_type, message_id}
user_subscriptions = {}
user_files = {}           # uid -> [(file_name, type)]
active_users = set()
admin_ids = {ADMIN_ID, OWNER_ID}
banned_users = set()
user_limits = {}
mandatory_channels = {}   # channel_id: {name, username, link}
bot_locked = False

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ───────────────────────── FLASK ─────────────────────────
flask_app = Flask('__main__')
@flask_app.route('/')
def home(): return "🤖 SHADOW HOST BOT is alive"
def keep_alive():
    try:
        port = int(os.environ.get("PORT", 8080))
        t = Thread(target=lambda: flask_app.run(host='0.0.0.0', port=port, use_reloader=False))
        t.daemon = True
        t.start()
        logger.info(f"✅ Flask keep-alive on port {port}")
    except Exception as e: logger.error(f"Keep-alive failed: {e}")

# ───────────────────────── DATABASE ─────────────────────────
def init_db():
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS subscriptions (user_id INTEGER PRIMARY KEY, expiry TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS user_files (user_id INTEGER, file_name TEXT, file_type TEXT, PRIMARY KEY (user_id, file_name))')
    c.execute('CREATE TABLE IF NOT EXISTS active_users (user_id INTEGER PRIMARY KEY, join_date TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)')
    c.execute('CREATE TABLE IF NOT EXISTS banned_users (user_id INTEGER PRIMARY KEY)')
    c.execute('CREATE TABLE IF NOT EXISTS user_limits (user_id INTEGER PRIMARY KEY, file_limit INTEGER)')
    c.execute('CREATE TABLE IF NOT EXISTS channels (channel_id TEXT PRIMARY KEY, name TEXT, username TEXT, link TEXT)')
    
    c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (OWNER_ID,))
    if ADMIN_ID != OWNER_ID:
        c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (ADMIN_ID,))
    conn.commit()
    conn.close()

def load_data():
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    for uid, exp in c.execute('SELECT user_id, expiry FROM subscriptions'):
        try: user_subscriptions[uid] = {'expiry': datetime.fromisoformat(exp)}
        except: pass
    for uid, fn, ft in c.execute('SELECT user_id, file_name, file_type FROM user_files'):
        user_files.setdefault(uid, []).append((fn, ft))
    active_users.update(u for (u,) in c.execute('SELECT user_id FROM active_users'))
    admin_ids.update(u for (u,) in c.execute('SELECT user_id FROM admins'))
    banned_users.update(u for (u,) in c.execute('SELECT user_id FROM banned_users'))
    for uid, fl in c.execute('SELECT user_id, file_limit FROM user_limits'):
        user_limits[uid] = fl
    for cid, name, uname, link in c.execute('SELECT channel_id, name, username, link FROM channels'):
        mandatory_channels[cid] = {'name': name, 'username': uname, 'link': link}
    conn.close()

init_db()
load_data()

# ───────────────────────── HELPERS ─────────────────────────
def get_user_folder(uid):
    folder = os.path.join(UPLOAD_BOTS_DIR, str(uid))
    os.makedirs(folder, exist_ok=True)
    return folder

def get_user_file_limit(uid):
    if uid == OWNER_ID: return OWNER_LIMIT
    if uid in admin_ids: return ADMIN_LIMIT
    if uid in user_limits: return user_limits[uid]
    if uid in user_subscriptions and user_subscriptions[uid]['expiry'] > datetime.now():
        return SUBSCRIBED_USER_LIMIT
    return FREE_USER_LIMIT

def get_user_file_count(uid):
    return len(user_files.get(uid, []))

def is_running(owner_id, file_name):
    key = f"{owner_id}_{file_name}"
    info = bot_scripts.get(key)
    if not info or not info.get('process'): return False
    try:
        p = psutil.Process(info['process'].pid)
        if p.is_running() and p.status() != psutil.STATUS_ZOMBIE: return True
    except psutil.NoSuchProcess: pass
    try:
        if info.get('log_file') and not info['log_file'].closed: info['log_file'].close()
    except: pass
    bot_scripts.pop(key, None)
    return False

def kill_tree(info):
    try:
        lf = info.get('log_file')
        if lf and not lf.closed: lf.close()
    except: pass
    proc = info.get('process')
    if not proc or not getattr(proc, 'pid', None): return
    try:
        parent = psutil.Process(proc.pid)
        for ch in parent.children(recursive=True):
            try: ch.kill()
            except: pass
        parent.kill()
    except Exception: pass

def check_channels(uid):
    if not mandatory_channels: return True, []
    not_joined = []
    for cid, data in mandatory_channels.items():
        try:
            member = bot.get_chat_member(cid, uid)
            if member.status not in ['member', 'administrator', 'creator']:
                not_joined.append(data)
        except Exception:
            not_joined.append(data)
    return len(not_joined) == 0, not_joined

# ───────────────────────── KEYBOARDS ─────────────────────────
def get_main_keyboard(uid):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    if uid in admin_ids:
        buttons = [
            "📤 Upload File", "📂 Check Files",
            "⚡ Bot Speed", "📊 Statistics",
            "💳 Subscriptions", "📢 Broadcast",
            "🔒 Lock Bot", "🟢 Running All Code",
            "👑 Admin Panel", "📞 Contact Owner",
            "📢 Channel Add", "🛠️ Manual Install",
            "👥 User Management", "⚙️ Settings"
        ]
    else:
        buttons = [
            "📢 Updates Channel",
            "📤 Upload File", "📂 Check Files",
            "⚡ Bot Speed", "📊 Statistics",
            "📞 Contact Owner", "🆘 Help"
        ]
    markup.add(*[types.KeyboardButton(text) for text in buttons])
    return markup

def get_admin_panel_inline():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.row(types.InlineKeyboardButton('➕ Add Admin', callback_data='adm_add'), types.InlineKeyboardButton('➖ Remove Admin', callback_data='adm_rem'))
    markup.row(types.InlineKeyboardButton('📋 List Admins', callback_data='adm_list'))
    return markup

def get_user_mgmt_inline():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.row(types.InlineKeyboardButton('🚫 Ban User', callback_data='usr_ban'), types.InlineKeyboardButton('✅ Unban User', callback_data='usr_unban'))
    markup.row(types.InlineKeyboardButton('🔧 Set Limit', callback_data='usr_limit'), types.InlineKeyboardButton('📊 User Info', callback_data='usr_info'))
    return markup

# ───────────────────────── RUNNER ─────────────────────────
def run_script(script_path, owner_id, user_folder, file_name, chat_id):
    if not os.path.exists(script_path):
        bot.send_message(chat_id, f"❌ Script `{file_name}` not found!")
        return

    ext = os.path.splitext(file_name)[1].lower()
    log_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
    
    try:
        log_file = open(log_path, 'w', encoding='utf-8', errors='ignore')
        cmd = [sys.executable, script_path] if ext == '.py' else ['node', script_path]
        
        process = subprocess.Popen(
            cmd, cwd=user_folder, stdout=log_file, stderr=log_file, stdin=subprocess.PIPE
        )
        
        key = f"{owner_id}_{file_name}"
        bot_scripts[key] = {
            'process': process, 'log_file': log_file, 'file_name': file_name,
            'script_owner_id': owner_id, 'user_folder': user_folder,
            'type': ext[1:], 'script_key': key
        }
        bot.send_message(chat_id, f"✅ Started `{file_name}` (PID: `{process.pid}`)", parse_mode='Markdown')
    except Exception as e:
        bot.send_message(chat_id, f"❌ Failed to start `{file_name}`: {e}")

# ───────────────────────── COMMANDS & BUTTONS ─────────────────────────
@bot.message_handler(commands=['start'])
def cmd_start(m):
    uid = m.from_user.id
    if uid in banned_users: return bot.reply_to(m, "❌ You are banned.")
    if uid not in active_users:
        active_users.add(uid)
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        conn.execute('INSERT OR IGNORE INTO active_users (user_id, join_date) VALUES (?, ?)', (uid, datetime.now().isoformat()))
        conn.commit(); conn.close()
    
    text = f"〽️ Welcome, {m.from_user.first_name}!\n\n🆔 Your ID: `{uid}`\n📁 Files: {get_user_file_count(uid)}/{get_user_file_limit(uid)}\n\n👇 Use buttons below."
    bot.reply_to(m, text, reply_markup=get_main_keyboard(uid), parse_mode='Markdown')

@bot.message_handler(func=lambda m: m.text in ["📤 Upload File", "📂 Check Files", "⚡ Bot Speed", "📊 Statistics", "💳 Subscriptions", "📢 Broadcast", "🔒 Lock Bot", "🟢 Running All Code", "👑 Admin Panel", "📞 Contact Owner", "📢 Channel Add", "🛠️ Manual Install", "👥 User Management", "⚙️ Settings", "📢 Updates Channel", "🆘 Help"])
def handle_buttons(m):
    uid = m.from_user.id
    text = m.text
    if uid in banned_users: return bot.reply_to(m, "❌ You are banned.")

    if text == "📤 Upload File":
        bot.reply_to(m, "📤 Send a `.py`, `.js`, or `.zip` file.\n*It will be sent to Admin for approval.*", parse_mode='Markdown')
    elif text == "📂 Check Files":
        files = user_files.get(uid, [])
        if not files: return bot.reply_to(m, "📂 No files uploaded yet.")
        markup = types.InlineKeyboardMarkup(row_width=1)
        for fn, ft in files:
            icon = "🟢" if is_running(uid, fn) else "🔴"
            markup.add(types.InlineKeyboardButton(f"{fn} ({ft}) {icon}", callback_data=f"file_{uid}_{fn}"))
        bot.reply_to(m, "📂 Your Files:", reply_markup=markup)
    elif text == "⚡ Bot Speed":
        start = time.time()
        msg = bot.reply_to(m, "Testing...")
        bot.edit_message_text(f"⚡ Latency: {(time.time()-start)*1000:.0f} ms\n🔒 Bot: {'Locked' if bot_locked else 'Unlocked'}", m.chat.id, msg.message_id)
    elif text == "📊 Statistics":
        bot.reply_to(m, f"📊 Stats:\n👥 Users: {len(active_users)}\n📁 Files: {sum(len(f) for f in user_files.values())}\n🟢 Running: {len(bot_scripts)}")
    elif text == "📢 Updates Channel":
        bot.reply_to(m, f"Join: https://t.me/{UPDATE_CHANNEL.replace('@', '')}")
    elif text == "📞 Contact Owner":
        bot.reply_to(m, f"Owner: @{YOUR_USERNAME.replace('@', '')}")
    elif text == "🆘 Help":
        bot.reply_to(m, "Upload `.py` or `.js` files. Admin will approve and run them.")
    
    # ── Admin Buttons ──
    elif uid in admin_ids:
        if text == "👑 Admin Panel":
            bot.reply_to(m, "👑 Admin Panel", reply_markup=get_admin_panel_inline())
        elif text == "👥 User Management":
            bot.reply_to(m, "👥 User Management", reply_markup=get_user_mgmt_inline())
        elif text == "🔒 Lock Bot":
            global bot_locked
            bot_locked = not bot_locked
            bot.reply_to(m, f"Bot is now {'Locked 🔒' if bot_locked else 'Unlocked 🔓'}")
        elif text == "📢 Broadcast":
            msg = bot.reply_to(m, "📢 Send the message to broadcast. /cancel to abort.")
            bot.register_next_step_handler(msg, do_broadcast)
        elif text == "🟢 Running All Code":
            bot.reply_to(m, "🟢 Starting all saved scripts...")
            count = 0
            for oid, files in user_files.items():
                for fn, ft in files:
                    if not is_running(oid, fn):
                        folder = get_user_folder(oid)
                        threading.Thread(target=run_script, args=(os.path.join(folder, fn), oid, folder, fn, m.chat.id)).start()
                        count += 1; time.sleep(0.5)
            bot.reply_to(m, f"✅ Started {count} scripts.")
        elif text == "📢 Channel Add":
            msg = bot.reply_to(m, "📢 Send Channel ID (e.g. `-100123456789`). Make sure bot is Admin there.\n/cancel to abort.")
            bot.register_next_step_handler(msg, do_add_channel)
        elif text == "💳 Subscriptions":
            msg = bot.reply_to(m, "💳 Send `user_id days` to add subscription.")
            bot.register_next_step_handler(msg, do_add_sub)
        elif text == "🛠️ Manual Install":
            msg = bot.reply_to(m, "🛠️ Send `user_id package_name` to install.")
            bot.register_next_step_handler(msg, do_manual_install)

def do_broadcast(m):
    if m.text.lower() == '/cancel': return bot.reply_to(m, "Cancelled.")
    sent, failed = 0, 0
    msg = bot.reply_to(m, "Broadcasting...")
    for u in active_users:
        try:
            bot.copy_message(u, m.chat.id, m.message_id)
            sent += 1
        except: failed += 1
        time.sleep(0.05)
    bot.edit_message_text(f"✅ Broadcast done. Sent: {sent}, Failed: {failed}", m.chat.id, msg.message_id)

def do_add_channel(m):
    if m.text.lower() == '/cancel': return bot.reply_to(m, "Cancelled.")
    cid = m.text.strip()
    try:
        chat = bot.get_chat(cid)
        bot_member = bot.get_chat_member(cid, bot.get_me().id)
        if bot_member.status not in ['administrator', 'creator']: return bot.reply_to(m, "❌ Bot must be Admin in that channel.")
        
        link = chat.invite_link or (f"https://t.me/{chat.username}" if chat.username else "No Link")
        mandatory_channels[str(chat.id)] = {'name': chat.title, 'username': chat.username, 'link': link}
        
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        conn.execute('INSERT OR REPLACE INTO channels (channel_id, name, username, link) VALUES (?, ?, ?, ?)', (str(chat.id), chat.title, chat.username, link))
        conn.commit(); conn.close()
        bot.reply_to(m, f"✅ Channel Added: {chat.title}\nLink: {link}")
    except Exception as e: bot.reply_to(m, f"❌ Error: {e}")

def do_add_sub(m):
    try:
        parts = m.text.split()
        uid, days = int(parts[0]), int(parts[1])
        exp = datetime.now() + timedelta(days=days)
        user_subscriptions[uid] = {'expiry': exp}
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        conn.execute('INSERT OR REPLACE INTO subscriptions (user_id, expiry) VALUES (?, ?)', (uid, exp.isoformat()))
        conn.commit(); conn.close()
        bot.reply_to(m, f"✅ Sub for `{uid}` added till {exp:%Y-%m-%d}", parse_mode='Markdown')
    except: bot.reply_to(m, "❌ Format: `user_id days`")

def do_manual_install(m):
    try:
        parts = m.text.split()
        uid, pkg = int(parts[0]), parts[1]
        bot.reply_to(m, f"Installing `{pkg}` for {uid}...")
        r = subprocess.run([sys.executable, '-m', 'pip', 'install', pkg], capture_output=True, text=True)
        if r.returncode == 0: bot.reply_to(m, f"✅ Installed `{pkg}`")
        else: bot.reply_to(m, f"❌ Failed: {r.stderr[:200]}")
    except: bot.reply_to(m, "❌ Format: `user_id package_name`")

# ───────────────────────── FILE UPLOAD & APPROVAL ─────────────────────────
@bot.message_handler(content_types=['document'])
def handle_doc(m):
    uid = m.from_user.id
    if uid in banned_users: return bot.reply_to(m, "❌ You are banned.")
    if bot_locked and uid not in admin_ids: return bot.reply_to(m, "⚠️ Bot locked.")

    if get_user_file_count(uid) >= get_user_file_limit(uid):
        return bot.reply_to(m, f"⚠️ Limit reached ({get_user_file_limit(uid)}).")

    doc = m.document
    fn = doc.file_name
    ext = os.path.splitext(fn)[1].lower() if fn else ''
    if ext not in ['.py', '.js', '.zip']: return bot.reply_to(m, "⚠️ Only `.py`, `.js`, `.zip` allowed.")
    if doc.file_size > MAX_FILE_SIZE: return bot.reply_to(m, "⚠️ File too large (Max 20MB).")

    wait = bot.reply_to(m, f"⏳ Downloading `{fn}`...", parse_mode='Markdown')
    try:
        file_info = bot.get_file(doc.file_id)
        content = bot.download_file(file_info.file_path)
        folder = get_user_folder(uid)
        file_path = os.path.join(folder, fn)
        
        with open(file_path, 'wb') as f: f.write(content)
        
        # ── Create Pending Request ──
        pending_id = str(uuid.uuid4())[:8]
        pending_files[pending_id] = {
            'user_id': uid, 'file_name': fn, 'file_path': file_path, 
            'file_type': ext[1:], 'chat_id': m.chat.id
        }

        # ── Notify Admins ──
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("✅ Approve", callback_data=f"appr_{pending_id}"),
            types.InlineKeyboardButton("❌ Reject", callback_data=f"rej_{pending_id}")
        )
        admin_msg = f"🚨 **New File Upload**\n👤 User: `{uid}`\n📁 File: `{fn}`\n\nDo you want to approve and run this file?"
        
        for adm in admin_ids:
            try: bot.send_message(adm, admin_msg, reply_markup=markup, parse_mode='Markdown')
            except: pass
        
        bot.edit_message_text(f"⏳ `{fn}` sent to Admin for approval. Please wait...", m.chat.id, wait.message_id, parse_mode='Markdown')
    
    except Exception as e:
        bot.edit_message_text(f"❌ Error: {e}", m.chat.id, wait.message_id)

@bot.callback_query_handler(func=lambda c: c.data.startswith('appr_') or c.data.startswith('rej_'))
def handle_approval(call):
    if call.from_user.id not in admin_ids: return bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
    
    action, pid = call.data.split('_', 1)
    if pid not in pending_files: return bot.answer_callback_query(call.id, "Expired or already handled.", show_alert=True)

    data = pending_files[pid]
    uid, fn, fpath, ft, cid = data['user_id'], data['file_name'], data['file_path'], data['file_type'], data['chat_id']

    if action == 'appr':
        bot.edit_message_text(f"✅ Approved `{fn}` by {call.from_user.first_name}", call.message.chat.id, call.message.message_id)
        bot.send_message(cid, f"✅ Your file `{fn}` was approved! Starting...", parse_mode='Markdown')
        
        # Save to DB
        user_files.setdefault(uid, []).append((fn, ft))
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        conn.execute('INSERT OR REPLACE INTO user_files (user_id, file_name, file_type) VALUES (?, ?, ?)', (uid, fn, ft))
        conn.commit(); conn.close()
        
        # Run File
        threading.Thread(target=run_script, args=(fpath, uid, get_user_folder(uid), fn, cid)).start()

    elif action == 'rej':
        bot.edit_message_text(f"❌ Rejected `{fn}` by {call.from_user.first_name}", call.message.chat.id, call.message.message_id)
        bot.send_message(cid, f"❌ Your file `{fn}` was rejected by Admin.")
        try: os.remove(fpath)
        except: pass

    del pending_files[pid]
    bot.answer_callback_query(call.id)

# ───────────────────────── FILE CONTROLS ─────────────────────────
@bot.callback_query_handler(func=lambda c: c.data.startswith('file_'))
def handle_file_control(call):
    _, owner, fn = call.data.split('_', 2)
    owner = int(owner)
    if call.from_user.id != owner and call.from_user.id not in admin_ids:
        return bot.answer_callback_query(call.id, "Not your file.", show_alert=True)
    
    running = is_running(owner, fn)
    markup = types.InlineKeyboardMarkup(row_width=2)
    if running:
        markup.row(types.InlineKeyboardButton("🔴 Stop", callback_data=f"stop_{owner}_{fn}"), types.InlineKeyboardButton("🔄 Restart", callback_data=f"rest_{owner}_{fn}"))
    else:
        markup.row(types.InlineKeyboardButton("🟢 Start", callback_data=f"start_{owner}_{fn}"))
    markup.row(types.InlineKeyboardButton("📜 Logs", callback_data=f"logs_{owner}_{fn}"), types.InlineKeyboardButton("🗑️ Delete", callback_data=f"del_{owner}_{fn}"))
    
    bot.edit_message_text(f"⚙️ `{fn}`\nStatus: {'🟢 Running' if running else '🔴 Stopped'}", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith(('start_', 'stop_', 'rest_', 'del_', 'logs_')))
def process_file_action(call):
    action, owner, fn = call.data.split('_', 2)
    owner = int(owner)
    if call.from_user.id != owner and call.from_user.id not in admin_ids: return bot.answer_callback_query(call.id, "No permission.", show_alert=True)
    
    key = f"{owner}_{fn}"
    folder = get_user_folder(owner)
    fpath = os.path.join(folder, fn)
    ft = next((f[1] for f in user_files.get(owner, []) if f[0] == fn), 'py')

    if action == 'start' and not is_running(owner, fn):
        threading.Thread(target=run_script, args=(fpath, owner, folder, fn, call.message.chat.id)).start()
        bot.answer_callback_query(call.id, "Starting...")
    elif action == 'stop':
        if key in bot_scripts:
            kill_tree(bot_scripts[key])
            bot_scripts.pop(key, None)
        bot.answer_callback_query(call.id, "Stopped.")
    elif action == 'rest':
        if key in bot_scripts:
            kill_tree(bot_scripts[key])
            bot_scripts.pop(key, None)
        time.sleep(1)
        threading.Thread(target=run_script, args=(fpath, owner, folder, fn, call.message.chat.id)).start()
        bot.answer_callback_query(call.id, "Restarting...")
    elif action == 'del':
        if key in bot_scripts:
            kill_tree(bot_scripts[key])
            bot_scripts.pop(key, None)
        for p in (fpath, os.path.join(folder, os.path.splitext(fn)[0]+'.log')):
            if os.path.exists(p): os.remove(p)
        user_files[owner] = [f for f in user_files.get(owner, []) if f[0] != fn]
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        conn.execute('DELETE FROM user_files WHERE user_id=? AND file_name=?', (owner, fn))
        conn.commit(); conn.close()
        bot.edit_message_text(f"🗑️ Deleted `{fn}`", call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id)
    elif action == 'logs':
        lp = os.path.join(folder, os.path.splitext(fn)[0]+'.log')
        if not os.path.exists(lp): return bot.answer_callback_query(call.id, "No logs.", show_alert=True)
        with open(lp, 'r', errors='ignore') as f: content = f.read()[-3000:]
        bot.send_message(call.message.chat.id, f"📜 Logs:\n```\n{content or '(empty)'}\n```", parse_mode='Markdown')
        bot.answer_callback_query(call.id)

# ───────────────────────── ADMIN INLINE CALLBACKS ─────────────────────────
@bot.callback_query_handler(func=lambda c: c.data.startswith(('adm_', 'usr_')))
def handle_admin_cbs(call):
    if call.from_user.id not in admin_ids: return bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
    data = call.data
    
    if data == 'adm_add':
        msg = bot.send_message(call.message.chat.id, "Send User ID to add as Admin.")
        bot.register_next_step_handler(msg, lambda m: add_admin(m, call.message))
    elif data == 'adm_rem':
        msg = bot.send_message(call.message.chat.id, "Send User ID to remove from Admin.")
        bot.register_next_step_handler(msg, lambda m: rem_admin(m, call.message))
    elif data == 'adm_list':
        bot.send_message(call.message.chat.id, "👑 Admins:\n" + "\n".join(f"- `{a}`" for a in admin_ids), parse_mode='Markdown')
    elif data == 'usr_ban':
        msg = bot.send_message(call.message.chat.id, "Send `user_id reason` to ban.")
        bot.register_next_step_handler(msg, do_ban)
    elif data == 'usr_unban':
        msg = bot.send_message(call.message.chat.id, "Send `user_id` to unban.")
        bot.register_next_step_handler(msg, do_unban)
    elif data == 'usr_limit':
        msg = bot.send_message(call.message.chat.id, "Send `user_id limit`.")
        bot.register_next_step_handler(msg, do_set_limit)
    elif data == 'usr_info':
        msg = bot.send_message(call.message.chat.id, "Send `user_id` to check info.")
        bot.register_next_step_handler(msg, do_user_info)
    bot.answer_callback_query(call.id)

def add_admin(m, edit_msg):
    try:
        uid = int(m.text)
        admin_ids.add(uid)
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        conn.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (uid,))
        conn.commit(); conn.close()
        bot.reply_to(m, f"✅ `{uid}` is now an Admin.")
    except: bot.reply_to(m, "❌ Invalid ID.")

def rem_admin(m, edit_msg):
    try:
        uid = int(m.text)
        if uid == OWNER_ID: return bot.reply_to(m, "❌ Cannot remove Owner.")
        admin_ids.discard(uid)
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        conn.execute('DELETE FROM admins WHERE user_id=?', (uid,))
        conn.commit(); conn.close()
        bot.reply_to(m, f"✅ Removed `{uid}` from Admin.")
    except: bot.reply_to(m, "❌ Invalid ID.")

def do_ban(m):
    try:
        parts = m.text.split()
        uid = int(parts[0])
        reason = parts[1] if len(parts) > 1 else "No reason"
        if uid in admin_ids or uid == OWNER_ID: return bot.reply_to(m, "❌ Cannot ban Admin/Owner.")
        banned_users.add(uid)
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        conn.execute('INSERT OR IGNORE INTO banned_users (user_id) VALUES (?)', (uid,))
        conn.commit(); conn.close()
        bot.reply_to(m, f"✅ Banned `{uid}`")
    except: bot.reply_to(m, "❌ Format: `user_id reason`")

def do_unban(m):
    try:
        uid = int(m.text)
        banned_users.discard(uid)
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        conn.execute('DELETE FROM banned_users WHERE user_id=?', (uid,))
        conn.commit(); conn.close()
        bot.reply_to(m, f"✅ Unbanned `{uid}`")
    except: bot.reply_to(m, "❌ Invalid ID.")

def do_set_limit(m):
    try:
        uid, lim = map(int, m.text.split())
        user_limits[uid] = lim
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        conn.execute('INSERT OR REPLACE INTO user_limits (user_id, file_limit) VALUES (?, ?)', (uid, lim))
        conn.commit(); conn.close()
        bot.reply_to(m, f"✅ Limit for `{uid}` set to {lim}.")
    except: bot.reply_to(m, "❌ Format: `user_id limit`")

def do_user_info(m):
    try:
        uid = int(m.text)
        files = user_files.get(uid, [])
        running = sum(1 for fn, _ in files if is_running(uid, fn))
        txt = f"👤 User: `{uid}`\nBanned: {uid in banned_users}\nFiles: {len(files)}/{get_user_file_limit(uid)}\nRunning: {running}"
        bot.reply_to(m, txt, parse_mode='Markdown')
    except: bot.reply_to(m, "❌ Invalid ID.")

# ───────────────────────── MAIN ─────────────────────────
def cleanup():
    logger.warning("Stopping all scripts...")
    for key in list(bot_scripts.keys()):
        try: kill_tree(bot_scripts[key])
        except: pass
    bot_scripts.clear()

atexit.register(cleanup)

if __name__ == '__main__':
    logger.info("🤖 SHADOW BOT Started!")
    keep_alive()
    bot.infinity_polling(timeout=60, long_polling_timeout=30)