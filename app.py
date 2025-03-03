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

    def get_historical_data(self, symbol, interval='1h', lookback_days=270):
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

    def get_historical_data(self, symbol, interval='1h', lookback_days=270):
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
            
            # For 1h timeframe, we need to be more lenient with data requirements
            # With 24 hours per day, we need approximately 24 * lookback_days candles
            # But let's be more lenient and require at least 22 hours per day
            min_required_candles = lookback_days * 22
            
            if len(klines) < min_required_candles:
                print(f"Not enough data for {symbol}: {len(klines)} candles, required {min_required_candles}")
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
            'enableRateLimit': True,
            # Add timeout to prevent hanging
            'timeout': 30000,
            # Add retry count
            'retry_count': 3
        })
        # For cryptocom specifically
        if exchange_name == 'cryptocom':
            self.base_currency = '/USDT'
        else:
            self.base_currency = 'USDT'

    def get_top_symbols(self, limit=200):
        try:
            with self.api_lock:
                # Use fetch_markets which is often more reliable than fetch_tickers
                markets = self.exchange.fetch_markets()
                time.sleep(1)  # Increase delay to avoid rate limits
                
            # Handle different exchange formats
            if isinstance(self.base_currency, str) and self.base_currency.startswith('/'):
                # Format like /USDT (e.g., for cryptocom)
                usdt_pairs = [m['symbol'] for m in markets if m['symbol'].endswith(self.base_currency)]
            else:
                # Format like USDT (e.g., for binance)
                usdt_pairs = [m['symbol'] for m in markets if m['quote'] == self.base_currency]
            
            # If we couldn't find pairs with the above method, try a more general approach
            if not usdt_pairs:
                usdt_pairs = [m['symbol'] for m in markets if 'USDT' in m['symbol']]
            
            if not usdt_pairs:
                raise ValueError(f"No USDT pairs found for {self.exchange.id}")
                
            # Try to get volume data for sorting
            try:
                with self.api_lock:
                    tickers = self.exchange.fetch_tickers(usdt_pairs[:min(50, len(usdt_pairs))])
                    time.sleep(1)
                
                # Sort by volume if available
                sorted_pairs = sorted(
                    [symbol for symbol in tickers.keys()],
                    key=lambda x: float(tickers[x].get('quoteVolume', 0) or 0),
                    reverse=True
                )
            except Exception as e:
                print(f"Error fetching tickers: {e}, using unsorted pairs")
                sorted_pairs = usdt_pairs
                
            return sorted_pairs[:limit]
        except Exception as e:
            print(f"Error in get_top_symbols: {e}")
            # Return some default common pairs as fallback
            return ["BTC/USDT", "ETH/USDT", "XRP/USDT", "SOL/USDT", "ADA/USDT"]

    def get_historical_data(self, symbol, interval='1h', lookback_days=270):
        try:
            timeframe = '1h'  # Using 1h timeframe as requested
            since = int((datetime.now() - timedelta(days=lookback_days)).timestamp() * 1000)
            
            # Some exchanges have limits on how many candles can be fetched at once
            # Let's fetch data in chunks to ensure we get all the data
            all_ohlcv = []
            current_since = since
            max_retries = 3
            
            while True:
                retries = 0
                chunk_data = None
                
                # Retry mechanism for each chunk
                while retries < max_retries and chunk_data is None:
                    try:
                        with self.api_lock:
                            chunk_data = self.exchange.fetch_ohlcv(
                                symbol, timeframe, current_since, limit=1000
                            )
                            time.sleep(1)  # Increased delay
                    except Exception as e:
                        retries += 1
                        print(f"Retry {retries}/{max_retries} for {symbol}: {e}")
                        time.sleep(2)  # Wait before retry
                
                if chunk_data is None:
                    print(f"Failed to fetch data for {symbol} after {max_retries} retries")
                    return None
                
                if not chunk_data:
                    break
                    
                all_ohlcv.extend(chunk_data)
                
                # If we got fewer candles than requested, we've reached the end
                if len(chunk_data) < 1000:
                    break
                    
                # Update since for the next chunk (use the timestamp of the last candle + 1ms)
                current_since = chunk_data[-1][0] + 1
                
                # Safety check - if we've fetched too many candles already, break
                if len(all_ohlcv) > lookback_days * 24 * 2:  # twice the expected number
                    break
            
            # For 1h timeframe, we need to be more lenient with data requirements
            # Be more forgiving with hourly data - require only 8 hours per day on average
            min_required_candles = lookback_days * 8
            
            if len(all_ohlcv) < min_required_candles:
                print(f"Not enough data for {symbol}: {len(all_ohlcv)} candles, required {min_required_candles}")
                return None
                
            df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            
            # Sort by timestamp to ensure chronological order
            df = df.sort_values('timestamp')
            
            # Drop duplicates if any
            df = df.drop_duplicates(subset=['timestamp'])
            
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

    def analyze_symbols(self, lookback_days=270, required_coins=200, interval='1h'):
        symbols = self.exchange.get_top_symbols(limit=required_coins * 2)  # Get more symbols to account for filtering
        if not symbols:
            raise ValueError("No symbols returned from exchange")
            
        print(f"Top {len(symbols)} symbols: {symbols[:10]}...")
        
        results = []
        processed_count = 0
        total_symbols = len(symbols)
        self.update_progress(0)
        
        with ThreadPoolExecutor(max_workers=self.num_threads) as executor:
            while processed_count < total_symbols and len(results) < required_coins:
                batch_size = min(required_coins - len(results), len(symbols) - processed_count)
                if batch_size <= 0:
                    break
                    
                current_batch = symbols[processed_count:processed_count + batch_size]
                future_to_symbol = {executor.submit(self.process_symbol, symbol, lookback_days, interval): symbol for symbol in current_batch}
                
                for future in as_completed(future_to_symbol):
                    symbol = future_to_symbol[future]
                    try:
                        result = future.result()
                    except Exception as e:
                        print(f"Error processing {symbol}: {e}")
                        result = None
                        
                    processed_count += 1
                    progress = min(100, int((processed_count / total_symbols) * 100))
                    self.update_progress(progress)
                    
                    if result is not None:
                        results.append(result)
                        print(f"Successfully processed {symbol} ({len(results)}/{required_coins})")
                        
                    if len(results) >= required_coins:
                        break
                        
        self.update_progress(100)
        
        if not results:
            print("No valid results found")
            return pd.DataFrame()
            
        results_df = pd.DataFrame(results)
        if not results_df.empty:
            results_df = results_df.sort_values('percent_diff')
            
        return results_df

    def process_symbol(self, symbol, lookback_days=270, interval='1h'):
        try:
            print(f"Processing {symbol}...")
            df = self.exchange.get_historical_data(symbol, interval=interval, lookback_days=lookback_days)
            
            if df is None or len(df) < 200:  # Need at least 200 candles for 200 EMA
                return None
                
            # Calculate POC
            poc = self.calculate_poc(df)
            if poc is None:
                return None
                
            # Calculate 200 EMA
            df['ema_200'] = df['close'].ewm(span=200, adjust=False).mean()
            current_ema = df['ema_200'].iloc[-1]
            
            current_price = float(df['close'].iloc[-1])
            percent_diff = ((current_price - poc) / poc) * 100
            ema_percent_diff = ((current_price - current_ema) / current_ema) * 100
            
            return {
                'symbol': symbol,
                'current_price': current_price,
                'poc': poc,
                'percent_diff': percent_diff,
                'ema_200': current_ema,
                'ema_percent_diff': ema_percent_diff,
                'volume_24h': float(df['volume'].iloc[-1]),
                'price_category': 'High' if current_price >= 100 else 'Mid' if current_price >= 1 else 'Low'
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
# Ensure cryptocom is actually in the list (some ccxt versions might not have it)
available_exchanges = ccxt.exchanges
if 'cryptocom' not in available_exchanges:
    exchange_options = ['binance', 'binanceus', 'kucoin'] + sorted([ex for ex in available_exchanges 
                                                                  if ex not in ['binance', 'binanceus', 'kucoin']])
    default_index = 0  # Default to binance if cryptocom is not available
else:
    exchange_options = ['cryptocom', 'binance', 'binanceus'] + sorted([ex for ex in available_exchanges 
                                                                     if ex not in ['cryptocom', 'binance', 'binanceus']])
    default_index = 0  # cryptocom is first

selected_exchange = st.sidebar.selectbox("Select Exchange", exchange_options, index=default_index)

# Add debug toggle
debug_mode = st.sidebar.checkbox("Enable Debug Mode", value=False)

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

# Calculate and display the date we're going back to
today = datetime.now().date()
lookback_days = st.sidebar.slider("Lookback Days", min_value=7, max_value=365, value=90)  # Reduced default
lookback_date = today - timedelta(days=lookback_days)
st.sidebar.text(f"Analysis from {lookback_date.strftime('%Y-%m-%d')} to {today.strftime('%Y-%m-%d')}")

# Add date selection via calendar
st.sidebar.subheader("Or select specific start date:")
selected_date = st.sidebar.date_input("Start Date", value=lookback_date, max_value=today - timedelta(days=1))
# Recalculate lookback days if date is selected
if selected_date:
    lookback_days = (today - selected_date).days
    st.sidebar.text(f"Corresponds to {lookback_days} lookback days")

num_coins = st.sidebar.slider("Number of Coins", min_value=5, max_value=100, value=20)  # Reduced defaults
num_threads = st.sidebar.slider("Number of Threads", min_value=1, max_value=8, value=2)  # Reduced default

# Main content
st.write("""
This tool analyzes cryptocurrency prices and calculates the Point of Control (POC) 
for each trading pair using 1-hour timeframe data. It also calculates the 200 EMA 
for comparison. Only coins with complete historical data for the specified period are included.
""")

st.info("""
**Tip**: Start with a smaller lookback period (7-30 days) and fewer coins (5-10) to test if 
the exchange API is working properly. If you get "No results found", try changing the exchange
or reducing the lookback period further.
""")

# Run analysis
if st.button("Run Analysis"):
    try:
        progress_bar = st.progress(0)
        status_text = st.empty()
        debug_info = st.empty()

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
        status_text.text(f"Starting analysis with {selected_exchange} exchange...")
        
        # Use 1h timeframe for analysis
        results = analyzer.analyze_symbols(lookback_days=lookback_days, required_coins=num_coins, interval='1h')
        execution_time = time.time() - start_time
        status_text.text(f"Analysis completed in {execution_time:.2f} seconds")

        if debug_mode:
            debug_info.code("Debug information is enabled. Check your terminal/console for detailed logs.")

        if results.empty:
            st.error("""
            No results found. Please try the following:
            1. Reduce the lookback period (try 7-14 days)
            2. Reduce the number of coins (try 5-10)
            3. Try a different exchange (Binance or KuCoin often work well)
            4. Check if your API keys are correct (if using)
            """)
        else:
            st.success(f"Successfully analyzed {len(results)} cryptocurrencies!")
            st.header("Results")
            display_df = results[
                ['symbol', 'current_price', 'poc', 'percent_diff', 'ema_200', 'ema_percent_diff', 'price_category']
            ]
            display_df = display_df.round({
                'current_price': 4,
                'poc': 4,
                'percent_diff': 2,
                'ema_200': 4,
                'ema_percent_diff': 2
            })
            
            # Rename columns for better clarity
            display_df = display_df.rename(columns={
                'percent_diff': 'POC Diff %',
                'ema_percent_diff': 'EMA Diff %',
                'ema_200': '200 EMA'
            })

            def highlight_diff(val):
                if isinstance(val, float):
                    color = 'red' if val < 0 else 'green'
                    return f'color: {color}'
                return ''

            styled_df = display_df.style.applymap(
                highlight_diff,
                subset=['POC Diff %', 'EMA Diff %']
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
            col1, col2, col3, col4 = st.columns(4)

            with col1:
                st.metric("Avg POC Diff", f"{results['percent_diff'].mean():.2f}%")
            with col2:
                st.metric("Most Undervalued (POC)", f"{results['percent_diff'].min():.2f}%")
            with col3:
                st.metric("Avg EMA Diff", f"{results['ema_percent_diff'].mean():.2f}%")
            with col4:
                st.metric("Most Undervalued (EMA)", f"{results['ema_percent_diff'].min():.2f}%")
    except BinanceAPIException as e:
        st.error(f"Binance API Error: {str(e)}")
        if "Invalid API-key" in str(e):
            st.info(
                "Please check if your API keys are correct and have the necessary permissions (read-only is sufficient)."
            )
    except Exception as e:
        st.error(f"An error occurred: {str(e)}")
        if debug_mode:
            st.exception(e)
