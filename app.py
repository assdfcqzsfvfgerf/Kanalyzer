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
                
            # Calculate 200 EMA
            df['ema_200'] = df['close'].ewm(span=200, adjust=False).mean()
            current_ema = df['ema_200'].iloc[-1]
            
            # Calculate VWAP over the entire period
            df = self.calculate_vwap(df)
            current_vwap = df['vwap'].iloc[-1]
            
            current_price = float(df['close'].iloc[-1])
            percent_diff = ((current_price - poc) / poc) * 100
            ema_percent_diff = ((current_price - current_ema) / current_ema) * 100
            vwap_percent_diff = ((current_price - current_vwap) / current_vwap) * 100

            # Calculate entry price based on strategy
            entry_price, strategy_signal = self.calculate_entry_price(
                current_price, poc, current_ema, current_vwap, 
                percent_diff, ema_percent_diff, vwap_percent_diff
            )
            
            return {
                'symbol': symbol,
                'current_price': current_price,
                'poc': poc,
                'percent_diff': percent_diff,
                'ema_200': current_ema,
                'ema_percent_diff': ema_percent_diff,
                'vwap': current_vwap,
                'vwap_percent_diff': vwap_percent_diff,
                'entry_price': entry_price,
                'signal': strategy_signal,
                'volume_24h': float(df['volume'].iloc[-1]),
                'price_category': 'High' if current_price >= 100 else 'Mid' if current_price >= 1 else 'Low'
            }
        except Exception as e:
            print(f"Error analyzing {symbol}: {e}")
            return None
    def calculate_vwap(self, df):
        """Calculate Volume Weighted Average Price (VWAP)"""
        df['typical_price'] = (df['high'] + df['low'] + df['close']) / 3
        df['volume_price'] = df['typical_price'] * df['volume']
        
        df['cum_volume'] = df['volume'].cumsum()
        df['cum_volume_price'] = df['volume_price'].cumsum()
        
        # Calculate VWAP
        df['vwap'] = df['cum_volume_price'] / df['cum_volume']
        
        return df

    def calculate_entry_price(self, current_price, poc, ema_200, vwap, 
                             poc_diff, ema_diff, vwap_diff):
        """
        Calculate optimal entry price based on POC, EMA 200, and VWAP.
        
        Strategy logic:
        1. If price is below all indicators (POC, EMA 200, VWAP), it's potentially undervalued
           - Entry = Current Price (immediate entry)
        2. If price is below 2 out of 3 indicators, calculate weighted target
           - Entry = Average of the 2 lower indicators
        3. If price is below 1 out of 3 indicators, more conservative entry
           - Entry = The lowest indicator minus small buffer
        4. If price is above all indicators, wait for pullback
           - Entry = Highest of the three indicators
        """
        # Sort indicators from lowest to highest
        indicators = sorted([(poc, "POC"), (ema_200, "EMA"), (vwap, "VWAP")])
        lowest_indicator, lowest_name = indicators[0]
        middle_indicator, middle_name = indicators[1]
        highest_indicator, highest_name = indicators[2]
        
        # Determine how many indicators price is below
        below_count = sum(1 for ind, _ in indicators if current_price < ind)
        
        if below_count == 3:  # Price below all indicators
            # Immediate entry, price is undervalued
            return current_price, "Strong Buy - Price below all indicators"
            
        elif below_count == 2:  # Price below 2 indicators
            # Average of the two lower indicators for entry
            target_entry = (lowest_indicator + middle_indicator) / 2
            return target_entry, f"Buy - Price below {lowest_name} and {middle_name}"
            
        elif below_count == 1:  # Price below 1 indicator
            # Entry at lowest indicator with 1% buffer
            buffer = lowest_indicator * 0.01  # 1% buffer
            target_entry = lowest_indicator - buffer
            return target_entry, f"Cautious Buy - Wait for pullback to {lowest_name}"
            
        else:  # Price above all indicators
            # Wait for pullback to highest indicator
            target_entry = highest_indicator
            return target_entry, "Wait - Price above all indicators"
    
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
st.set_page_config(page_title="Crypto POC Analyzer with VWAP", layout="wide")
st.title("Cryptocurrency Point of Control (POC), EMA 200, and VWAP Analyzer")

# Add exchange selection
st.sidebar.header("Exchange Configuration")
exchange_options = ['cryptocom', 'binance', 'binanceus'] + sorted([ex for ex in ccxt.exchanges if ex != 'cryptocom'])
# Setting cryptocom as default
selected_exchange = st.sidebar.selectbox("Select Exchange", exchange_options, index=0)

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
lookback_days = st.sidebar.slider("Lookback Days", min_value=30, max_value=365, value=270)
lookback_date = today - timedelta(days=lookback_days)
st.sidebar.text(f"Analysis from {lookback_date.strftime('%Y-%m-%d')} to {today.strftime('%Y-%m-%d')}")

# Add date selection via calendar
st.sidebar.subheader("Or select specific start date:")
selected_date = st.sidebar.date_input("Start Date", value=lookback_date, max_value=today - timedelta(days=1))
# Recalculate lookback days if date is selected
if selected_date:
    lookback_days = (today - selected_date).days
    st.sidebar.text(f"Corresponds to {lookback_days} lookback days")

num_coins = st.sidebar.slider("Number of Coins", min_value=10, max_value=200, value=150)
num_threads = st.sidebar.slider("Number of Threads", min_value=1, max_value=8, value=4)

# Interval selection
interval = st.sidebar.radio("Timeframe", options=["1d"], index=0)  # Only daily timeframe option

# Main content
st.write("""
This tool analyzes cryptocurrency prices and calculates the Point of Control (POC), 
200 EMA, and VWAP for each trading pair. It uses these three indicators to suggest optimal 
entry points for spot trading. Only coins with at least 98% of historical data for the 
specified period are included.
""")

st.info("""
**Trading Strategy**: The tool identifies optimal entry points based on the relationship between 
current price, POC, 200 EMA, and VWAP. When price is below all three indicators, it suggests an 
immediate entry. When price is above all indicators, it suggests waiting for a pullback to the 
highest indicator level.
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
        
        results = analyzer.analyze_symbols(lookback_days=lookback_days, required_coins=num_coins)
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
                ['symbol', 'current_price', 'poc', 'percent_diff', 'ema_200', 'ema_percent_diff', 
                 'vwap', 'vwap_percent_diff', 'entry_price', 'signal', 'price_category']
            ]
            display_df = display_df.round({
                'current_price': 4,
                'poc': 4,
                'percent_diff': 2,
                'ema_200': 4,
                'ema_percent_diff': 2,
                'vwap': 4,
                'vwap_percent_diff': 2,
                'entry_price': 4
            })
            
            # Rename columns for better clarity
            display_df = display_df.rename(columns={
                'percent_diff': 'POC Diff %',
                'ema_percent_diff': 'EMA Diff %',
                'vwap_percent_diff': 'VWAP Diff %',
                'ema_200': '200 EMA',
                'entry_price': 'Entry Price'
            })
            
            def highlight_diff(val):
                if isinstance(val, float):
                    color = 'red' if val < 0 else 'green'
                    return f'color: {color}'
                return ''

            def highlight_signal(val):
                if isinstance(val, str):
                    if "Strong Buy" in val:
                        return 'background-color: lightgreen'
                    elif "Buy" in val:
                        return 'background-color: #e6ffe6'  # Very light green
                    elif "Wait" in val:
                        return 'background-color: #ffe6e6'  # Very light red
                return ''

            styled_df = display_df.style.applymap(
                highlight_diff,
                subset=['POC Diff %', 'EMA Diff %', 'VWAP Diff %']
            ).applymap(
                highlight_signal,
                subset=['signal']
            )

            st.dataframe(styled_df, use_container_width=True)

            csv = display_df.to_csv(index=False)
            st.download_button(
                label="Download results as CSV",
                data=csv,
                file_name=f"crypto_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv"
            )

            st.header("Strategy Overview")
            st.write("""
            ### Entry Price Strategy Explanation
            
            The analysis uses a multi-layered strategy to determine optimal entry points based on 3 key indicators:
            
            1. **Point of Control (POC)**: The price level with the highest historical trading volume
            2. **200-day EMA**: Long-term trend indicator
            3. **VWAP (Volume Weighted Average Price)**: Represents the fair value based on both price and volume
            
            #### Entry Strategy Logic:
            
            - **Strong Buy Signal**: When current price is below all three indicators (POC, EMA 200, VWAP), suggesting the asset is potentially undervalued across all metrics. The entry is immediate at current price.
            
            - **Buy Signal**: When price is below two indicators but above one. Entry price is calculated as the average of the two lower indicators, providing a balanced entry point.
            
            - **Cautious Buy Signal**: When price is below only one indicator. Entry is suggested slightly below that indicator (with a 1% buffer) to catch potential pullbacks.
            
            - **Wait Signal**: When price is above all indicators, suggesting the asset may be overvalued. The entry is set at the highest indicator level to wait for a significant pullback.
            
            This strategy aims to provide optimal entries by balancing technical indicators that represent historical trading patterns (POC), long-term trends (EMA 200), and fair value based on recent volume (VWAP).
            """)

            st.header("Summary Statistics")
            col1, col2, col3 = st.columns(3)

            with col1:
                st.subheader("POC Analysis")
                st.metric("Avg POC Diff", f"{results['percent_diff'].mean():.2f}%")
                st.metric("Most Undervalued (POC)", f"{results['percent_diff'].min():.2f}%")
                
            with col2:
                st.subheader("EMA Analysis")
                st.metric("Avg EMA Diff", f"{results['ema_percent_diff'].mean():.2f}%")
                st.metric("Most Undervalued (EMA)", f"{results['ema_percent_diff'].min():.2f}%")
                
            with col3:
                st.subheader("VWAP Analysis")
                st.metric("Avg VWAP Diff", f"{results['vwap_percent_diff'].mean():.2f}%")
                st.metric("Most Undervalued (VWAP)", f"{results['vwap_percent_diff'].min():.2f}%")
                
            # Count signals by type
            signal_counts = results['signal'].str.extract(r'(Strong Buy|Buy|Cautious Buy|Wait)')[0].value_counts()
            
            st.subheader("Signal Distribution")
            st.bar_chart(signal_counts)

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
