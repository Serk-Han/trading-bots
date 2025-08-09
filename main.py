import asyncio
import pandas as pd
import numpy as np
from binance import AsyncClient
from telegram import Bot
from telegram.ext import Application, CommandHandler
from ta.momentum import RSIIndicator
from ta.volatility import BollingerBands
from ta.volatility import AverageTrueRange
import ta.volume
import math
import logging
import time
import os
from fastapi import FastAPI
import uvicorn

# FastAPI endpoint
app = FastAPI()

@app.get("/")
async def root():
    return {"status": "Bot is running"}

# Binance Futures ve Telegram ayarları
API_KEY = os.getenv("API_KEY", "your_default_api_key")
API_SECRET = os.getenv("API_SECRET", "your_default_api_secret")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "7747350733:AAExXm8sTc7JNq3L0eR5Hv9oo-LXWVof2aM")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1433612470")
SYMBOLS = ["SUIUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "AVAXUSDT", "DOGEUSDT", "ADAUSDT", "XRPUSDT", "XLMUSDT", "UNIUSDT"]
LEVERAGE = 10
TRADE_FEE = 0.0004
RISK_PER_TRADE = 0.02
INITIAL_BALANCE = 100.0
STOP_LOSS_PCT = 0.005
TAKE_PROFIT_PCT = 0.01
INTERVAL = "3m"
VOLUME_THRESHOLD = 1.3
CHOP_THRESHOLD = 50.0
RSI_LOWER = 35
RSI_UPPER = 65
BB_TOLERANCE = 0.01
MAX_RETRIES = 3
RETRY_DELAY = 10

# Sanal cüzdan ve durum takibi
balance = INITIAL_BALANCE
trade_history = []
open_positions = {}
is_real_mode = False
symbol_precision = {}
symbol_locks = {symbol: asyncio.Lock() for symbol in SYMBOLS}

# Log ayarları
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Telegram bildirimi gönder
async def send_telegram_message(message):
    bot = Bot(token=TELEGRAM_TOKEN)
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
        logger.info(f"Telegram mesajı gönderildi: {message}")
    except Exception as e:
        logger.error(f"Telegram mesajı gönderilemedi: {e}")

# Trade history CSV’yi Telegram’a gönder
async def send_trade_history():
    while True:
        try:
            with open("trade_history.csv", "rb") as file:
                bot = Bot(token=TELEGRAM_TOKEN)
                await bot.send_document(chat_id=TELEGRAM_CHAT_ID, document=file)
                logger.info("Trade history CSV Telegram’a gönderildi")
        except Exception as e:
            logger.error(f"Trade history gönderilemedi: {e}")
        await asyncio.sleep(86400)

# Bot durum bildirimi
async def send_status_update():
    while True:
        total_profit = sum(trade['net_profit'] for trade in trade_history) if trade_history else 0
        open_position_count = len(open_positions)
        total_trades = len(trade_history)
        mode = "Gerçek" if is_real_mode else "Sanal"
        message = (f"🤖 Bot Durum Güncellemesi\n"
                   f"Mod: {mode}\n"
                   f"Sanal Bakiye: {balance:.2f} USDT\n"
                   f"Toplam Kâr/Zarar: {total_profit:.2f} USDT\n"
                   f"Açık Pozisyon Sayısı: {open_position_count}\n"
                   f"Toplam İşlem Sayısı: {total_trades}\n"
                   f"Semboller: {', '.join(SYMBOLS)}")
        await send_telegram_message(message)
        await asyncio.sleep(3600)

# Telegram komutları
async def mode_sanal(update, context):
    global is_real_mode, open_positions
    is_real_mode = False
    open_positions.clear()
    await send_telegram_message("✅ Sanal işlem moduna geçildi. Açık pozisyonlar sıfırlandı.")
    logger.info("Sanal işlem moduna geçildi.")

async def mode_gercek(update, context):
    global is_real_mode, open_positions
    is_real_mode = True
    open_positions.clear()
    await send_telegram_message("✅ Gerçek işlem moduna geçildi. Açık pozisyonlar sıfırlandı. Dikkatli olun!")
    logger.info("Gerçek işlem moduna geçildi.")

# Sembol hassasiyetlerini çek ve geçerli sembolleri döndür
async def get_symbol_precision(client):
    try:
        exchange_info = await client.futures_exchange_info()
        valid_symbols = []
        for symbol_info in exchange_info['symbols']:
            if symbol_info['symbol'] in SYMBOLS:
                symbol_precision[symbol_info['symbol']] = symbol_info['quantityPrecision']
                valid_symbols.append(symbol_info['symbol'])
        logger.info(f"Sembol hassasiyetleri: {symbol_precision}")
        logger.info(f"Geçerli semboller: {valid_symbols}")
        return valid_symbols
    except Exception as e:
        logger.error(f"Hassasiyet bilgisi alınamadı: {e}")
        symbol_precision.update({symbol: 2 for symbol in SYMBOLS})
        return SYMBOLS

# Açık pozisyonları kontrol et
async def check_open_position(client, symbol):
    if not is_real_mode:
        return open_positions.get(symbol)
    try:
        positions = await client.futures_position_information(symbol=symbol)
        for pos in positions:
            if pos['symbol'] == symbol and float(pos['positionAmt']) != 0:
                return {
                    'entry_price': float(pos['entryPrice']),
                    'position_size': abs(float(pos['positionAmt'])),
                    'side': 'LONG' if float(pos['positionAmt']) > 0 else 'SHORT'
                }
        return None
    except Exception as e:
        logger.error(f"{symbol} pozisyon kontrol hatası: {e}")
        return open_positions.get(symbol)

# Yeterli margin kontrolü
async def check_margin_balance(client):
    try:
        account = await client.futures_account()
        available_balance = float(account['availableBalance'])
        return available_balance >= 10.0
    except Exception as e:
        logger.error(f"Bakiye kontrol hatası: {e}")
        return False

# Kline verileri
async def fetch_kline_data(client, symbol, interval, limit=100):
    for attempt in range(MAX_RETRIES):
        try:
            klines = await client.futures_klines(symbol=symbol, interval=interval, limit=limit)
            df = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume',
                                              'close_time', 'quote_asset_volume', 'trades',
                                              'taker_buy_base', 'taker_buy_quote', 'ignored'])
            if df.empty:
                logger.error(f"{symbol} için veri boş, tekrar deneniyor...")
                await asyncio.sleep(RETRY_DELAY)
                continue
            return df
        except Exception as e:
            logger.error(f"{symbol} kline verisi alınamadı (deneme {attempt + 1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY)
    logger.error(f"{symbol} için veri alınamadı, tüm denemeler başarısız.")
    await send_telegram_message(f"⚠️ {symbol} için veri alınamadı, işlem durduruldu.")
    return None

# RSI, Bollinger, Hacim ve CHOP hesaplama
def calculate_indicators(df):
    df['close'] = df['close'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    df['volume'] = df['volume'].astype(float)
    rsi = RSIIndicator(df['close'], window=14).rsi()
    bb = BollingerBands(df['close'], window=20, window_dev=2)
    atr = AverageTrueRange(df['high'], df['low'], df['close'], window=1).average_true_range()
    df['rsi'] = rsi
    df['bb_lower'] = bb.bollinger_lband()
    df['bb_upper'] = bb.bollinger_hband()
    df['volume_avg'] = df['volume'].rolling(window=14).mean()
    length = 14
    sum_atr = atr.rolling(window=length).sum()
    high_low_range = df['high'].rolling(window=length).max() - df['low'].rolling(window=length).min()
    df['chop'] = 100 * np.log10(sum_atr / high_low_range) / np.log10(length)
    return df

# Pozisyon büyüklüğünü hesapla ve yuvarla
def calculate_position_size(entry_price, balance, risk_per_trade, stop_loss_pct, leverage, symbol):
    risk_amount = balance * risk_per_trade
    price_diff = entry_price * stop_loss_pct
    position_size = risk_amount / (price_diff * leverage)
    position_size = max(position_size, 0.01)
    if symbol in symbol_precision:
        precision = symbol_precision[symbol]
        position_size = round(position_size, precision)
    return position_size

# Scalping stratejisi
async def scalping_strategy(client, symbol, df, position):
    global balance, trade_history, open_positions
    async with symbol_locks[symbol]:
        latest = df.iloc[-1]
        price = latest['close']
        rsi = latest['rsi']
        bb_lower = latest['bb_lower']
        bb_upper = latest['bb_upper']
        volume = latest['volume']
        volume_avg = latest['volume_avg']
        chop = latest['chop']

        api_position = await check_open_position(client, symbol)
        if api_position and not position:
            open_positions[symbol] = api_position
            position = api_position

        if position:
            if ((position['side'] == 'LONG' and rsi > RSI_UPPER and price >= bb_upper * (1 - BB_TOLERANCE)) or
                (position['side'] == 'SHORT' and rsi < RSI_LOWER and price <= bb_lower * (1 + BB_TOLERANCE))) and \
               volume >= volume_avg * VOLUME_THRESHOLD and chop > CHOP_THRESHOLD:
                exit_price = price
                position_size = abs(float((await check_open_position(client, symbol))['position_size'])) if is_real_mode else position['position_size']
                if position['side'] == 'LONG':
                    profit = (exit_price - position['entry_price']) * position_size * LEVERAGE
                    fee = (position['entry_price'] + exit_price) * position_size * TRADE_FEE * LEVERAGE
                    net_profit = profit - fee
                    if is_real_mode:
                        try:
                            await client.futures_create_order(symbol=symbol, side='SELL', type='MARKET', quantity=position_size)
                            account = await client.futures_account()
                            balance = float(account['totalWalletBalance'])
                        except Exception as e:
                            logger.error(f"{symbol} Long kapatma hatası: {e}")
                            await send_telegram_message(f"⚠️ {symbol} Long kapatma hatası: {e}")
                            return position
                    else:
                        balance += net_profit
                    trade_history.append({
                        'symbol': symbol,
                        'entry_price': position['entry_price'],
                        'exit_price': exit_price,
                        'position_size': position_size,
                        'net_profit': net_profit,
                        'balance': float(balance),
                        'side': 'LONG',
                        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
                    })
                    pd.DataFrame(trade_history).to_csv("trade_history.csv", index=False)
                    message = (f"📉 {symbol} LONG Kapatıldı\n"
                               f"Giriş: {position['entry_price']:.2f}\n"
                               f"Çıkış: {exit_price:.2f}\n"
                               f"Pozisyon Büyüklüğü: {position_size:.4f}\n"
                               f"Net Kâr/Zarar: {net_profit:.2f}\n"
                               f"Sanal Bakiye: {balance:.2f} USDT")
                    await send_telegram_message(message)
                    logger.info(f"{symbol} Long Kapatıldı: {exit_price}, Net Kâr: {net_profit}, Bakiye: {balance}")
                else:
                    profit = (position['entry_price'] - exit_price) * position_size * LEVERAGE
                    fee = (position['entry_price'] + exit_price) * position_size * TRADE_FEE * LEVERAGE
                    net_profit = profit - fee
                    if is_real_mode:
                        try:
                            await client.futures_create_order(symbol=symbol, side='BUY', type='MARKET', quantity=position_size)
                            account = await client.futures_account()
                            balance = float(account['totalWalletBalance'])
                        except Exception as e:
                            logger.error(f"{symbol} Short kapatma hatası: {e}")
                            await send_telegram_message(f"⚠️ {symbol} Short kapatma hatası: {e}")
                            return position
                    else:
                        balance += net_profit
                    trade_history.append({
                        'symbol': symbol,
                        'entry_price': position['entry_price'],
                        'exit_price': exit_price,
                        'position_size': position_size,
                        'net_profit': net_profit,
                        'balance': float(balance),
                        'side': 'SHORT',
                        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
                    })
                    pd.DataFrame(trade_history).to_csv("trade_history.csv", index=False)
                    message = (f"📈 {symbol} SHORT Kapatıldı\n"
                               f"Giriş: {position['entry_price']:.2f}\n"
                               f"Çıkış: {exit_price:.2f}\n"
                               f"Pozisyon Büyüklüğü: {position_size:.4f}\n"
                               f"Net Kâr/Zarar: {net_profit:.2f}\n"
                               f"Sanal Bakiye: {balance:.2f} USDT")
                    await send_telegram_message(message)
                    logger.info(f"{symbol} Short Kapatıldı: {exit_price}, Net Kâr: {net_profit}, Bakiye: {balance}")
                del open_positions[symbol]
                return None

            if (position['side'] == 'LONG' and (price >= position['take_profit'] or price <= position['stop_loss'])) or \
               (position['side'] == 'SHORT' and (price <= position['take_profit'] or price >= position['stop_loss'])):
                exit_price = price
                position_size = abs(float((await check_open_position(client, symbol))['position_size'])) if is_real_mode else position['position_size']
                if position['side'] == 'LONG':
                    profit = (exit_price - position['entry_price']) * position_size * LEVERAGE
                    fee = (position['entry_price'] + exit_price) * position_size * TRADE_FEE * LEVERAGE
                    net_profit = profit - fee
                    if is_real_mode:
                        try:
                            await client.futures_create_order(symbol=symbol, side='SELL', type='MARKET', quantity=position_size)
                            account = await client.futures_account()
                            balance = float(account['totalWalletBalance'])
                        except Exception as e:
                            logger.error(f"{symbol} Long kapatma hatası: {e}")
                            await send_telegram_message(f"⚠️ {symbol} Long kapatma hatası: {e}")
                            return position
                    else:
                        balance += net_profit
                    trade_history.append({
                        'symbol': symbol,
                        'entry_price': position['entry_price'],
                        'exit_price': exit_price,
                        'position_size': position_size,
                        'net_profit': net_profit,
                        'balance': float(balance),
                        'side': 'LONG',
                        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
                    })
                    pd.DataFrame(trade_history).to_csv("trade_history.csv", index=False)
                    message = (f"📉 {symbol} LONG Kapatıldı\n"
                               f"Giriş: {position['entry_price']:.2f}\n"
                               f"Çıkış: {exit_price:.2f}\n"
                               f"Pozisyon Büyüklüğü: {position_size:.4f}\n"
                               f"Net Kâr/Zarar: {net_profit:.2f}\n"
                               f"Sanal Bakiye: {balance:.2f} USDT")
                    await send_telegram_message(message)
                    logger.info(f"{symbol} Long Kapatıldı: {exit_price}, Net Kâr: {net_profit}, Bakiye: {balance}")
                else:
                    profit = (position['entry_price'] - exit_price) * position_size * LEVERAGE
                    fee = (position['entry_price'] + exit_price) * position_size * TRADE_FEE * LEVERAGE
                    net_profit = profit - fee
                    if is_real_mode:
                        try:
                            await client.futures_create_order(symbol=symbol, side='BUY', type='MARKET', quantity=position_size)
                            account = await client.futures_account()
                            balance = float(account['totalWalletBalance'])
                        except Exception as e:
                            logger.error(f"{symbol} Short kapatma hatası: {e}")
                            await send_telegram_message(f"⚠️ {symbol} Short kapatma hatası: {e}")
                            return position
                    else:
                        balance += net_profit
                    trade_history.append({
                        'symbol': symbol,
                        'entry_price': position['entry_price'],
                        'exit_price': exit_price,
                        'position_size': position_size,
                        'net_profit': net_profit,
                        'balance': float(balance),
                        'side': 'SHORT',
                        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
                    })
                    pd.DataFrame(trade_history).to_csv("trade_history.csv", index=False)
                    message = (f"📈 {symbol} SHORT Kapatıldı\n"
                               f"Giriş: {position['entry_price']:.2f}\n"
                               f"Çıkış: {exit_price:.2f}\n"
                               f"Pozisyon Büyüklüğü: {position_size:.4f}\n"
                               f"Net Kâr/Zarar: {net_profit:.2f}\n"
                               f"Sanal Bakiye: {balance:.2f} USDT")
                    await send_telegram_message(message)
                    logger.info(f"{symbol} Short Kapatıldı: {exit_price}, Net Kâr: {net_profit}, Bakiye: {balance}")
                del open_positions[symbol]
                return None
            return position

        if is_real_mode and not await check_margin_balance(client):
            logger.error(f"{symbol} için yeterli margin yok.")
            await send_telegram_message(f"⚠️ {symbol} için yeterli margin yok. İşlem açılmadı.")
            return None

        if is_real_mode and await check_open_position(client, symbol):
            logger.info(f"{symbol} için zaten açık pozisyon var, yeni işlem açılmadı.")
            return None

        if rsi < RSI_LOWER and price <= bb_lower * (1 + BB_TOLERANCE) and \
           volume >= volume_avg * VOLUME_THRESHOLD and chop > CHOP_THRESHOLD:
            entry_price = price
            stop_loss = entry_price * (1 - STOP_LOSS_PCT)
            take_profit = entry_price * (1 + TAKE_PROFIT_PCT)
            position_size = calculate_position_size(entry_price, balance, RISK_PER_TRADE, STOP_LOSS_PCT, LEVERAGE, symbol)
            fee = entry_price * position_size * TRADE_FEE * LEVERAGE
            if is_real_mode:
                try:
                    await client.futures_create_order(symbol=symbol, side='BUY', type='MARKET', quantity=position_size)
                    account = await client.futures_account()
                    balance = float(account['totalWalletBalance'])
                except Exception as e:
                    logger.error(f"{symbol} Long emir hatası: {e}")
                    await send_telegram_message(f"⚠️ {symbol} Long emir hatası: {e}")
                    return None
            message = (f"📈 {symbol} LONG Sinyali (10x)\n"
                       f"Fiyat: {entry_price:.2f}\n"
                       f"Pozisyon Büyüklüğü: {position_size:.4f}\n"
                       f"Stop-Loss: {stop_loss:.2f}\n"
                       f"Take-Profit: {take_profit:.2f}\n"
                       f"İşlem Ücreti: {fee:.2f}\n"
                       f"Hacim: {volume:.2f} (Ort: {volume_avg:.2f})\n"
                       f"CHOP: {chop:.2f}\n"
                       f"Sanal Bakiye: {balance:.2f} USDT")
            await send_telegram_message(message)
            logger.info(f"{symbol} Long: {entry_price}, Pozisyon: {position_size}, Hacim: {volume}, CHOP: {chop}")
            open_positions[symbol] = {'entry_price': entry_price, 'stop_loss': stop_loss,
                                     'take_profit': take_profit, 'side': 'LONG',
                                     'position_size': position_size}
            return open_positions[symbol]
        elif rsi > RSI_UPPER and price >= bb_upper * (1 - BB_TOLERANCE) and \
             volume >= volume_avg * VOLUME_THRESHOLD and chop > CHOP_THRESHOLD:
            entry_price = price
            stop_loss = entry_price * (1 + STOP_LOSS_PCT)
            take_profit = entry_price * (1 - TAKE_PROFIT_PCT)
            position_size = calculate_position_size(entry_price, balance, RISK_PER_TRADE, STOP_LOSS_PCT, LEVERAGE, symbol)
            fee = entry_price * position_size * TRADE_FEE * LEVERAGE
            if is_real_mode:
                try:
                    await client.futures_create_order(symbol=symbol, side='SELL', type='MARKET', quantity=position_size)
                    account = await client.futures_account()
                    balance = float(account['totalWalletBalance'])
                except Exception as e:
                    logger.error(f"{symbol} Short emir hatası: {e}")
                    await send_telegram_message(f"⚠️ {symbol} Short emir hatası: {e}")
                    return None
            message = (f"📉 {symbol} SHORT Sinyali (10x)\n"
                       f"Fiyat: {entry_price:.2f}\n"
                       f"Pozisyon Büyüklüğü: {position_size:.4f}\n"
                       f"Stop-Loss: {stop_loss:.2f}\n"
                       f"Take-Profit: {take_profit:.2f}\n"
                       f"İşlem Ücreti: {fee:.2f}\n"
                       f"Hacim: {volume:.2f} (Ort: {volume_avg:.2f})\n"
                       f"CHOP: {chop:.2f}\n"
                       f"Sanal Bakiye: {balance:.2f} USDT")
            await send_telegram_message(message)
            logger.info(f"{symbol} Short: {entry_price}, Pozisyon: {position_size}, Hacim: {volume}, CHOP: {chop}")
            open_positions[symbol] = {'entry_price': entry_price, 'stop_loss': stop_loss,
                                     'take_profit': take_profit, 'side': 'SHORT',
                                     'position_size': position_size}
            return open_positions[symbol]
        return None

# Kaldıraç ayarı
async def set_leverage(client, symbol):
    try:
        await client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
        logger.info(f"{symbol} için {LEVERAGE}x kaldıraç ayarlandı")
    except Exception as e:
        logger.error(f"{symbol} kaldıraç ayarı başarısız: {e}. Binance arayüzünden manuel ayar yapın.")

# Sembol için veri işleme
async def process_symbol(client, symbol):
    await set_leverage(client, symbol)
    position = None
    while True:
        try:
            df = await fetch_kline_data(client, symbol, INTERVAL)
            if df is None or df.empty:
                logger.error(f"{symbol} için veri alınamadı, tekrar deneniyor...")
                await asyncio.sleep(RETRY_DELAY)
                continue
            df = calculate_indicators(df)
            position = await scalping_strategy(client, symbol, df, position)
            await asyncio.sleep(10)
        except Exception as e:
            logger.error(f"{symbol} işlem hatası: {e}")
            await asyncio.sleep(RETRY_DELAY)

# Ana fonksiyon
async def main():
    global SYMBOLS
    client = None
    try:
        client = await AsyncClient.create(API_KEY, API_SECRET, testnet=False)
        SYMBOLS = await get_symbol_precision(client)  # Geçerli sembolleri güncelle
        app = Application.builder().token(TELEGRAM_TOKEN).build()
        app.add_handler(CommandHandler("mode_sanal", mode_sanal))
        app.add_handler(CommandHandler("mode_gercek", mode_gercek))
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        message = (f"🤖 Bot Başladı\n"
                   f"Mod: {'Sanal' if not is_real_mode else 'Gerçek'}\n"
                   f"Sanal Bakiye: {balance:.2f} USDT\n"
                   f"Risk Oranı: {RISK_PER_TRADE*100:.1f}%\n"
                   f"Kaldıraç: {LEVERAGE}x\n"
                   f"Semboller: {', '.join(SYMBOLS)}")
        await send_telegram_message(message)
        status_task = asyncio.create_task(send_status_update())
        csv_task = asyncio.create_task(send_trade_history())
        tasks = [process_symbol(client, symbol) for symbol in SYMBOLS]
        tasks.append(status_task)
        tasks.append(csv_task)
        await asyncio.gather(*tasks)
    except Exception as e:
        logger.error(f"Main hata: {e}")
    finally:
        if client:
            await client.close_connection()
            logger.info("Client bağlantısı kapatıldı")

# Render için asenkron çalıştırma
if __name__ == "__main__":
    asyncio.create_task(main())
    uvicorn.run(app, host="0.0.0.0", port=10000)
