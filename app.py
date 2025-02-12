import streamlit as st
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time
from binance.client import Client as BinanceClient
from binance.exceptions import BinanceAPIException
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import ccxt

class ExchangeFactory:
    @staticmethod
    def create_exchange(exchange_name, api_key=None, api_secret=None):
        if exchange_name == 'binance':
            return BinanceAdapter(api_key, api_secret)
        elif exchange_name == 'binanceus':
            return BinanceUSAdapter(api_key, api_secret)
        elif exchange_name in ccxt.exchanges:
            return CCXTAdapter(exchange_name, api_key, api_secret)
        raise ValueError(f"Unsupported exchange: {exchange_name}")

class ExchangeAdapter:
    def __init__(self, api_key=None, api_secret=None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_lock = threading.Lock()

    def get_top_symbols(self, limit=200):
        raise NotImplementedError

    def get_historical_data(self, symbol, interval='1d', lookback_days=270):
        raise NotImplementedError

class BinanceAdapter(ExchangeAdapter):
    def __init__(self, api_key=None, api_secret=None):
        super().__init__(api_key, api_secret)
        self.client = BinanceClient(api_key, api_secret)
        self.base_currency = 'USDT'

    def get_top_symbols(self, limit=200):
        with self.api_lock:
            tickers = self.client.get_ticker()
            time.sleep(0.5)

        usdt_pairs = [t for t in tickers if t['symbol'].endswith(self.base_currency)]
        sorted_pairs = sorted(usdt_pairs, key=lambda x: float(x['quoteVolume']), reverse=True)
        return [pair['symbol'] for pair in sorted_pairs[:limit * 2]]

    def get_historical_data(self, symbol, interval='1d', lookback_days=270):
        try:
            start_time = int((datetime.now() - timedelta(days=lookback_days)).timestamp() * 1000)

            with self.api_lock:
                klines = self.client.get_klines(
                    symbol=symbol,
                    interval=interval,
                    startTime=start_time,
                    limit=1000
                )
                time.sleep(0.5)

            if len(klines) < lookback_days * 0.95:
                return None

            df = pd.DataFrame(klines, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_volume', 'trades', 'taker_buy_base',
                'taker_buy_quote', 'ignored'
            ])

            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = df[col].astype(float)

            return df

        except Exception as e:
            print(f"Error fetching data for {symbol}: {e}")
            return None

class BinanceUSAdapter(BinanceAdapter):
    def __init__(self, api_key=None, api_secret=None):
        super().__init__(api_key, api_secret)
        self.client = BinanceClient(api_key, api_secret, tld='us')

class CCXTAdapter(ExchangeAdapter):
    def __init__(self, exchange_name, api_key=None, api_secret=None):
        super().__init__(api_key, api_secret)
        exchange_class = getattr(ccxt, exchange_name)
        self.exchange = exchange_class({
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True
        })
        self.base_currency = 'USDT'

    def get_top_symbols(self, limit=200):
        with self.api_lock:
            tickers = self.exchange.fetch_tickers()
            time.sleep(0.5)

        usdt_pairs = [symbol for symbol in tickers.keys() if symbol.endswith(self.base_currency)]
        sorted_pairs = sorted(
            usdt_pairs,
            key=lambda x: float(tickers[x]['quoteVolume'] or 0),
            reverse=True
        )
        return sorted_pairs[:limit * 2]

    def get_historical_data(self, symbol, interval='1d', lookback_days=270):
        try:
            timeframe = '1d'  # CCXT uses different interval format
            since = int((datetime.now() - timedelta(days=lookback_days)).timestamp() * 1000)

            with self.api_lock:
                ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, since, limit=1000)
                time.sleep(0.5)

            if len(ohlcv) < lookback_days * 0.95:
                return None

            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            return df

        except Exception as e:
            print(f"Error fetching data for {symbol}: {e}")
            return None

class CryptoAnalyzer:
    def __init__(self, exchange_adapter, num_threads=4):
        self.exchange = exchange_adapter
        self.num_threads = num_threads

    # ... Rest of your CryptoAnalyzer class remains the same ...
    # (Copy all methods from your original CryptoAnalyzer class here)

# Streamlit interface
st.set_page_config(page_title="Crypto POC Analyzer", layout="wide")

st.title("Cryptocurrency Point of Control (POC) Analyzer")

# Add exchange selection
st.sidebar.header("Exchange Configuration")
exchange_options = ['binance', 'binanceus'] + sorted(ccxt.exchanges)
selected_exchange = st.sidebar.selectbox("Select Exchange", exchange_options)

# API Configuration
st.sidebar.header("API Configuration")
api_key = st.sidebar.text_input("API Key", type="password")
api_secret = st.sidebar.text_input("API Secret", type="password")

# Save configuration in session state
if 'api_configured' not in st.session_state:
    st.session_state.api_configured = False

if api_key and api_secret:
    st.session_state.api_configured = True
    st.session_state.api_key = api_key
    st.session_state.api_secret = api_secret

# Analysis parameters
st.sidebar.header("Analysis Parameters")
lookback_days = st.sidebar.slider("Lookback Days", min_value=30, max_value=365, value=270)
num_coins = st.sidebar.slider("Number of Coins", min_value=10, max_value=200, value=50)
num_threads = st.sidebar.slider("Number of Threads", min_value=1, max_value=8, value=4)

# Main content
st.write("""
This tool analyzes cryptocurrency prices and calculates the Point of Control (POC) 
for each trading pair. It only includes coins with complete historical data for the specified period.
""")

# Run analysis
if st.button("Run Analysis"):
    try:
        progress_bar = st.progress(0)
        status_text = st.empty()

        # Create exchange adapter
        exchange_adapter = ExchangeFactory.create_exchange(
            selected_exchange,
            api_key=st.session_state.api_key if st.session_state.api_configured else None,
            api_secret=st.session_state.api_secret if st.session_state.api_configured else None
        )

        # Initialize analyzer
        analyzer = CryptoAnalyzer(
            exchange_adapter=exchange_adapter,
            num_threads=num_threads
        )

        # ... Rest of your Streamlit interface code remains the same ...
        # (Copy the analysis and display code from your original app here)

    except Exception as e:
        st.error(f"An error occurred: {str(e)}")
