import logging
import os
import random
import string
import threading
import time
import ccxt
from google import genai
from google.genai import types
import telebot

# Детальне логування в консоль Railway
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(filename)s:%(lineno)d - %(message)s",
)

# --- ЗМІННІ СЕРЕДОВИЩА (Railway Environment Variables) ---
TELEGRAM_TOKEN = os.environ.get(
    "TELEGRAM_TOKEN", "5994037577:AAFjj03rMKr31vhtB-b2zmF1Q1MZdSYRCag"
)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AQ.Ab8RN6JGj-s7PsRCcWKz0uqF-Oxs-5Rlmes132KnRzC2tbsVFg")

ADMIN_ID = int(os.environ.get("ADMIN_ID", 1813590984))
SUBSCRIPTION_PRICE_STARS = int(os.environ.get("SUBSCRIPTION_PRICE_STARS", 500))
# --------------------------------------------------------

bot = telebot.TeleBot(TELEGRAM_TOKEN)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

exchange = ccxt.binance({"enableRateLimit": True})

user_subscriptions = {ADMIN_ID: float("inf")}
user_chat_ids = {ADMIN_ID}
custom_promo_codes = {}
active_signals = {}
last_signals_time = {}

auto_scanner_active = True


def has_active_access(user_id):
  if user_id == ADMIN_ID:
    return True
  expiry = user_subscriptions.get(user_id, 0)
  return time.time() < expiry


def ask_ai_to_analyze(symbol, closes, current_price):
  prompt = f"""
    Ти професійний квантовий трейдер. Проаналізуй останні 40 свічок (годинний таймфрейм) пари {symbol}. 
    ПОТОЧНА РИНКОВА ЦІНА ЗАРАЗ: {current_price}.
    
    ОБОВ'ЯЗКОВО ВИЗНАЧ:
    1. DIRECTION: [LONG або SHORT] (Лонг — ріст, Шорт — падіння).
    2. TRADE_TYPE: [Коротка або Довга] (Коротка — якщо це швидкий скальп/інтрадей з близькими цілями; Довга — якщо це свінг/позиційна угода на довший рух). І Лонг, і Шорт можуть бути як короткими, так і довгими!
    
    Точка входу (ENTRY) повинна бути максимально близькою до поточної ринкової ціни ({current_price}).
    
    Дай відповідь СУВОРО у такому форматі без зайвого тексту:
    DIRECTION: [LONG або SHORT]
    TRADE_TYPE: [Коротка або Довга]
    ENTRY: [точна ціна входу, цифра]
    TP1: [ціна першого тейк-профіту, цифра]
    TP2: [ціна другого тейк-профіту, цифра]
    SL: [ціна стоп-лосу, цифра]
    LEVERAGE: [кредитне плечо, наприклад x5 або x10]
    CHANCE: [вірогідність успіху у відсотках, наприклад 78%]
    REASON: [детальна причина внизу, чому саме такий прогноз]
    """

  for _ in range(3):
    try:
      response = ai_client.models.generate_content(
          model="gemini-3.8-flash",
          contents=prompt,
      )
      return response.text
    except Exception as e:
      if "429" in str(e) or "ResourceExhausted" in str(e):
        logging.warning(
            f"Ліміт запитів Gemini (429) для {symbol}. Пауза 10 секунд..."
        )
        time.sleep(10)
      else:
        logging.error(f"Помилка Gemini API для {symbol}: {e}")
        return None
  return None


def parse_ai_response(ai_text, symbol, current_price):
  try:
    lines = ai_text.strip().split("\n")
    data = {}
    for line in lines:
      if ":" in line:
        key, val = line.split(":", 1)
        data[key.strip().upper()] = val.strip()

    direction = data.get("DIRECTION", "LONG").upper()
    if direction not in ["LONG", "SHORT"]:
      direction = "LONG"

    trade_type = data.get("TRADE_TYPE", "Коротка").capitalize()
    if trade_type not in ["Коротка", "Довга"]:
      trade_type = "Коротка"

    entry = float(data.get("ENTRY", current_price))
    tp1 = float(
        data.get(
            "TP1", current_price * (1.02 if direction == "LONG" else 0.98)
        )
    )
    tp2 = float(
        data.get(
            "TP2", current_price * (1.04 if direction == "LONG" else 0.96)
        )
    )
    sl = float(
        data.get(
            "SL", current_price * (0.98 if direction == "LONG" else 1.02)
        )
    )
    leverage = data.get("LEVERAGE", "x10")
    chance = data.get("CHANCE", "80%")
    reason = data.get("REASON", "Технічний імпульс та аналіз свічок.")

    active_signals[symbol] = {
        "direction": direction,
        "entry": entry,
        "tp1": tp1,
        "tp2": tp2,
        "sl": sl,
    }

    if direction == "LONG":
      dir_text = f"Лонг ({trade_type}) 🟢"
    else:
      dir_text = f"Шорт ({trade_type}) 🔴"

    formatted_msg = (
        f"#{symbol} — {dir_text}\n\n"
        f"📍 Точка входа: `{entry}`\n"
        f"🛑 Стоп-лос: `{sl}`\n\n"
        f"🎯 Цель 1: `{tp1}`\n"
        f"🎯 Цель 2: `{tp2}`\n"
        f"⚡ Кредитное плечо: `{leverage}`\n\n"
        f"📊 **Вірогідність успіху:** `{chance}`\n"
        f"💡 **Чому так (Аналіз):** {reason}\n\n"
        f"⚠️ *Не фінансова рекомендація (NFA)!*"
    )
    return formatted_msg
  except Exception as e:
    logging.error(f"Помилка парсингу відповіді ШІ: {e}")
    return ai_text


def background_ai_scanner():
  global auto_scanner_active
  while True:
    try:
      if not auto_scanner_active:
        time.sleep(10)
        continue

      active_chats = list(user_chat_ids)
      if not active_chats:
        time.sleep(30)
        continue

      logging.info("--- Фоновий сканер ринку перевіряє пари ---")
      exchange.load_markets()
      usdt_symbols = [
          s for s in exchange.symbols if s.endswith("/USDT") and ":" not in s
      ]

      random.shuffle(usdt_symbols)
      scan_list = usdt_symbols[:6]

      for symbol in scan_list:
        if not auto_scanner_active:
          break

        try:
          if time.time() - last_signals_time.get(symbol, 0) < 7200:
            continue

          ohlcv = exchange.fetch_ohlcv(symbol, timeframe="1h", limit=40)
          closes = [candle[4] for candle in ohlcv]
          current_price = closes[-1]

          ai_text = ask_ai_to_analyze(symbol, closes, current_price)
          if ai_text and ("LONG" in ai_text or "SHORT" in ai_text):
            last_signals_time[symbol] = time.time()
            signal_card = parse_ai_response(ai_text, symbol, current_price)

            for chat_id in active_chats:
              try:
                bot.send_message(
                    chat_id,
                    f"🚨 **АВТО-СИГНАЛ РИНКУ** 🚨\n\n{signal_card}",
                    parse_mode="Markdown",
                )
              except Exception as chat_err:
                logging.error(
                    f"Не вдалося надіслати в чат {chat_id}: {chat_err}"
                )

            logging.info(f"Авто-сигнал по {symbol} успішно розіслано!")

          time.sleep(12)
        except Exception:
          continue

    except Exception as e:
      logging.error(f"Помилка у фоновому сканері: {e}")

    time.sleep(240)


# --- ТЕЛЕГРАМ ОБРОБНИКИ ---


@bot.message_handler(commands=["start"])
def send_welcome(message):
  user_id = message.from_user.id
  user_chat_ids.add(message.chat.id)

  if not has_active_access(user_id):
    bot.reply_to(
        message,
        "🔒 **Доступ обмежено!**\n\n"
        "Для користування ботом необхідна активна підписка.\n\n"
        f"💳 Вартість підписки: **{SUBSCRIPTION_PRICE_STARS} ⭐** за 1 день.\n"
        "Натисніть команду `/buy`, щоб придбати підписку, або введіть промокод"
        " через `/promo [код]`.",
        parse_mode="Markdown",
    )
    return

  bot.reply_to(
      message,
      "👋 Вітаю! Ваш доступ до сигналів активний.\n\n"
      "⚙️ **Основні команди:**\n"
      "• `/analyze BTC` — Миттєвий ручний аналіз монети\n"
      "• `/check BTC` — Перевірити відработку сигналу\n"
      "• `/buy` — Подовжити підписку за зірки Telegram\n"
      "• `/promo [код]` — Актувати промокод\n\n"
      "👑 *Адмін-панель:* \n"
      "• `/createpromo [дні]` — Створити промокод\n"
      "• `/autoon` — Увімкнути авто-сигнали\n"
      "• `/autooff` — Вимкнути авто-сигнали",
      parse_mode="Markdown",
  )


@bot.message_handler(commands=["autoon"])
def turn_auto_on(message):
  if message.from_user.id != ADMIN_ID:
    return
  global auto_scanner_active
  auto_scanner_active = True
  bot.reply_to(
      message,
      "🟢 **Автоматичні розсилки сигналів УВІМКНЕНО!**",
      parse_mode="Markdown",
  )


@bot.message_handler(commands=["autooff"])
def turn_auto_off(message):
  if message.from_user.id != ADMIN_ID:
    return
  global auto_scanner_active
  auto_scanner_active = False
  bot.reply_to(
      message,
      "🔴 **Автоматичні розсилки сигналів ВИМКНЕНО!**",
      parse_mode="Markdown",
  )


@bot.message_handler(commands=["buy"])
def buy_subscription(message):
  prices = [
      telebot.types.LabeledPrice(
          label="VIP Підписка на ШІ сигналки (1 день)",
          amount=SUBSCRIPTION_PRICE_STARS,
      )
  ]
  bot.send_invoice(
      chat_id=message.chat.id,
      title="VIP Доступ до сигналів ШІ",
      description="Доступ до точних LONG та SHORT сигналів на 1 день.",
      invoice_payload="vip_sub_1day",
      provider_token="",
      currency="XTR",
      prices=prices,
  )


@bot.pre_checkout_query_handler(func=lambda query: True)
def checkout(pre_checkout_query):
  bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@bot.message_handler(content_types=["successful_payment"])
def got_payment(message):
  user_id = message.from_user.id
  current_expiry = user_subscriptions.get(user_id, time.time())
  base_time = max(current_expiry, time.time())
  user_subscriptions[user_id] = base_time + 86400

  user_chat_ids.add(message.chat.id)
  bot.reply_to(
      message,
      "🎉 **Оплата успішна!** Вашу підписку подовжено на 1 день. Дякуємо!",
      parse_mode="Markdown",
  )


@bot.message_handler(commands=["createpromo"])
def create_promo(message):
  if message.from_user.id != ADMIN_ID:
    bot.reply_to(message, "❌ Ця команда доступна лише адміністратору.")
    return

  args = message.text.split()
  days = 1
  if len(args) > 1 and args[1].isdigit():
    days = int(args[1])

  code = "".join(
      random.choice(string.ascii_uppercase + string.digits) for _ in range(8)
  )
  custom_promo_codes[code] = days

  bot.reply_to(
      message,
      f"✅ **Створено новий промокод!**\n\n"
      f"🔑 Код: `{code}`\n"
      f"⏳ Термін дії підписки: `{days} днів`",
      parse_mode="Markdown",
  )


@bot.message_handler(commands=["promo"])
def use_promo(message):
  user_id = message.from_user.id
  args = message.text.split()

  if len(args) < 2:
    bot.reply_to(
        message,
        "⚠️ Введіть промокод. Приклад: `/promo ABC123XYZ`",
        parse_mode="Markdown",
    )
    return

  code = args[1].upper()

  if code in custom_promo_codes:
    days = custom_promo_codes.pop(code)
    current_expiry = user_subscriptions.get(user_id, time.time())
    base_time = max(current_expiry, time.time())
    user_subscriptions[user_id] = base_time + (days * 86400)
    user_chat_ids.add(message.chat.id)

    bot.reply_to(
        message,
        f"✅ **Промокод активовано!** Доступ надано на `{days}` днів.",
        parse_mode="Markdown",
    )
  else:
    bot.reply_to(
        message,
        "❌ Невірний промокод або він уже був використаний.",
        parse_mode="Markdown",
    )


@bot.message_handler(commands=["analyze", "coin"])
def manual_analyze(message):
  user_id = message.from_user.id
  user_chat_ids.add(message.chat.id)

  if not has_active_access(user_id):
    bot.reply_to(
        message,
        "🔒 У вас немає активної підписки. Придбайте її через `/buy` або введіть"
        " промокод через `/promo [код]`",
        parse_mode="Markdown",
    )
    return

  args = message.text.split()
  if len(args) < 2:
    bot.reply_to(
        message,
        "⚠️ Вкажіть монету. Приклад: `/analyze BTC` або `/analyze ETH`",
        parse_mode="Markdown",
    )
    return

  coin_input = args[1].upper()
  symbol = coin_input if "/" in coin_input else f"{coin_input}/USDT"

  bot.reply_to(
      message,
      f"🔍 Аналізую `{symbol}` через ШІ...",
      parse_mode="Markdown",
  )

  try:
    exchange.load_markets()
    if symbol not in exchange.symbols:
      bot.reply_to(
          message,
          f"❌ Монету `{symbol}` не знайдено на Binance.",
          parse_mode="Markdown",
      )
      return

    ohlcv = exchange.fetch_ohlcv(symbol, timeframe="1h", limit=40)
    closes = [candle[4] for candle in ohlcv]
    current_price = closes[-1]

    ai_text = ask_ai_to_analyze(symbol, closes, current_price)
    if ai_text:
      signal_card = parse_ai_response(ai_text, symbol, current_price)
      bot.send_message(message.chat.id, signal_card, parse_mode="Markdown")
    else:
      bot.reply_to(
          message,
          "⚠️ Тимчасовий ліміт ШІ (429). Спробуйте ще раз через хвилину.",
      )
  except Exception as e:
    bot.reply_to(message, f"❌ Помилка: {e}")


@bot.message_handler(commands=["check"])
def check_signal_status(message):
  args = message.text.split()
  if len(args) < 2:
    bot.reply_to(
        message, "⚠️ Вкажіть монету. Приклад: `/check BTC`", parse_mode="Markdown"
    )
    return

  coin_input = args[1].upper()
  symbol = coin_input if "/" in coin_input else f"{coin_input}/USDT"

  if symbol not in active_signals:
    bot.reply_to(
        message,
        f"❌ Немає збереженого сигналу по `{symbol}`. Зробіть спочатку"
        " `/analyze [монета]`",
        parse_mode="Markdown",
    )
    return

  try:
    ticker = exchange.fetch_ticker(symbol)
    current_price = ticker["last"]
    sig = active_signals[symbol]

    direction = sig["direction"]
    entry = sig["entry"]
    tp1 = sig["tp1"]
    sl = sig["sl"]

    status_text = (
        f"📊 **Перевірка відработки сетапу по {symbol} ({direction})**\n\n"
    )
    status_text += f"💵 Поточна ціна: `{current_price}`\n"
    status_text += f"📍 Точка входу: `{entry}`\n"

    if direction == "LONG":
      if current_price >= tp1:
        status_text += (
            "✅ **Результат:** Тейк-профіт досягнуто! Ціна пішла вгору 🚀"
        )
      elif current_price <= sl:
        status_text += (
            "❌ **Результат:** Ціна пробила стоп-лос вниз. Угода в мінус 🛑"
        )
      else:
        status_text += "⏳ **Результат:** Угода у процесі."
    else:
      if current_price <= tp1:
        status_text += (
            "✅ **Результат:** Тейк-профіт досягнуто! Ціна впала вниз 🚀"
        )
      elif current_price >= sl:
        status_text += (
            "❌ **Результат:** Ціна пробила стоп-лос вгору. Угода в мінус 🛑"
        )
      else:
        status_text += "⏳ **Результат:** Угода у процесі."

    bot.reply_to(message, status_text, parse_mode="Markdown")
  except Exception as e:
    bot.reply_to(message, f"❌ Помилка перевірки ціни: {e}")


if __name__ == "__main__":
  scanner_thread = threading.Thread(target=background_ai_scanner)
  scanner_thread.daemon = True
  scanner_thread.start()

  print("🚀 Бот запущено!")
  logging.info("Бот запущено успішно...")
  bot.infinity_polling()
