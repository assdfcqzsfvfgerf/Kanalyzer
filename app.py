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
        # Multiply limit by 2 to allow filtering later
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
            timeframe = '1d'  # CCXT uses its own timeframes
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
        self.progress_callback = None

    def set_progress_callback(self, callback):
        self.progress_callback = callback

    def update_progress(self, progress):
        if self.progress_callback:
            self.progress_callback(progress)

    def analyze_symbols(self, lookback_days=270, required_coins=200):
        symbols = self.exchange.get_top_symbols(limit=required_coins)
        results = []
        processed_count = 0
        total_symbols = len(symbols)
        self.update_progress(0)
        with ThreadPoolExecutor(max_workers=self.num_threads) as executor:
            # Process symbols in batches if necessary
            while processed_count < required_coins and symbols:
                batch_size = min(required_coins - processed_count, len(symbols))
                current_batch = symbols[:batch_size]
                symbols = symbols[batch_size:]
                future_to_symbol = {executor.submit(self.process_symbol, symbol, lookback_days): symbol for symbol in current_batch}
                for future in as_completed(future_to_symbol):
                    try:
                        result = future.result()
                    except Exception as e:
                        print(f"Error processing symbol: {e}")
                        result = None
                    if result is not None:
                        results.append(result)
                    processed_count += 1
                    progress = min(100, int((processed_count / total_symbols) * 100))
                    self.update_progress(progress)
                    if len(results) >= required_coins:
                        break
                if not symbols and len(results) < required_coins:
                    print(f"\nWarning: Only found {len(results)} valid coins with {lookback_days} days of data")
                    break
        self.update_progress(100)
        results_df = pd.DataFrame(results)
        if not results_df.empty:
            results_df = results_df.sort_values('percent_diff')
        return results_df

    def process_symbol(self, symbol, lookback_days=270):
        try:
            print(f"Processing {symbol}...")
            df = self.exchange.get_historical_data(symbol, lookback_days=lookback_days)
            if df is None:
                return None
            poc = self.calculate_poc(df)
            if poc is None:
                return None
            current_price = float(df['close'].iloc[-1])
            percent_diff = ((current_price - poc) / poc) * 100
            return {
                'symbol': symbol,
                'current_price': current_price,
                'poc': poc,
                'percent_diff': percent_diff,
                'volume_24h': float(df['volume'].iloc[-1]),
                'price_category': 'High' if current_price >= 100 else 'Mid' if current_price >= 1 else 'Low',
                'days_of_data': len(df)
            }
        except Exception as e:
            print(f"Error analyzing {symbol}: {e}")
            return None

    def get_appropriate_bins(self, price):
        if price >= 100:
            return 175
        elif price >= 1:
            return 85
        else:
            return 40

    def calculate_poc(self, df, num_bins=None):
        if df is None or len(df) == 0:
            return None
        current_price = float(df['close'].iloc[-1])
        if num_bins is None:
            num_bins = self.get_appropriate_bins(current_price)
        price_range = df[['high', 'low']].values.flatten()
        bins = np.linspace(min(price_range), max(price_range), num_bins + 1)
        volume_profile = np.zeros(num_bins)
        for idx, row in df.iterrows():
            low_idx = np.digitize(row['low'], bins) - 1
            high_idx = np.digitize(row['high'], bins) - 1
            volume_per_level = row['volume'] / (high_idx - low_idx + 1) if high_idx >= low_idx else row['volume']
            volume_profile[low_idx:high_idx + 1] += volume_per_level
        poc_idx = np.argmax(volume_profile)
        poc_price = (bins[poc_idx] + bins[poc_idx + 1]) / 2
        return poc_price

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

        def update_progress(progress):
            progress_bar.progress(progress / 100)
            status_text.text(f"Analysis in progress: {progress}% complete...")

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

        # Set up progress callback
        analyzer.set_progress_callback(update_progress)

        start_time = time.time()
        status_text.text("Starting analysis...")
        results = analyzer.analyze_symbols(lookback_days=lookback_days, required_coins=num_coins)
        execution_time = time.time() - start_time
        status_text.text(f"Analysis completed in {execution_time:.2f} seconds")

        if not results.empty:
            st.header("Results")
            display_df = results[
                ['symbol', 'current_price', 'poc', 'percent_diff', 'days_of_data', 'price_category']
            ]
            display_df = display_df.round({
                'current_price': 4,
                'poc': 4,
                'percent_diff': 2
            })

            def highlight_percent_diff(val):
                if isinstance(val, float):
                    color = 'red' if val < 0 else 'green'
                    return f'color: {color}'
                return ''

            styled_df = display_df.style.applymap(
                highlight_percent_diff,
                subset=['percent_diff']
            )

            st.dataframe(styled_df, use_container_width=True)

            csv = display_df.to_csv(index=False)
            st.download_button(
                label="Download results as CSV",
                data=csv,
                file_name=f"crypto_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv"
            )

            st.header("Summary Statistics")
            col1, col2, col3 = st.columns(3)

            with col1:
                st.metric("Average % Difference", f"{results['percent_diff'].mean():.2f}%")
            with col2:
                st.metric("Most Undervalued", f"{results['percent_diff'].min():.2f}%")
            with col3:
                st.metric("Most Overvalued", f"{results['percent_diff'].max():.2f}%")
        else:
            st.error("No results found. Try adjusting the parameters.")
    except BinanceAPIException as e:
        st.error(f"Binance API Error: {str(e)}")
        if "Invalid API-key" in str(e):
            st.info(
                "Please check if your API keys are correct and have the necessary permissions (read-only is sufficient)."
            )
    except Exception as e:
        st.error(f"An error occurred: {str(e)}")
