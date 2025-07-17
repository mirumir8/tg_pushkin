#!/usr/bin/env python3
# Pushkin bot — main.py (версия с навигацией, статистикой и персонализацией)
#
# CSV: Название;Новый_текст;Координаты (UTF-8, ; delimiter)
# ----------------------------------------------------------------------------
import os, sys, math, csv, sqlite3, textwrap, html, logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List

from fastapi import FastAPI, Request, Response
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
)
from telegram.error import BadRequest as TgBad

# ── базовые настройки логирования ───────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("main")
log.info("Python %s", sys.version.split()[0])

BOT_TOKEN = os.getenv("BOT_TOKEN") or ""
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is missing")

DB              = ".data/poi.sqlite"
RADIUS          = 20           # м для авто-показа
REVISIT_HOURS   = 24          # повтор через … часов
LOCATIONS_FILE  = "locations.csv"

# ── DB helpers ───────────────────────────────────────────────────────────────
def connect_db():
    c = sqlite3.connect(DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.create_function("sqrt", 1, math.sqrt)
    c.create_function("pow",  2, math.pow)
    return c

def init_db():
    os.makedirs(".data", exist_ok=True)
    with connect_db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS poi(
          id INTEGER PRIMARY KEY,
          name_ru   TEXT,
          lat       REAL,
          lon       REAL,
          summary_ru TEXT,
          UNIQUE(name_ru, lat, lon)
        );
        CREATE TABLE IF NOT EXISTS visit_log(
          id INTEGER PRIMARY KEY,
          user_id  INTEGER,
          poi_id   INTEGER,
          visited_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS user_tracking(
            user_id INTEGER PRIMARY KEY,
            last_lat REAL,
            last_lon REAL,
            last_update TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            target_poi_id INTEGER,
            notified_50m BOOLEAN DEFAULT 0,
            notified_arrived BOOLEAN DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS user_stats(
            user_id INTEGER PRIMARY KEY,
            total_distance REAL DEFAULT 0,
            total_time INTEGER DEFAULT 0,
            first_visit DATE,
            last_visit DATE,
            current_streak INTEGER DEFAULT 0,
            max_streak INTEGER DEFAULT 0,
            favorite_poi_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS user_sessions(
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            end_time TIMESTAMP,
            distance REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS user_interests(
            user_id INTEGER,
            interest TEXT,
            PRIMARY KEY (user_id, interest)
        );
        """)

def import_csv() -> int:
    """Импортирует CSV, возвращает кол-во вставленных строк"""
    if not os.path.exists(LOCATIONS_FILE):
        log.warning("%s not found — creating minimal demo file", LOCATIONS_FILE)
        with open(LOCATIONS_FILE, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent("""Название;Новый_текст;Координаты
Кавалерские дома 🏛️;**История:** 1752-1753 годы, архитектор Чевакинский создает эти барочные дома...;59.71618,30.39530
"""))
    inserted = 0
    skipped = 0
    total_rows = 0
    with connect_db() as c:
        cursor = c.cursor()
        with open(LOCATIONS_FILE, newline="", encoding="utf-8") as f:
            rdr = csv.DictReader(f, delimiter=';', quotechar='"')
            for i, row in enumerate(rdr, 1):
                total_rows += 1
                log.debug("Processing row %d: %s", i, row)
                try:
                    lat, lon = map(float, row['Координаты'].split(','))
                    cursor.execute("""
                        INSERT OR IGNORE INTO poi(name_ru, lat, lon, summary_ru)
                        VALUES(?,?,?,?)""",
                                  (row['Название'].strip(), lat, lon, row['Новый_текст'].strip()))
                    if cursor.rowcount > 0:
                        inserted += 1
                        log.info("Imported row %d: %s", i, row['Название'])
                    else:
                        skipped += 1
                        log.warning("Skipped duplicate row %d: %s (lat: %f, lon: %f)", i, row['Название'], lat, lon)
                except Exception as e:
                    log.error("CSV error at row %d: %s → %s", i, row, e)
        log.info("CSV import: processed %s rows, +%s новых точек, %s пропущено", total_rows, inserted, skipped)
    return inserted

# ── геопоиск ────────────────────────────────────────────────────────────────
R_EARTH = 6_371_000

def haversine(lat1, lon1, lat2, lon2):
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    φ1, φ2 = map(math.radians, (lat1, lat2))
    a = math.sin(dφ/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin(dλ/2)**2
    return 2*R_EARTH*math.asin(math.sqrt(a))

def nearest(uid: int, lat: float, lon: float):
    limit = datetime.utcnow() - timedelta(hours=REVISIT_HOURS)
    with connect_db() as c:
        return c.execute("""
        SELECT *, 111000*sqrt(pow(lat-?,2)+pow((lon-?)*0.6,2)) AS dist
        FROM poi WHERE NOT EXISTS(
          SELECT 1 FROM visit_log WHERE user_id=? AND poi_id=poi.id AND visited_at>? )
        AND 111000*sqrt(pow(lat-?,2)+pow((lon-?)*0.6,2)) <= ?
        ORDER BY dist LIMIT 1""", (lat, lon, uid, limit, lat, lon, RADIUS)).fetchone()

def find_nearest_unvisited(uid: int, lat: float, lon: float, limit: int = 3):
    """Находит несколько ближайших непосещенных точек"""
    time_limit = datetime.utcnow() - timedelta(hours=REVISIT_HOURS)
    with connect_db() as c:
        return c.execute("""
        SELECT *, 111000*sqrt(pow(lat-?,2)+pow((lon-?)*0.6,2)) AS dist
        FROM poi WHERE NOT EXISTS(
          SELECT 1 FROM visit_log WHERE user_id=? AND poi_id=poi.id AND visited_at>? )
        ORDER BY dist LIMIT ?""", (lat, lon, uid, time_limit, limit)).fetchall()

def get_poi_by_id(poi_id: int):
    with connect_db() as c:
        return c.execute("SELECT * FROM poi WHERE id=?", (poi_id,)).fetchone()

def mark_visit(uid: int, pid: int):
    with connect_db() as c:
        c.execute("INSERT INTO visit_log(user_id, poi_id) VALUES(?,?)", (uid, pid))
        # Обновляем статистику посещений
        update_visit_stats(uid)

def update_visit_stats(uid: int):
    """Обновляет статистику пользователя после посещения"""
    with connect_db() as c:
        # Инициализируем запись если её нет
        c.execute("INSERT OR IGNORE INTO user_stats(user_id, first_visit) VALUES(?, date('now'))", (uid,))
        c.execute("UPDATE user_stats SET last_visit = date('now') WHERE user_id=?", (uid,))
        
        # Обновляем любимое место
        fav = c.execute("""
            SELECT poi_id, COUNT(*) as cnt FROM visit_log 
            WHERE user_id=? GROUP BY poi_id ORDER BY cnt DESC LIMIT 1
        """, (uid,)).fetchone()
        if fav:
            c.execute("UPDATE user_stats SET favorite_poi_id=? WHERE user_id=?", (fav['poi_id'], uid))

# ── навигация ───────────────────────────────────────────────────────────────
def get_direction(lat1, lon1, lat2, lon2):
    """Возвращает эмодзи направления"""
    dlon = lon2 - lon1
    y = math.sin(math.radians(dlon)) * math.cos(math.radians(lat2))
    x = math.cos(math.radians(lat1)) * math.sin(math.radians(lat2)) - \
        math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(math.radians(dlon))
    bearing = math.degrees(math.atan2(y, x))
    bearing = (bearing + 360) % 360
    
    directions = ["⬆️", "↗️", "➡️", "↘️", "⬇️", "↙️", "⬅️", "↖️"]
    index = round(bearing / 45) % 8
    return directions[index]

def set_navigation_target(uid: int, poi_id: int):
    """Устанавливает цель для навигации"""
    with connect_db() as c:
        c.execute("""
            INSERT OR REPLACE INTO user_tracking(user_id, target_poi_id, notified_50m, notified_arrived)
            VALUES(?, ?, 0, 0)
        """, (uid, poi_id))

def clear_navigation_target(uid: int):
    """Очищает цель навигации"""
    with connect_db() as c:
        c.execute("UPDATE user_tracking SET target_poi_id=NULL, notified_50m=0, notified_arrived=0 WHERE user_id=?", (uid,))

def update_user_position(uid: int, lat: float, lon: float):
    """Обновляет позицию пользователя и считает пройденное расстояние"""
    with connect_db() as c:
        # Инициализируем запись в user_stats если её нет
        c.execute("INSERT OR IGNORE INTO user_stats(user_id, first_visit) VALUES(?, date('now'))", (uid,))
        
        # Получаем последнюю позицию
        last = c.execute(
            "SELECT last_lat, last_lon FROM user_tracking WHERE user_id=?", (uid,)
        ).fetchone()
        
        # Обновляем позицию
        c.execute("""
            INSERT OR REPLACE INTO user_tracking(user_id, last_lat, last_lon, last_update)
            VALUES(?, ?, ?, datetime('now'))
        """, (uid, lat, lon))
        
        # Если есть предыдущая позиция - считаем расстояние
        if last and last['last_lat']:
            dist = haversine(last['last_lat'], last['last_lon'], lat, lon)
            if dist > 5:  # Игнорируем микродвижения
                c.execute(
                    "UPDATE user_stats SET total_distance = total_distance + ? WHERE user_id=?",
                    (dist/1000, uid)  # в километрах
                )

# ── персонализация ──────────────────────────────────────────────────────────
INTERESTS = {
    "history": "📜 История",
    "architecture": "🏛️ Архитектура", 
    "legends": "🔮 Легенды",
    "people": "👤 Великие люди",
    "nature": "🌳 Природа и парки"
}

def get_user_interests(uid: int) -> List[str]:
    """Получает интересы пользователя"""
    with connect_db() as c:
        rows = c.execute("SELECT interest FROM user_interests WHERE user_id=?", (uid,)).fetchall()
        return [r['interest'] for r in rows]

def add_user_interest(uid: int, interest: str):
    """Добавляет интерес пользователя"""
    with connect_db() as c:
        c.execute("INSERT OR IGNORE INTO user_interests(user_id, interest) VALUES(?, ?)", (uid, interest))

def remove_user_interest(uid: int, interest: str):
    """Удаляет интерес пользователя"""
    with connect_db() as c:
        c.execute("DELETE FROM user_interests WHERE user_id=? AND interest=?", (uid, interest))

def get_personalized_description(poi: sqlite3.Row, interests: List[str]) -> str:
    """Адаптирует описание под интересы пользователя"""
    text = poi['summary_ru']
    
    # В реальном боте здесь были бы разные тексты для разных интересов
    # Пока просто добавляем эмодзи-подсказки
    if interests:
        tags = []
        if "history" in interests:
            tags.append("📜")
        if "architecture" in interests:
            tags.append("🏛️")
        if "legends" in interests:
            tags.append("🔮")
        if tags:
            text = " ".join(tags) + " " + text
    
    return text

# ── helpers ─────────────────────────────────────────────────────────────────
html_escape = html.escape

def maps_link(lat, lon):
    return f"https://yandex.ru/maps/?ll={lon},{lat}&z=17&pt={lon},{lat},pm2rdm"

def poi_count():
    with connect_db() as c:
        return c.execute("SELECT COUNT(*) FROM poi").fetchone()[0]

# ── Telegram handlers ───────────────────────────────────────────────────────
WELCOME = textwrap.dedent("""
🏛️ <b>Добро пожаловать в Царское Село!</b>

🔹 Нажмите «📍 Отправить геолокацию» и получите историю ближайшего здания
🔹 Включите Live-локацию для навигации и автоматических рассказов
🔹 Настройте свои интересы командой /interests

📍 <b>Команды:</b>
/stats — ваша статистика
/mystats — подробная статистика 
/interests — настроить интересы
/route — построить маршрут
/reset — начать заново
/reload — перечитать locations.csv (админ)
""")

async def cmd_start(u: Update, _):
    kb = [[KeyboardButton("📍 Отправить геолокацию", request_location=True)]]
    await u.message.reply_text(WELCOME, reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode='HTML')
    
    # Инициализируем статистику
    uid = u.effective_user.id
    with connect_db() as c:
        c.execute("INSERT OR IGNORE INTO user_stats(user_id, first_visit) VALUES(?, date('now'))", (uid,))

# ── статистика и достижения ─────────────────────────────────────────────────
LEVELS = [
    (1,   "🌱 Первооткрыватель"),
    (3,   "🚶‍♀️ Любознательный путник"),
    (5,   "🔍 Исследователь"),
    (10,  "🏛️ Знаток императорского города"),
    (15,  "🎭 Царскосельский хроникёр"),
    (20,  "👑 Хранитель истории")
]

def user_stats(uid: int):
    with connect_db() as c:
        visited = c.execute("SELECT COUNT(DISTINCT poi_id) FROM visit_log WHERE user_id=?", (uid,)).fetchone()[0]
    total = poi_count()
    title = "💫 Гость"  # default
    for n, t in LEVELS:
        if visited >= n:
            title = t
    return visited, total, title

def next_level_info(visited: int):
    """Возвращает инфо о следующем уровне"""
    for n, title in LEVELS:
        if visited < n:
            return n - visited, title
    return 0, None

async def cmd_stats(u: Update, _):
    v, tot, title = user_stats(u.effective_user.id)
    bar = "▓"*v + "░"*(max(tot,1)-v)
    await u.message.reply_text(f"<b>Достижения</b>\n{title}\n\n{v}/{tot} мест\n<code>{bar[:30]}</code>", parse_mode='HTML')

async def cmd_mystats(u: Update, _):
    """Подробная статистика пользователя"""
    uid = u.effective_user.id
    with connect_db() as c:
        stats = c.execute("SELECT * FROM user_stats WHERE user_id=?", (uid,)).fetchone()
        
        if not stats:
            await u.message.reply_text("Вы еще не начали исследование. Отправьте геолокацию!")
            return
        
        visited_count = c.execute("SELECT COUNT(DISTINCT poi_id) FROM visit_log WHERE user_id=?", (uid,)).fetchone()[0]
        total_visits = c.execute("SELECT COUNT(*) FROM visit_log WHERE user_id=?", (uid,)).fetchone()[0]
        
        # Любимое место
        fav = None
        if stats['favorite_poi_id']:
            fav_poi = c.execute("SELECT name_ru FROM poi WHERE id=?", (stats['favorite_poi_id'],)).fetchone()
            if fav_poi:
                fav = fav_poi['name_ru']
    
    text = f"""
📊 <b>Ваша статистика:</b>

🚶 Пройдено: {stats['total_distance']:.1f} км
📍 Мест изучено: {visited_count}
🔄 Всего посещений: {total_visits}
❤️ Любимое место: {fav or 'Пока нет'}

<i>Исследуете с {stats['first_visit']}</i>
"""
    await u.message.reply_text(text, parse_mode='HTML')

# ── навигация и маршруты ────────────────────────────────────────────────────
async def cmd_route(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Предлагает маршрут"""
    uid = u.effective_user.id
    
    # Получаем последнюю известную позицию
    with connect_db() as c:
        track = c.execute("SELECT last_lat, last_lon FROM user_tracking WHERE user_id=?", (uid,)).fetchone()
    
    if not track or not track['last_lat']:
        await u.message.reply_text(
            "📍 Сначала отправьте геолокацию, чтобы я мог построить маршрут!",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("📍 Отправить геолокацию", request_location=True)]], resize_keyboard=True)
        )
        return
    
    # Находим 3 ближайшие непосещенные точки
    pois = find_nearest_unvisited(uid, track['last_lat'], track['last_lon'], limit=3)
    
    if not pois:
        await u.message.reply_text("Вы изучили все места поблизости! Попробуйте /reset для нового путешествия.")
        return
    
    text = "🗺️ <b>Рекомендуемый маршрут:</b>\n\n"
    buttons = []
    
    for i, poi in enumerate(pois, 1):
        dist = round(poi['dist'])
        direction = get_direction(track['last_lat'], track['last_lon'], poi['lat'], poi['lon'])
        text += f"{i}. {poi['name_ru']} {direction} {dist}м\n"
        buttons.append([InlineKeyboardButton(f"{i}. {poi['name_ru']}", callback_data=f"navigate_{poi['id']}")])
    
    text += "\n<i>Выберите место для навигации:</i>"
    
    await u.message.reply_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(buttons))

# ── интересы ────────────────────────────────────────────────────────────────
async def cmd_interests(u: Update, _):
    """Настройка интересов пользователя"""
    uid = u.effective_user.id
    current = get_user_interests(uid)
    
    buttons = []
    for key, name in INTERESTS.items():
        if key in current:
            buttons.append([InlineKeyboardButton(f"✅ {name}", callback_data=f"interest_remove_{key}")])
        else:
            buttons.append([InlineKeyboardButton(name, callback_data=f"interest_add_{key}")])
    
    buttons.append([InlineKeyboardButton("💾 Сохранить", callback_data="interests_done")])
    
    text = "🎯 <b>Выберите ваши интересы:</b>\n\nЯ буду подбирать информацию специально для вас!"
    if current:
        text += "\n\n<i>Активные интересы помечены ✅</i>"
    
    await u.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode='HTML')

# ── обработчики callback ────────────────────────────────────────────────────
async def on_callback(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Обработчик inline кнопок"""
    query = u.callback_query
    
    # Детальное логирование
    log.info(f"=== CALLBACK DEBUG ===")
    log.info(f"User: {query.from_user.id}")
    log.info(f"Data: {query.data}")
    log.info(f"Message ID: {query.message.message_id if query.message else 'No message'}")
    
    try:
        await query.answer()  # Убираем "часики"
    except Exception as e:
        log.error(f"Failed to answer callback: {e}")
        return
    
    uid = query.from_user.id
    data = query.data
    
    try:
        # Показ следующего места
        if data.startswith("show_next_"):
            log.info(f"Processing show_next callback")
            
            try:
                parts = data.split("_")
                log.info(f"Callback parts: {parts}")
                poi_id = int(parts[2])
            except (IndexError, ValueError) as e:
                log.error(f"Failed to parse poi_id from {data}: {e}")
                await query.edit_message_text("❌ Ошибка данных")
                return
            
            log.info(f"Looking for POI with id: {poi_id}")
            poi = get_poi_by_id(poi_id)
            
            if not poi:
                log.error(f"POI {poi_id} not found!")
                await query.edit_message_text("❌ Место не найдено")
                return
                
            # Получаем текущую позицию пользователя
            with connect_db() as c:
                track = c.execute("SELECT last_lat, last_lon FROM user_tracking WHERE user_id=?", (uid,)).fetchone()
            
            if track and track['last_lat']:
                dist = haversine(track['last_lat'], track['last_lon'], poi['lat'], poi['lon'])
            else:
                dist = 0
            
            # Сохраняем старое количество посещений
            visited_before, _, title_before = user_stats(uid)
            
            # Отмечаем посещение
            mark_visit(uid, poi['id'])
            
            # Получаем интересы для персонализации
            interests = get_user_interests(uid)
            description = get_personalized_description(poi, interests)
            
            yandex_link = maps_link(poi['lat'], poi['lon'])
            
            caption = (f"<b>{html_escape(poi['name_ru'])}</b>\n\n"
                       f"{html_escape(description)}\n\n"
                       f"📍 {round(dist)} м | <a href='{yandex_link}'>Карта</a>")
            
            # Ищем следующее место для новой кнопки
            buttons = []
            next_pois = find_nearest_unvisited(uid, poi['lat'], poi['lon'], limit=2)
            if next_pois:
                buttons.append([InlineKeyboardButton("➡️ Следующее место", callback_data=f"show_next_{next_pois[0]['id']}")])
            
            # Добавляем кнопку навигации
            buttons.append([InlineKeyboardButton("🧭 Навести меня туда", callback_data=f"navigate_{poi['id']}")])
            
            keyboard = InlineKeyboardMarkup(buttons) if buttons else None
            
            # Обновляем сообщение
            await query.edit_message_text(caption, parse_mode='HTML', disable_web_page_preview=False, reply_markup=keyboard)
            
            # Проверяем достижения
            visited_after, total, title_after = user_stats(uid)
            
            # Если получили новый уровень
            if title_after != title_before:
                await u.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=f"🎉 <b>Новое достижение!</b>\n\n"
                         f"Вы получили звание:\n{title_after}\n\n"
                         f"Изучено мест: {visited_after}/{total}",
                    parse_mode='HTML'
                )
        
        # Навигация
        elif data.startswith("navigate_"):
            log.info(f"Processing navigate callback")
            poi_id = int(data.split("_")[1])
            set_navigation_target(uid, poi_id)
            poi = get_poi_by_id(poi_id)
            await query.edit_message_text(
                f"🧭 Навигация к <b>{html_escape(poi['name_ru'])}</b> включена!\n\n"
                f"Включите Live-локацию для получения подсказок по маршруту.",
                parse_mode='HTML'
            )
        
        # Интересы
        elif data.startswith("interest_add_"):
            interest = data.split("_")[2]
            add_user_interest(uid, interest)
            await cmd_interests_update(query)
        
        elif data.startswith("interest_remove_"):
            interest = data.split("_")[2]
            remove_user_interest(uid, interest)
            await cmd_interests_update(query)
        
        elif data == "interests_done":
            interests = get_user_interests(uid)
            if interests:
                text = "✅ Интересы сохранены!\n\nВаши интересы: " + ", ".join([INTERESTS[i] for i in interests])
            else:
                text = "✅ Интересы сохранены!\n\nВы не выбрали интересы - буду показывать всю информацию."
            await query.edit_message_text(text)
            
    except Exception as e:
        log.error(f"Error in callback handler: {e}", exc_info=True)
        try:
            await query.answer("❌ Произошла ошибка", show_alert=True)
        except:
            pass
          
async def cmd_interests_update(query):
    """Обновляет сообщение с интересами"""
    uid = query.from_user.id
    current = get_user_interests(uid)
    
    buttons = []
    for key, name in INTERESTS.items():
        if key in current:
            buttons.append([InlineKeyboardButton(f"✅ {name}", callback_data=f"interest_remove_{key}")])
        else:
            buttons.append([InlineKeyboardButton(name, callback_data=f"interest_add_{key}")])
    
    buttons.append([InlineKeyboardButton("💾 Сохранить", callback_data="interests_done")])
    
    text = "🎯 <b>Выберите ваши интересы:</b>\n\nЯ буду подбирать информацию специально для вас!"
    if current:
        text += "\n\n<i>Активные интересы помечены ✅</i>"
    
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode='HTML')

# ── основной обработчик локации ─────────────────────────────────────────────
async def on_location(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Обработчик локации с поддержкой навигации"""
    log.info(f"on_location called for user {u.effective_user.id}")  # Для отладки
    
    m = u.effective_message
    
    # Получаем локацию из разных источников
    loc = None
    if m.location:
        loc = m.location
    elif hasattr(m, 'live_location'):
        loc = m.live_location
    elif u.edited_message and u.edited_message.location:
        loc = u.edited_message.location
        
    if not loc:
        return
    
    uid = u.effective_user.id
    
    # Обновляем позицию и статистику
    update_user_position(uid, loc.latitude, loc.longitude)
    
    # Проверяем, это обновление live location?
    if u.edited_message or (m.edit_date and m.location):
        await handle_live_location(u, ctx, loc)
        return
    
    # Обычная локация - показываем ближайшее место
    await show_nearest_poi(u, ctx, loc)

async def handle_live_location(u: Update, ctx: ContextTypes.DEFAULT_TYPE, loc):
    """Обработка live location для навигации"""
    uid = u.effective_user.id
    message = u.edited_message or u.effective_message  # Используем правильное сообщение
    
    with connect_db() as c:
        track = c.execute("SELECT * FROM user_tracking WHERE user_id=?", (uid,)).fetchone()
    
    if not track or not track['target_poi_id']:
        # Нет цели - предлагаем выбрать
        pois = find_nearest_unvisited(uid, loc.latitude, loc.longitude, limit=3)
        if pois:
            text = "🧭 <b>Выберите место для навигации:</b>\n\n"
            buttons = []
            for poi in pois:
                dist = round(poi['dist'])
                text += f"📍 {poi['name_ru']} - {dist}м\n"
                buttons.append([InlineKeyboardButton(poi['name_ru'], callback_data=f"navigate_{poi['id']}")])
            
            await message.edit_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(buttons))
        return
    
    # Есть цель - показываем навигацию
    poi = get_poi_by_id(track['target_poi_id'])
    dist = haversine(loc.latitude, loc.longitude, poi['lat'], poi['lon'])
    direction = get_direction(loc.latitude, loc.longitude, poi['lat'], poi['lon'])
    
    if dist <= RADIUS:
        # Прибыли!
        if not track['notified_arrived']:
            await show_poi_info(u, poi, dist)
            clear_navigation_target(uid)
            with connect_db() as c:
                c.execute("UPDATE user_tracking SET notified_arrived=1 WHERE user_id=?", (uid,))
    elif dist <= 50:
        # Близко
        if not track['notified_50m']:
            text = f"🎯 Вы у цели!\n\n<b>{poi['name_ru']}</b>\nОсталось: {round(dist)}м {direction}"
            await message.edit_text(text, parse_mode='HTML')
            with connect_db() as c:
                c.execute("UPDATE user_tracking SET notified_50m=1 WHERE user_id=?", (uid,))
    else:
        # Далеко - обновляем направление
        text = f"🧭 <b>{poi['name_ru']}</b>\n\n📍 {round(dist)}м {direction}"
        try:
            await message.edit_text(text, parse_mode='HTML')
        except:
            pass  # Игнорируем ошибки если текст не изменился

async def show_nearest_poi(u: Update, ctx: ContextTypes.DEFAULT_TYPE, loc):
    """Показывает информацию о ближайшей точке"""
    uid = u.effective_user.id
    
    # Сохраняем статистику ДО посещения
    visited_before, total, title_before = user_stats(uid)
    
    p = nearest(uid, loc.latitude, loc.longitude)
    if not p:
        await u.message.reply_text(
            "Рядом нет новых мест. Попробуйте:\n"
            "/route - построить маршрут\n"
            "/reset - начать заново"
        )
        return
    
    await show_poi_info(u, p, haversine(loc.latitude, loc.longitude, p['lat'], p['lon']))
    
    # Проверяем новый уровень
    visited_after, _, title_after = user_stats(uid)
    
    if title_after != title_before:
        # Поздравляем с новым уровнем!
        await u.message.reply_text(
            f"🎉 <b>Новое достижение!</b>\n\n"
            f"Вы получили звание:\n{title_after}\n\n"
            f"Изучено мест: {visited_after}/{total}",
            parse_mode='HTML'
        )
    else:
        # Показываем прогресс
        to_next, next_title = next_level_info(visited_after)
        if to_next > 0:
            await u.message.reply_text(
                f"📊 До уровня «{next_title}» осталось: {to_next} мест",
                parse_mode='HTML'
            )

async def show_poi_info(u: Update, poi: sqlite3.Row, distance: float):
    """Показывает информацию о точке интереса"""
    # Получаем uid правильно - из message или callback
    if u.message:
        uid = u.effective_user.id
        message = u.message
    elif u.callback_query:
        uid = u.callback_query.from_user.id
        message = u.callback_query.message
    else:
        log.error("No message or callback_query in update")
        return
    
    log.info(f"show_poi_info: uid={uid}, poi_id={poi['id']}, poi_name={poi['name_ru']}")
    
    # Получаем интересы для персонализации
    interests = get_user_interests(uid)
    description = get_personalized_description(poi, interests)
    
    dist = round(distance)
    yandex_link = maps_link(poi['lat'], poi['lon'])
    
    caption = (f"<b>{html_escape(poi['name_ru'])}</b>\n\n"
               f"{html_escape(description)}\n\n"
               f"📍 {dist} м | <a href='{yandex_link}'>Карта</a>")
    
    # Кнопка для показа следующего места
    buttons = []
    next_pois = find_nearest_unvisited(uid, poi['lat'], poi['lon'], limit=2)
    
    log.info(f"Found {len(next_pois) if next_pois else 0} next POIs")
    
    if next_pois:
        next_poi = next_pois[0]
        log.info(f"Next POI: id={next_poi['id']}, name={next_poi['name_ru']}")
        buttons.append([InlineKeyboardButton("➡️ Следующее место", callback_data=f"show_next_{next_poi['id']}")])
    
    keyboard = InlineKeyboardMarkup(buttons) if buttons else None
    
    # Отправляем сообщение
    await message.reply_text(caption, parse_mode='HTML', disable_web_page_preview=False, reply_markup=keyboard)
    mark_visit(uid, poi['id'])

# ── админские команды ───────────────────────────────────────────────────────
async def cmd_reset(u: Update, _):
    with connect_db() as c:
        c.execute("DELETE FROM visit_log WHERE user_id=?", (u.effective_user.id,))
        c.execute("DELETE FROM user_tracking WHERE user_id=?", (u.effective_user.id,))
        c.execute("UPDATE user_stats SET total_distance=0 WHERE user_id=?", (u.effective_user.id,))
    await u.message.reply_text("✨ История сброшена. Можно исследовать заново!")

async def cmd_reload(u: Update, _):
    if u.effective_user.id != u.effective_chat.id:
        return
    inserted = import_csv()
    await u.message.reply_text(f"✅ CSV перечитан, добавлено новых точек: {inserted}")

# ── FastAPI / bot lifecycle ────────────────────────────────────────────────
app = FastAPI(title="PushkinBot")
tg_app = None

@app.on_event("startup")
async def startup():
    global tg_app
    init_db()
    import_csv()
    tg_app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # Команды
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("stats", cmd_stats))
    tg_app.add_handler(CommandHandler("mystats", cmd_mystats))
    tg_app.add_handler(CommandHandler("route", cmd_route))
    tg_app.add_handler(CommandHandler("interests", cmd_interests))
    tg_app.add_handler(CommandHandler("reset", cmd_reset))
    tg_app.add_handler(CommandHandler("reload", cmd_reload))
    
    # Обработчики
    tg_app.add_handler(MessageHandler(filters.LOCATION, on_location))
    tg_app.add_handler(CallbackQueryHandler(on_callback))
    
    await tg_app.bot.delete_webhook(drop_pending_updates=True)
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(drop_pending_updates=True)
    log.info("Bot started ✅")

@app.on_event("shutdown")
async def shutdown():
    await tg_app.updater.stop()
    await tg_app.stop()
    await tg_app.shutdown()

@app.get("/")
async def root():
    return {"status": "ok", "total": poi_count(), "version": "2.0"}

@app.post("/webhook")
async def noop(_: Request):
    return Response(status_code=200)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 3000)))