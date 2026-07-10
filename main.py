import telebot
import json
import uuid
import datetime
import os
import logging
import threading
import time
import requests
from flask import Flask, request, jsonify
import firebase_admin
from firebase_admin import credentials, firestore

# ================= SERVER CONFIG =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
SERVER_URL = os.getenv("SERVER_URL")
FIREBASE_CONFIG = os.getenv("FIREBASE_JSON")

# Process ADMIN_ID to avoid format errors
try:
    REAL_ADMIN_ID = str(ADMIN_ID).strip().replace("Value:", "").replace(" ", "")
except:
    REAL_ADMIN_ID = ""

# Setup Logging
logging.basicConfig(level=logging.INFO)
telebot.logger.setLevel(logging.INFO)

# Initialize Bot and Flask
bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
server = Flask(__name__)
import json
server.json.ensure_ascii = False

# ================= FIREBASE CONNECTION =================
db = None
try:
    if not FIREBASE_CONFIG:
        print("❌ ERROR: 'FIREBASE_JSON' environment variable is not configured")
    else:
        cred_dict = json.loads(FIREBASE_CONFIG)
        cred = credentials.Certificate(cred_dict)
        try:
            firebase_admin.get_app()
        except ValueError:
            firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("✅ FIREBASE CONNECTED SUCCESSFULLY!")
except Exception as e:
    print(f"⚠️ Firebase connection error: {e}")

# ================= HELPER FUNCTIONS =================
def calculate_expiry(duration_str):
    """Calculate expiration time from string (e.g., 1d, 2h)"""
    now = datetime.datetime.now()
    try:
        val = int(''.join(filter(str.isdigit, duration_str)))
        if 'd' in duration_str: return now + datetime.timedelta(days=val), f"{val} Days"
        if 'h' in duration_str: return now + datetime.timedelta(hours=val), f"{val} Hours"
        if 'm' in duration_str: return now + datetime.timedelta(minutes=val), f"{val} Minutes"
    except: pass
    return None, None

def check_admin(user_id):
    """Check Admin permission"""
    return str(user_id) == REAL_ADMIN_ID

def send_admin_notify(message):
    """Send notification to Admin Telegram"""
    try:
        if REAL_ADMIN_ID:
            bot.send_message(REAL_ADMIN_ID, message, parse_mode="Markdown")
    except Exception as e:
        print(f"⚠️ Error sending message: {e}")

def get_key_document(client_key, game_id):
    """
    Find key in DB.
    1. Exact match (System generated key).
    2. Match by Game Prefix (Custom key with same name).
    Returns: (document_snapshot, real_key_id_in_db)
    """
    try:
        # Method 1: Exact match
        doc_ref = db.collection('keys').document(client_key)
        doc = doc_ref.get()
        if doc.exists:
            return doc, client_key

        # Method 2: Match by Game Prefix (If client sends game_id)
        if game_id:
            prefixed_key = f"{game_id.upper()}-{client_key}"
            doc_ref_p = db.collection('keys').document(prefixed_key)
            doc_p = doc_ref_p.get()
            if doc_p.exists:
                return doc_p, prefixed_key
    except Exception:
        pass
    return None, None

# ================= AUTO CLEAN EXPIRED KEYS =================
def auto_clean_expired_keys():
    """Background thread: Scan and delete expired keys"""
    print("⏳ Waiting for server stabilization (30s)...")
    time.sleep(30)
    
    print("🧹 Starting expired key cleaner...")
    while True:
        try:
            if db:
                now = datetime.datetime.now()
                # Định dạng thời gian hiện tại thành chuỗi để query trực tiếp
                now_str = now.strftime("%Y-%m-%d %H:%M:%S")
                
                # CHỈ lấy những key có thời gian hết hạn nhỏ hơn thời gian hiện tại
                expired_docs = db.collection('keys').where('expiry', '<', now_str).stream()
                
                for doc in expired_docs:
                    key_id = doc.id
                    print(f"🗑️ Deleting expired key: {key_id}")
                    db.collection('keys').document(key_id).delete()
                    msg = f"🗑️ *Deleted expired key*\n`{key_id}`"
                    send_admin_notify(msg)
        except Exception as e:
            print(f"⚠️ Auto Clean thread error: {e}")
        
        # Tăng thời gian chờ lên 2 tiếng (7200 giây) thay vì 60 giây
        time.sleep(7200)


# ================= API CHECK KEY =================
@server.route('/check_key', methods=['POST'])
def api_check_key():
    try:
        if not db: return jsonify({"status": False, "msg": "Database Server Error"})
        
        # --- CHANGES HERE: Get data from Form instead of JSON ---
        client_key = request.form.get('key', '').strip()
        client_hwid = request.form.get('hwid', '').strip()
        client_game_id = request.form.get('game_id', '').strip().lower()

        # If request doesn't have key, it might be JSON fallback
        if not client_key:
            data = request.get_json(silent=True) or {}
            client_key = data.get('key', '').strip()
            client_hwid = data.get('hwid', '').strip()
            client_game_id = data.get('game_id', '').strip().lower()

        if not client_key:
            return jsonify({"status": False, "msg": "Key data not received from Game!"})
        
        # --- 1. CHECK BLOCK HWID ---
        try:
            blacklist_doc = db.collection('settings').document('blacklist').get()
            if blacklist_doc.exists:
                blocked_models = blacklist_doc.to_dict().get('models', [])
                for blocked_kw in blocked_models:
                    if str(blocked_kw).strip() == client_hwid:
                        return jsonify({"status": False, "msg": f"Your device has been blocked by Admin!"})
        except Exception: pass

        # --- 2. FIND KEY ---
        doc, real_key_id = get_key_document(client_key, client_game_id)

        if not doc:
            return jsonify({"status": False, "msg": "Key does not exist!"})

        key_data = doc.to_dict()
        doc_ref = db.collection('keys').document(real_key_id) 

        # --- 3. CHECK GAME ID (PREVENT CROSS-TOOL USAGE) ---
        key_game_id = key_data.get('game_id', 'all').lower()
        if key_game_id != 'all' and key_game_id != client_game_id:
             return jsonify({
                 "status": False, 
                 "msg": f"This key is for Tool: {key_game_id.upper()}\nYou are using Tool: {client_game_id.upper()}"
             })

        # --- 4. CHECK LOCK & EXPIRY ---
        if key_data.get('is_locked', False) is True:
             return jsonify({"status": False, "msg": "This key is permanently locked!"})

        try:
            expiry_dt = datetime.datetime.strptime(key_data['expiry'], "%Y-%m-%d %H:%M:%S")
            now = datetime.datetime.now()
            if now > expiry_dt:
                return jsonify({"status": False, "msg": "Key has expired!"})

            remaining = expiry_dt - now
            remain_str = str(remaining).split('.')[0] # Keep default English format for timedelta
        except:
            return jsonify({"status": False, "msg": "Date format error."})

        # --- 5. HANDLE VIP LOGIC ---
        if key_data['type'] == 'vip':
            stored_hwid = key_data.get('hwid')

            if stored_hwid is None:
                doc_ref.update({
                    "hwid": client_hwid,
                    "info": f"Active {client_hwid}"
                })
                notify_msg = (
                    f"👑 *VIP KEY ACTIVATED ({key_game_id.upper()})* 👑\n"
                    f"🔑 *Key:* `{client_key}`\n"
                    f"📱 *HWID:* `{client_hwid}`\n"
                    f"🕒 *Time:* `{now.strftime('%H:%M:%S %d/%m/%y')}`\n"                    
                    f"⏳ *Remaining:* `{remain_str}`"
                )
                send_admin_notify(notify_msg)
                return jsonify({"status": True, "msg": "VIP Active Success", "type": "VIP"})

            elif stored_hwid == client_hwid:
                return jsonify({"status": True, "msg": "VIP Login Success", "type": "VIP"})
            else:
                return jsonify({"status": False, "msg": "❌ Invalid device! Please contact Admin to reset."})

        # --- 6. HANDLE FREE LOGIC ---
        elif key_data['type'] == 'free':
            hwids = key_data.get('hwids', [])
            max_dev = key_data.get('max_devices', 1)

            if client_hwid in hwids:
                return jsonify({"status": True, "msg": "FREE Login Success", "type": "FREE"})

            elif len(hwids) < max_dev:
                doc_ref.update({
                    "hwids": firestore.ArrayUnion([client_hwid])
                })
                notify_msg = (
                    f"🎁 *FREE KEY ACTIVATED ({key_game_id.upper()})* 🎁\n"
                    f"🔑 *Key:* `{client_key}`\n"
                    f"📱 *HWID:* `{client_hwid}`\n"
                    f"🔢 *Slot:* {len(hwids) + 1}/{max_dev}\n"
                    f"⏳ *Remaining:* `{remain_str}`"
                )
                send_admin_notify(notify_msg)
                return jsonify({"status": True, "msg": "FREE Active Success", "type": "FREE"})
            else:
                return jsonify({"status": False, "msg": "Key slots are full (Max Devices Reached)"})

        return jsonify({"status": False, "msg": "Key data error."})
    except Exception as e:
        return jsonify({"status": False, "msg": f"System Error: {str(e)}"})


# ================= API STATS =================
@server.route('/stats', methods=['GET'])
def api_stats():
    try:
        if not db: return jsonify({"status": False, "vip": 0, "free": 0})
        vip_c, free_c = 0, 0
        docs = db.collection('keys').stream()
        for doc in docs:
            d = doc.to_dict()
            if d.get('type') == 'vip' and d.get('hwid'): vip_c += 1
            elif d.get('type') == 'free': free_c += len(d.get('hwids', []))
        return jsonify({"status": True, "vip": vip_c, "free": free_c})
    except:
        return jsonify({"status": False, "vip": 0, "free": 0})

# ================= WEBHOOK & ROUTES =================
@server.route('/webhook', methods=['POST'])
def webhook_handler():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        t = threading.Thread(target=bot.process_new_updates, args=([update],))
        t.start()
        return "OK", 200
    return "Access Denied", 403

@server.route("/")
def index():
    return f"✅ Server is Running... Time: {datetime.datetime.now()}", 200

@server.route("/set_webhook")
def set_webhook():
    bot.remove_webhook()
    clean_url = SERVER_URL.strip().rstrip('/')
    webhook_url = f"{clean_url}/webhook"
    bot.set_webhook(url=webhook_url)
    return f"✅ Webhook set to: {webhook_url}", 200

# ================= BOT COMMANDS =================
def show_menu(m):
    bot.reply_to(m, """
🔥 *AKMODPUBG - KEY SERVER* 🔥

1️⃣ *CREATE KEY*
   `/vip `*<LUAPAK> <time>*
   `/free `*<LUAPAK> <slots> <time>*
   `/custom vip `*<LUAPAK> <time> <key>*
   `/custom free `*<LUAPAK> <slots> <time> <key>*

2️⃣ *MANAGE KEYS*
   `/list `- *View key list (by Game)*
   `/delete `- *Delete key (enter system ID)*
   `/reset `- *Reset device (enter system ID)*
   `/lockkey `- *Lock key*
   `/unlockkey `- *Unlock key*

3️⃣ *BLOCK DEVICE*
   `/blockmodel `- *Block device*
   `/unlockmodel `- *Unlock device*
   `/listblock `- *View block list*
""", parse_mode="Markdown")

@bot.message_handler(commands=['start', 'help'])
def send_welcome(m):
    if check_admin(m.from_user.id): show_menu(m)

# --- CREATE VIP: /vip <game> <time> ---
@bot.message_handler(commands=['vip'])
def create_vip(m):
    if not check_admin(m.from_user.id): return
    try:
        args = m.text.split()
        if len(args) < 3: return bot.reply_to(m, "⚠️ Invalid syntax. Example: `/vip LUAPAK 1d`")
        
        game_id = args[1].lower()
        expiry, label = calculate_expiry(args[2])
        if not expiry: return bot.reply_to(m, "⚠️ Invalid time format.")
        
        key = f"{game_id.upper()}-VIP-{str(uuid.uuid4())[:6].upper()}"
        data = {
            "type": "vip", 
            "game_id": game_id,
            "hwid": None, 
            "expiry": expiry.strftime("%Y-%m-%d %H:%M:%S"), 
            "info": "Not activated",
            "is_locked": False,
            "created_at": firestore.SERVER_TIMESTAMP
        }
        db.collection('keys').document(key).set(data)
        bot.reply_to(m, f"👑 *VIP KEY CREATED {game_id.upper()} ({label})*\n\n🔑 *KEY*: `{key}`", parse_mode="Markdown")
    except Exception as e: bot.reply_to(m, f"❌ Error: {e}")

# --- CREATE FREE: /free <game> <slot> <time> ---
@bot.message_handler(commands=['free'])
def create_free(m):
    if not check_admin(m.from_user.id): return
    try:
        args = m.text.split()
        if len(args) < 4: return bot.reply_to(m, "⚠️ Invalid syntax. Example: `/free LUAPAK 10 1d`")
        
        game_id = args[1].lower()
        max_d = int(args[2])
        expiry, label = calculate_expiry(args[3])
        if not expiry: return bot.reply_to(m, "⚠️ Invalid time format.")
        
        key = f"{game_id.upper()}-FREE-{str(uuid.uuid4())[:6].upper()}"
        data = {
            "type": "free",
            "game_id": game_id, 
            "max_devices": max_d, "hwids": [], 
            "expiry": expiry.strftime("%Y-%m-%d %H:%M:%S"), 
            "info": f"Free ({max_d} slots)",
            "is_locked": False,
            "created_at": firestore.SERVER_TIMESTAMP
        }
        db.collection('keys').document(key).set(data)
        bot.reply_to(m, f"🎁 *FREE KEY CREATED {game_id.upper()} ({max_d} SLOTS - {label})*\n\n🔑 *KEY:* `{key}`", parse_mode="Markdown")
    except Exception as e: bot.reply_to(m, f"❌ Error: {e}")

# --- CREATE CUSTOM (ALLOW DUPLICATE KEY IF DIFFERENT GAME) ---
@bot.message_handler(commands=['custom'])
def create_custom(m):
    if not check_admin(m.from_user.id): return
    try:
        args = m.text.split()
        type_k = args[1].lower()
        
        if type_k == 'vip':
            # /custom vip <game> <time> <name>
            game_id = args[2].lower()
            expiry, label = calculate_expiry(args[3])
            user_key_name = args[4].strip()
            
            # Save with ID GAME-KEY to avoid duplicates
            db_id = f"{game_id.upper()}-{user_key_name}"
            
            if db.collection('keys').document(db_id).get().exists: 
                return bot.reply_to(m, f"⚠️ Key `{user_key_name}` for game {game_id} already exists!")
            
            data = {
                "type": "vip",
                "game_id": game_id,
                "hwid": None, 
                "expiry": expiry.strftime("%Y-%m-%d %H:%M:%S"), 
                "info": "Not activated", 
                "is_locked": False
            }
            db.collection('keys').document(db_id).set(data)
            bot.reply_to(m, f"👑 *CUSTOM VIP {game_id.upper()} ({label})*\n\n🔑 *KEY:* `{user_key_name}`\n(System ID: `{db_id}`)", parse_mode="Markdown")
            
        elif type_k == 'free':
            # /custom free <game> <slot> <time> <name>
            game_id = args[2].lower()
            max_d = int(args[3])
            expiry, label = calculate_expiry(args[4])
            user_key_name = args[5].strip()
            
            # Save with ID GAME-KEY
            db_id = f"{game_id.upper()}-{user_key_name}"

            if db.collection('keys').document(db_id).get().exists: 
                return bot.reply_to(m, f"⚠️ Key `{user_key_name}` for game {game_id} already exists!")
            
            data = {
                "type": "free",
                "game_id": game_id,
                "max_devices": max_d, 
                "hwids": [], 
                "expiry": expiry.strftime("%Y-%m-%d %H:%M:%S"), 
                "info": f"Free ({max_d} slots)", 
                "is_locked": False
            }
            db.collection('keys').document(db_id).set(data)
            bot.reply_to(m, f"🎁 *CUSTOM FREE {game_id.upper()} ({max_d} SLOTS - {label})*\n\n🔑 *KEY:* `{user_key_name}`", parse_mode="Markdown")
    except: 
        bot.reply_to(m, "⚠️ Invalid custom syntax.\nVIP: `/custom vip LUAPAK 1d KEYNAME`\nFREE: `/custom free LUAPAK 10 1d KEYNAME`")

# --- LIST KEYS (CATEGORIZED BY GAME) ---
@bot.message_handler(commands=['list'])
def list_keys(m):
    if not check_admin(m.from_user.id): return
    
    docs = db.collection('keys').stream()
    
    # 1. Group Keys by Game ID
    grouped_keys = {} # {'LUAPAKPUBG': [str1, str2], 'LIENQUAN': [...]}
    total_count = 0
    
    for doc in docs:
        total_count += 1
        k_id = doc.id
        v = doc.to_dict()
        game_tag = v.get('game_id', 'OTHER').upper()
        
        is_expired = False
        is_locked = v.get('is_locked', False)
        expiry_str = v.get('expiry', 'N/A')

        try:
            exp = datetime.datetime.strptime(expiry_str, "%Y-%m-%d %H:%M:%S")
            if datetime.datetime.now() > exp: is_expired = True
        except: pass

        if is_locked:
            stt_icon = "🔒 LOCKED"
        elif is_expired:
            stt_icon = "🔴 Expired"
        else:
            if v.get('type') == 'vip':
                if v.get('hwid'):
                    raw_info = v.get('info', '')
                    dev_name = raw_info.replace("Active ", "").strip() if "Active" in raw_info else "Online"
                    stt_icon = f"🟢 {dev_name}"
                else:
                    stt_icon = "⚪ Unused"
            else:
                used = len(v.get('hwids', []))
                mx = v.get('max_devices', 0)
                stt_icon = f"🎁 {used}/{mx} Slots"

        key_line = f"  ▪ `{k_id}`\n    └ {stt_icon} | ⏳ {expiry_str}"
        
        if game_tag not in grouped_keys:
            grouped_keys[game_tag] = []
        grouped_keys[game_tag].append(key_line)

    if total_count == 0:
        bot.reply_to(m, "📭 Database is empty.")
        return

    msg = "📊 *KEY LIST BY GAME*\n"
    sorted_games = sorted(grouped_keys.keys())
    
    for game in sorted_games:
        key_list = grouped_keys[game]
        msg += f"\n➖➖➖➖➖➖➖➖➖➖\n🎮 *{game}* ({len(key_list)} keys)\n"
        for line in key_list:
            msg += line + "\n"

    if len(msg) > 4000:
        part_1 = msg[:4000] + "\n\n⚠️ *List is too long...*"
        bot.reply_to(m, part_1, parse_mode="Markdown")
    else:
        bot.reply_to(m, msg, parse_mode="Markdown")

@bot.message_handler(commands=['delete'])
def delete_key(m):
    if not check_admin(m.from_user.id): return
    try:
        key = m.text.split()[1]
        db.collection('keys').document(key).delete()
        bot.reply_to(m, f"🗑️ Deleted key: `{key}`", parse_mode="Markdown")
    except: pass

@bot.message_handler(commands=['reset'])
def reset_key(m):
    if not check_admin(m.from_user.id): return
    try:
        key = m.text.split()[1]
        ref = db.collection('keys').document(key)
        doc = ref.get()
        if doc.exists:
            dt = doc.to_dict()
            if dt['type'] == 'vip': ref.update({"hwid": None, "info": "Reset"})
            else: ref.update({"hwids": []})
            bot.reply_to(m, f"♻️ Reset: `{key}`", parse_mode="Markdown")
        else:
            bot.reply_to(m, "❌ Key does not exist (Enter correct System ID).")
    except: pass

@bot.message_handler(commands=['lockkey'])
def lock_key_cmd(m):
    if not check_admin(m.from_user.id): return
    try:
        args = m.text.split()
        if len(args) < 2: return bot.reply_to(m, "⚠️ Enter key name!")
        key = args[1]
        ref = db.collection('keys').document(key)
        if ref.get().exists:
            ref.update({"is_locked": True})
            bot.reply_to(m, f"🔒 LOCKED key: `{key}`\n(User will be kicked in 30s)", parse_mode="Markdown")
        else:
            bot.reply_to(m, "❌ Key does not exist.")
    except Exception as e: bot.reply_to(m, f"Error: {e}")

@bot.message_handler(commands=['unlockkey'])
def unlock_key_cmd(m):
    if not check_admin(m.from_user.id): return
    try:
        args = m.text.split()
        if len(args) < 2: return bot.reply_to(m, "⚠️ Enter key name!")
        key = args[1]
        ref = db.collection('keys').document(key)
        if ref.get().exists:
            ref.update({"is_locked": False})
            bot.reply_to(m, f"🔓 UNLOCKED key: `{key}`", parse_mode="Markdown")
        else:
            bot.reply_to(m, "❌ Key does not exist.")
    except Exception as e: bot.reply_to(m, f"Error: {e}")

@bot.message_handler(commands=['blockmodel'])
def block_model_cmd(m):
    if not check_admin(m.from_user.id): return
    try:
        cmd_text = m.text.replace("/blockmodel", "").strip()
        if not cmd_text: return bot.reply_to(m, "⚠️ Enter Model name!")
        ref = db.collection('settings').document('blacklist')
        if not ref.get().exists:
            ref.set({"models": [cmd_text]})
        else:
            ref.update({"models": firestore.ArrayUnion([cmd_text])})
        bot.reply_to(m, f"🚫 Added Model to Blacklist:\n`{cmd_text}`", parse_mode="Markdown")
    except Exception as e: bot.reply_to(m, f"Error: {e}")

@bot.message_handler(commands=['unlockmodel'])
def unlock_model_cmd(m):
    if not check_admin(m.from_user.id): return
    try:
        cmd_text = m.text.replace("/unlockmodel", "").strip()
        if not cmd_text: return bot.reply_to(m, "⚠️ Enter Model name!")
        ref = db.collection('settings').document('blacklist')
        if ref.get().exists:
            ref.update({"models": firestore.ArrayRemove([cmd_text])})
            bot.reply_to(m, f"✅ Unblocked Model:\n`{cmd_text}`", parse_mode="Markdown")
        else:
            bot.reply_to(m, "❌ No block list found.")
    except Exception as e: bot.reply_to(m, f"Error: {e}")

@bot.message_handler(commands=['listblock'])
def list_block_cmd(m):
    if not check_admin(m.from_user.id): return
    try:
        ref = db.collection('settings').document('blacklist').get()
        if not ref.exists: return bot.reply_to(m, "📋 Block list is empty.")
        models = ref.to_dict().get('models', [])
        if not models: return bot.reply_to(m, "📋 Block list is empty.")
        msg = "🚫 **BLOCKED LIST** 🚫\n"
        for mod in models: msg += f"- `{mod}`\n"
        bot.reply_to(m, msg, parse_mode="Markdown")
    except Exception as e: bot.reply_to(m, f"Error: {e}")

# ================= STARTUP =================
def start_services():
    if not any(t.name == "AutoCleanThread" for t in threading.enumerate()):
        t = threading.Thread(target=auto_clean_expired_keys, name="AutoCleanThread", daemon=True)
        t.start()

start_services()

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    server.run(host="0.0.0.0", port=port)