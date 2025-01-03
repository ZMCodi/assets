import yfinance as yf
import pandas as pd
import numpy as np
import psycopg as pg
import matplotlib.pyplot as plt
import seaborn as sns
import plotly.express as px
from datetime import datetime
import kaleido
import mplfinance as mpf
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from scipy import stats
from config import DB_CONFIG

class Asset():

    def __init__(self, ticker):
        
        self.ticker = ticker
        self.get_data()

    def get_data(self):
        with pg.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT date, open, high, low, close, adj_close, volume FROM daily WHERE ticker = %s", (self.ticker,))
                if cur.rowcount == 0:
                    self.insert_new_ticker()
                    cur.execute("SELECT date, open, high, low, close, adj_close, volume FROM daily WHERE ticker = %s", (self.ticker,))

                data = cur.fetchall()

                self.daily = pd.DataFrame(data, columns=['date', 'open', 'high', 'low', 'close', 'adj_close', 'volume']).set_index('date')
                self.daily = self.daily.astype(float)
                self.daily.index = pd.to_datetime(self.daily.index)
                self.daily = self.daily.sort_index()
                self.daily['log_rets'] = np.log(self.daily['adj_close'] / self.daily['adj_close'].shift(1))
                self.daily['rets'] = self.daily['adj_close'].pct_change()

                cur.execute("SELECT date, open, high, low, close, adj_close, volume FROM five_minute WHERE ticker = %s", (self.ticker,))
                self.five_minute = pd.DataFrame(cur.fetchall(), columns=['date', 'open', 'high', 'low', 'close', 'adj_close', 'volume']).set_index('date')
                self.five_minute = self.five_minute.astype(float)
                self.five_minute.index = pd.to_datetime(self.five_minute.index)
                self.five_minute = self.five_minute.sort_index()
                self.five_minute['log_rets'] = np.log(self.five_minute['adj_close'] / self.five_minute['adj_close'].shift(1))
                self.five_minute['rets'] = self.five_minute['adj_close'].pct_change()

                cur.execute("SELECT asset_type FROM tickers WHERE ticker = %s", (self.ticker,))
                self.asset_type = cur.fetchone()[0]

                cur.execute("SELECT currency FROM tickers WHERE ticker = %s", (self.ticker,))
                self.currency = cur.fetchone()[0]
    
    def insert_new_ticker(self):
        print(f"{self.ticker} is not yet available in database. Downloading from yfinance...")

        # Ticker table data
        ticker = yf.Ticker(self.ticker)

        # Check valid ticker
        if ticker.history().empty:
            print(f'{self.ticker} is an invalid yfinance ticker')
            return

        comp_name = ticker.info['shortName'].replace("'", "''")
        exchange = ticker.info['exchange']
        currency = ticker.info['currency'].upper()
        start_date = pd.to_datetime('today').date()
        asset_type = ticker.info['quoteType'].lower()

        # Mapping dictionary
        exchange_mapping = {
            'NYQ': 'NYSE',
            'NMS': 'NASDAQ',
            'NGM': 'NASDAQ'
        }

        # Mapped list
        exchange = exchange_mapping.get(exchange, exchange)

        try:
            market_cap = ticker.info['marketCap']
        except KeyError:
            market_cap = None

        try:
            sector = ticker.info['sector']
        except KeyError:
            sector = None

        print(f'Inserting to DB {ticker=}, {comp_name=}, {exchange=}, {currency=}, {asset_type=}, {market_cap=}, {sector=}')
            
        # Get daily data from 2020
        daily_data = yf.download(self.ticker, start='2020-01-01')
        daily_data = daily_data.droplevel(1, axis=1)
        daily_data['ticker'] = self.ticker
        clean_daily = self.clean_data(daily_data)
        clean_daily = clean_daily.rename(columns={'Date': 'date', 'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume'})
        if 'Adj Close' not in clean_daily.columns:
            clean_daily['adj_close'] = clean_daily['close']
        else:
            clean_daily = clean_daily.rename(columns={'Adj Close': 'adj_close'})
        # print(f'{clean_daily.count()=}')

        # Get 5min data
        five_min_data = yf.download(self.ticker, interval='5m')
        five_min_data = five_min_data.droplevel(1, axis=1)
        five_min_data['ticker'] = self.ticker
        clean_five_min = self.clean_data(five_min_data)
        clean_five_min = clean_five_min.rename(columns={'Datetime': 'date', 'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume'})
        if 'Adj Close' not in clean_five_min.columns:
            clean_five_min['adj_close'] = clean_five_min['close']
        else:
            clean_five_min = clean_five_min.rename(columns={'Adj Close': 'adj_close'})
        # print(f'{clean_five_min.count()=}')

        # Insert to database
        with pg.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT DISTINCT LEFT(currency_pair, 3) FROM daily_forex;')
                currencies = [cur[0] for cur in cur.fetchall()]

                # Add if new currency
                if currency not in currencies:
                    self.add_new_currency(cur, conn, currencies, currency)

                cur.execute("INSERT INTO tickers (ticker, comp_name, exchange, sector, market_cap, start_date, currency, asset_type) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", (self.ticker, comp_name, exchange, sector, market_cap, start_date, currency, asset_type))

                BATCH_SIZE = 1000
                batch_count = 0
                rows_inserted = 0

                # insert daily data
                for _, row in clean_daily.iterrows():
                    cur.execute("INSERT INTO daily (ticker, date, open, high, low, close, adj_close, volume) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", (row['ticker'], row['date'], row['open'], row['high'], row['low'], row['close'], row['adj_close'], row['volume']))
                    batch_count += 1
                    rows_inserted += 1

                    if batch_count >= BATCH_SIZE:
                        conn.commit()
                        batch_count = 0
                
                if batch_count > 0:
                    conn.commit()
                    batch_count = 0
                
                print(f'daily_{rows_inserted=}')
                rows_inserted = 0

                # insert 5min data
                for _, row in clean_five_min.iterrows():
                    cur.execute("INSERT INTO five_minute (ticker, date, open, high, low, close, adj_close, volume) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", (row['ticker'], row['date'], row['open'], row['high'], row['low'], row['close'], row['adj_close'], row['volume']))
                    batch_count += 1
                    rows_inserted += 1

                    if batch_count >= BATCH_SIZE:
                        conn.commit()
                        batch_count = 0
                
                if batch_count > 0:
                    conn.commit()
                    batch_count = 0
                
                print(f'five_min_{rows_inserted=}')


    def clean_data(self, df):
        mask = (df['High'] < df['Open']) | (df['High'] < df['Close']) | (df['Low'] > df['Open']) | (df['Low'] > df['Close'])
        clean = df[~mask].copy()
        temp = df[mask].copy()

        temp['High'] = temp[['Open', 'Close', 'High']].max(axis=1)
        temp['Low'] = temp[['Open', 'Close', 'Low']].min(axis=1)
        clean = pd.concat([clean, temp], axis=0)
        clean = clean.reset_index()

        return clean
        
    def add_new_currency(self, cur, conn, currencies, currency):

        new_forex = []
        forex_ticker = []
        for curr in currencies:
            new_forex.append(f'{curr}{currency}=X')
            new_forex.append(f'{currency}{curr}=X')

        # get new forex data from yfinance
        df_list = []
        for pair in new_forex:
            data = yf.download(pair, start='2020-01-01')
            data = data.droplevel(1, axis=1)
            data['currency_pair'] = f'{pair[:3]}/{pair[3:6]}'
            df_list.append(data)

        df = pd.concat(df_list)
        clean = self.clean_data(df)
        clean.drop(columns=['Adj Close', 'Volume'], inplace=True)
        clean = clean.rename(columns={'Date': 'date', 'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close'})

        BATCH_SIZE = 1000
        batch_count = 0
        for _, row in clean.iterrows():
            cur.execute("INSERT INTO daily_forex (currency_pair, date, open, high, low, close) VALUES (%s, %s, %s, %s, %s, %s)", (row['currency_pair'], row['date'], row['open'], row['high'], row['low'], row['close']))
            batch_count += 1

            if batch_count >= BATCH_SIZE:
                conn.commit()
                batch_count = 0

        if batch_count > 0:
            conn.commit()

    def plot_price_history(self, *, timeframe='1d', start_date=None, end_date=None, resample=None, 
                           interactive=True, line=None, filename=None, fig=None, subplot_idx=None):
        
        data = self.daily['close'] if timeframe == '1d' else self.five_minute['close']

        if start_date is not None:
            data = data[data.index >= start_date]
        if end_date is not None:
            data = data[data.index <= end_date]
        if resample is not None:
            data = data.resample(resample).last()

        data = data.dropna()
        
        if not interactive:
            # Set the style and figure size
            plt.style.use('seaborn-v0_8')  # Clean, modern look

            if fig is None:
                fig, ax = plt.subplots(figsize=(12, 6))  # Wider aspect ratio
            else:
                ax = fig.axes[subplot_idx] if subplot_idx is not None else fig.gca()

            # Create the plot with customizations
            sns.lineplot(data=data.to_frame(), x=data.index, y=data, ax=ax,
                        color='#1f77b4',  # Professional blue color
                        linewidth=2)      # Slightly thicker line

            # Customize the title and labels with better typography
            ax.set_title(f'{self.ticker} Price History', 
                        fontsize=16, 
                        pad=20,
                        fontweight='bold')
            ax.set_xlabel('Year', fontsize=12)
            ax.set_ylabel(f'Price ({self.currency})', fontsize=12)

            ax.grid(color='gray', linestyle='--', linewidth=0.5, alpha=0.7)

            def format_prices(x, p):
                if x >= 1e3:
                    return f'{x/1e3:.1f}K'
                else:
                    return f'{x:.1f}'

            ax.yaxis.set_major_formatter(plt.FuncFormatter(format_prices))

            # Customize the spines
            ax.spines['top'].set_visible(False)
            ax.spines['right'].set_visible(False)

            ax.tick_params(labelsize=10)
            plt.xticks(rotation=0)

            # plt.tight_layout()

            ax.set_facecolor('#f8f9fa')
            fig.patch.set_facecolor('white')

            if line is not None:
                ax.axhline(line, color='r', linestyle='--')

            if filename is not None:
                fig.savefig(filename, dpi=300, bbox_inches='tight', transparent=(True if filename.endswith('.png') else False))
        else:

            standalone = False
            
            if fig is None:
                fig = go.Figure()
                standalone = True
                
            # Add trace to the figure
            fig.add_trace(
                go.Scatter(
                    x=data.index,
                    y=data,
                    name=f'{self.ticker} Price',
                    connectgaps=True
                ),
                row=subplot_idx[0] if subplot_idx else None,
                col=subplot_idx[1] if subplot_idx else None
            )

            # Only update layout if it's a standalone plot
            if standalone:
                fig.update_layout(
                    title=f'{self.ticker} Price History',
                    xaxis_title='Date',
                    yaxis_title=f'Price ({self.currency})'
                )

                if line is not None:
                    fig.add_hline(y=line,
                        line_dash="dash",
                        line_color="red",
                    )
                fig.show()
            else:
                fig.update_yaxes(
                    title_text=f'Price ({self.currency})', 
                    row=subplot_idx[0] if subplot_idx else None,
                    col=subplot_idx[1] if subplot_idx else None
                )
                fig.update_xaxes(
                    title_text=f'{self.ticker} Price History', 
                    row=subplot_idx[0] if subplot_idx else None,
                    col=subplot_idx[1] if subplot_idx else None
                )

            if filename is not None:
                fig.write_image(filename)

        return fig


    def plot_candlestick(self, *, start_date=None, end_date=None, timeframe='1d', 
                         interactive=True, volume=True, resample=None,
                         filename=None, fig=None, candle_idx=None, vol_idx=None):

        if resample is not None:
            data = self.resample(period=resample, five_min=True if timeframe != '1d' else False)
        else:
            data = self.daily if timeframe == '1d' else self.five_minute

        if end_date is not None:
            data = data[data.index <= end_date]
        
        if start_date is not None:
            data = data[data.index >= start_date]
        else:
            if timeframe == '1d':
                days_back = 365
            else:
                if self.asset_type == 'cryptocurrency':
                    days_back = 1
                else:
                    days_back = 3
            data = data[data.index >= data.index[-1] - pd.Timedelta(days=days_back)]

        data = data.dropna()

        if not interactive:
            # Create figure with white background
            plt.style.use('seaborn-v0_8')  # Reset to light style

            if fig is None:
                standalone = True
                if volume:
                    fig, (ax1, ax2) = plt.subplots(2, figsize=(12, 8), height_ratios=(3, 1))
                else:
                    fig, ax1 = plt.subplots(figsize=(12, 6))
            else:
                standalone = False
                if volume:
                    ax1 = fig.axes[candle_idx] if candle_idx is not None else fig.gca()
                    ax2 = fig.axes[vol_idx] if vol_idx is not None else fig.axes[candle_idx + 1]
                else:
                    ax1 = fig.axes[candle_idx] if candle_idx is not None else fig.gca()

            fig.patch.set_facecolor('white')
            ax1.set_facecolor('white')

            mc = mpf.make_marketcolors(up='#26A69A',
                                    down='#EF5350', 
                                    edge='inherit',
                                    volume='inherit')
            s = mpf.make_mpf_style(marketcolors=mc,
                                gridstyle=':',
                                gridcolor='#E0E0E0',
                                y_on_right=False)

            mpf.plot(data,
                    type='candle',
                    style=s,
                    ax=ax1,
                    datetime_format='%Y-%m-%d')

            ax1.grid(True, linestyle=':', color='#E0E0E0', alpha=0.6)

            # Add professional title with custom font
            ax1.set_title(f"{self.ticker} Candlestick Chart{' with Volume Bars' if volume else ''}", 
                        pad=20, 
                        fontsize=14, 
                        fontweight='bold',
                        fontfamily='sans-serif')
            
            # Refine x-axis formatting
            n_labels = 6 if standalone else 3
            step = len(data) // n_labels

            # Clean up candlestick axis
            ax1.spines['top'].set_visible(False)
            ax1.spines['right'].set_visible(False)
            ax1.tick_params(labelsize=10)
            ax1.set_ylabel(f'Price ({self.currency})', fontsize=10, fontfamily='sans-serif')

            ax1.set_xticks(range(0, len(data), step))
            ax1.set_xticklabels(data.index[::step].strftime('%Y-%m-%d'), rotation=0)

            def format_prices(x, p):
                if x >= 1e3:
                    return f'{x/1e3:.1f}K'
                else:
                    return f'{x:.1f}'

            ax1.yaxis.set_major_formatter(plt.FuncFormatter(format_prices))

            if volume:
                if standalone:
                    ax1.set_xticklabels([])
                ax2.set_facecolor('white')

                colors = ['#26A69A' if close >= open else '#EF5350'
                        for open, close in zip(data['open'], data['close'])]

                ax2.bar(range(len(data)), 
                        data['volume'], 
                        alpha=0.8,
                        color=colors)

                ax2.grid(True, linestyle=':', color='#E0E0E0', alpha=0.6)

                ax2.set_xticks(range(0, len(data), step))
                ax2.set_xticklabels(data.index[::step].strftime('%Y-%m-%d'), rotation=0)

                # Clean up volume axis
                ax2.spines['top'].set_visible(False)
                ax2.spines['right'].set_visible(False)
                ax2.tick_params(labelsize=10)
                ax2.set_ylabel('Volume', fontsize=10, fontfamily='sans-serif')

                # Link the x-axes
                ax1.set_xlim(ax2.get_xlim())

                def format_volume(x, p):
                    if x >= 1e9:
                        return f'{x/1e9:.1f}B'
                    elif x >= 1e6:
                        return f'{x/1e6:.1f}M'
                    else:
                        return f'{x/1e3:.1f}K'

                ax2.yaxis.set_major_formatter(plt.FuncFormatter(format_volume))

                # Add padding for volume subplot
                plt.subplots_adjust(bottom=0.15)
            else:
                # Format x-axis for single plot
                n_labels = 6 if standalone else 3
                step = len(data) // n_labels
                ax1.set_xticks(range(0, len(data), step))
                ax1.set_xticklabels(data.index[::step].strftime('%Y-%m-%d'), rotation=0)

            # plt.tight_layout()

            if filename is not None:
                fig.savefig(filename, dpi=300, bbox_inches='tight', transparent=(True if filename.endswith('.png') else False))

        else:
            standalone = False
            if fig is None:
                standalone = True
                if volume:
                    fig = make_subplots(rows=2, cols=1, 
                                    shared_xaxes=True, 
                                    vertical_spacing=0.03, 
                                    row_heights=[0.7, 0.3])
                else:
                    fig = go.Figure()

            # Add candlestick
            if standalone:
                candlestick_row = 1
                candlestick_col = 1
            else:
                candlestick_row = candle_idx[0] if candle_idx is not None else 1
                candlestick_col = candle_idx[1] if candle_idx is not None else 1

            if volume:  # volume and standalone or subplot
                fig.add_trace(
                    go.Candlestick(
                        x=data.index,
                        open=data['open'],
                        high=data['high'],
                        low=data['low'],
                        close=data['close'],
                        name='OHLC'
                    ),
                    row=candlestick_row,
                    col=candlestick_col
                )
            else:
                if standalone:  # no volume and standalone
                    fig.add_trace(
                        go.Candlestick(
                            x=data.index,
                            open=data['open'],
                            high=data['high'],
                            low=data['low'],
                            close=data['close'],
                            name='OHLC'
                        )
                    )
                else:  # no volume and subplot
                    fig.add_trace(
                        go.Candlestick(
                            x=data.index,
                            open=data['open'],
                            high=data['high'],
                            low=data['low'],
                            close=data['close'],
                            name='OHLC'
                        ),
                        row=candlestick_row,
                        col=candlestick_col
                    )

            # Add volume only if requested
            if volume:
                if vol_idx is not None:
                    vol_row, vol_col = vol_idx
                else:
                    vol_row, vol_col = 2, 1

                colors = ['#26A69A' if close >= open else '#EF5350' 
              for open, close in zip(data['open'], data['close'])]
    
                fig.add_trace(
                    go.Bar(
                        x=data.index,
                        y=data['volume'],
                        name='Volume',
                        marker=dict(
                            color=colors,
                            line=dict(color=colors)
                        )
                    ),
                    row=vol_row,
                    col=vol_col
                )

            # Update layout
            title = f'{self.ticker} Candlestick Chart'
            if volume:
                title += ' with Volume Bars'

            layout_updates = {
                f'xaxis{candlestick_row}_rangeslider_visible': False,
                'height': 800 if volume else 600
            }

            if volume:
                layout_updates[f'xaxis{vol_row}_rangeslider_visible'] = False
            
            if standalone:
                layout_updates['title'] = title
            

            # Update y-axes labels
            if standalone:  # Single figure
                layout_updates['yaxis_title'] = f'Price ({self.currency})'
            else:  # Part of subplots
                fig.update_yaxes(title_text=f'Price ({self.currency})', row=candlestick_row, col=candlestick_col)
                fig.update_xaxes(
                    title_text=f'{self.ticker} Candlestick', row=candlestick_row, col=candlestick_col
                )
                if volume:
                    fig.update_yaxes(title_text="Volume", row=vol_row, col=vol_col)
                    fig.update_xaxes(
                        title_text=f'{self.ticker} Volume', row=vol_row, col=vol_col
                    ) 

            fig.update_layout(**layout_updates)

            # Only show if it's a standalone figure
            if standalone:
                fig.show()

            if filename is not None:
                fig.write_image(filename)

        return fig

    def plot_returns_dist(self, *, log_rets=False, bins=100, filename=None, 
                        fig=None, interactive=True, subplot_idx=None, show_stats=True):

        data = self.daily['log_rets'] if log_rets else self.daily['rets']
        data = data.dropna()

        # Calculate statistics
        stats_text = (
            f'Mean: {np.mean(data):.4f}{'\n' if not interactive else '<br>'}'
            f'Std Dev: {np.std(data):.4f}{'\n' if not interactive else '<br>'}'
            f'Skewness: {stats.skew(data):.4f}{'\n' if not interactive else '<br>'}'
            f'Kurtosis: {stats.kurtosis(data):.4f}'
        )

        if not interactive:
            # Set style
            plt.style.use('seaborn-v0_8')  # Professional looking style

            if fig is None:
                standalone = True
                fig, ax = plt.subplots(figsize=(12, 7))
            else:
                standalone = False
                ax = fig.axes[subplot_idx] if subplot_idx is not None else fig.gca()

            # Create histogram with improved styling
            sns.histplot(data, 
                        bins=bins,
                        color='#2E86C1',  # Professional blue color
                        alpha=0.7,  # Slight transparency
                        edgecolor='white',  # White edges for better contrast
                        ax=ax)

            # Customize the plot
            ax.set_title(f'{self.ticker} Returns Distribution', fontsize=14, pad=15, fontweight='bold')
            ax.set_xlabel('Returns', fontsize=12)
            ax.set_ylabel('Count', fontsize=12)

            # Remove top and right spines
            ax.spines['top'].set_visible(False)
            ax.spines['right'].set_visible(False)

            if show_stats:
                # Add stats box with better styling
                plt.text(0.75, 0.85, stats_text,
                        transform=ax.transAxes,
                        bbox=dict(
                            facecolor='white',
                            edgecolor='#2E86C1',  # Match histogram color
                            alpha=0.9,
                            boxstyle='round,pad=0.5'
                        ),
                        fontsize=10,
                        verticalalignment='top')

            # Add grid with lighter color
            ax.grid(True, alpha=0.3, linestyle='--')

            # Adjust layout
            fig.subplots_adjust(hspace=0.4)

            if filename is not None:
                    fig.savefig(filename, dpi=300, bbox_inches='tight', transparent=(True if filename.endswith('.png') else False))

        else:
            standalone = False
            if fig is None:
                fig = go.Figure()
                standalone = True

            bins = np.linspace(data.min(), data.max(), bins + 1)

            fig.add_trace(
                go.Histogram(
                    x=data,
                    xbins=dict(
                        start=bins[0],
                        end=bins[-1],
                        size=(bins[1] - bins[0])  # Forces exact bin width
                    ),
                    name=f'{self.ticker} Returns Distribution'
                ),
                row=subplot_idx[0] if subplot_idx else None,
                col=subplot_idx[1] if subplot_idx else None
            )

            xref = 'paper'
            yref = 'paper'
            
            if subplot_idx:
                xref = f'x{subplot_idx[0] if subplot_idx[0] != 1 else ''} domain'
                yref = f'y{subplot_idx[0] if subplot_idx[0] != 1 else ''} domain'

            if show_stats:
                fig.add_annotation(
                    x=0.95,
                    y=0.95,
                    xref=xref,
                    yref=yref,
                    text=stats_text,
                    showarrow=False,
                    font=dict(size=10),
                    align='left',
                    bgcolor='white',
                    bordercolor='black',
                    borderwidth=1,
                    xanchor='right',  # Right-align the box
                    yanchor='top'     # Top-align the box
                )

            fig.update_layout(
                yaxis=dict(
                    range=[0, None],
                    rangemode='nonnegative'
                ),
                bargap=0.05
            )

            if standalone:
                fig.update_layout(
                        title=f'{self.ticker} {'Log' if log_rets else ''} Returns Distribution',
                        xaxis_title='Returns',
                        yaxis_title=f'Count'
                    )
                
                fig.show()
            else:
                fig.update_yaxes(
                    title_text='Count',
                    row=subplot_idx[0] if subplot_idx else None, 
                    col=subplot_idx[1] if subplot_idx else None
                )
                fig.update_xaxes(
                    title_text=f'{self.ticker} {'Log' if log_rets else ''} Returns Distribution', 
                    row=subplot_idx[0] if subplot_idx else None, 
                    col=subplot_idx[1] if subplot_idx else None
                )

            if filename is not None:
                fig.write_image(filename)

        return fig


    def resample(self, period='D', five_min=False):

        if five_min:
            data = self.five_minute
        else:
            data = self.daily

        data = data.resample(period).agg({
            'open': 'first',     # First price of the month
            'high': 'max',       # Highest price of the month
            'low': 'min',        # Lowest price of the month
            'close': 'last',     # Last price of the month
            'adj_close': 'last', # Last adjusted price of the month
            'volume': 'sum',     # Total volume for the month
            'log_rets': 'sum',   # Sum of log returns
            'rets': 'sum'        # Sum of returns
        })

        data = data.dropna()
        
        return data

    def rolling_stats(self, *, window=20, five_min=False, r=0., ewm=False, alpha=None, halflife=None, bollinger_bands=False, num_std=2):
        
        if five_min:
            data = self.five_minute
            if self.asset_type == 'cryptocurrency':
                annualization_factor = 252 * 24 * 12
            else:
                annualization_factor = 252 * 78  # Assuming ~78 5-min periods per day
        else:
            data = self.daily
            annualization_factor = 252  # Trading days in a year

        roll_df = pd.DataFrame()
        cols = ['close', 'adj_close', 'rets', 'log_rets']

        for col in cols:
            if ewm:
                if alpha is not None:
                    roll_df[f'{col}_mean'] = data[f'{col}'].ewm(alpha=alpha).mean()
                    roll_df[f'{col}_std'] = data[f'{col}'].ewm(alpha=alpha).std()
                elif halflife is not None:
                    roll_df[f'{col}_mean'] = data[f'{col}'].ewm(halflife=halflife).mean()
                    roll_df[f'{col}_std'] = data[f'{col}'].ewm(halflife=halflife).std()
                else:
                    roll_df[f'{col}_mean'] = data[f'{col}'].ewm(span=window).mean()
                    roll_df[f'{col}_std'] = data[f'{col}'].ewm(span=window).std()
            else:
                roll_df[f'{col}_mean'] = data[f'{col}'].rolling(window=window).mean()
                roll_df[f'{col}_std'] = data[f'{col}'].rolling(window=window).std()

        roll_df = roll_df.dropna()

        # Calculate annualized Sharpe ratio
        daily_rf_rate = (1 + r) ** (1/annualization_factor) - 1
        excess_returns = roll_df['rets_mean'] - daily_rf_rate
        roll_df['sharpe'] = (excess_returns / roll_df['rets_std']) * np.sqrt(annualization_factor)

        if bollinger_bands:
            roll_df = self.add_bollinger_bands(roll_df, num_std=num_std)

        return roll_df

    def basic_stats(self):
        stats = {}

        # return statistics
        stats['returns'] = {
            'total_return': (self.daily['close'].iloc[-1] / 
                            self.daily['close'].iloc[0]) - 1,
            'daily_mean': self.daily['rets'].mean(),
            'daily_std': self.daily['rets'].std(),
            'daily_median': self.daily['rets'].median(),
            'annualized_vol': self.daily['rets'].std() * np.sqrt(252)
        }

        stats['returns'] = {k: float(v) for k, v in stats['returns'].items()}

        # price statistics
        stats['price'] = {
            'high': self.daily['high'].max(),
            'low': self.daily['low'].min(),
            '52w_high': self.daily[self.daily.index >= datetime.now() - pd.Timedelta(weeks=52)]['high'].max(),
            '52w_low': self.daily[self.daily.index >= datetime.now() - pd.Timedelta(weeks=52)]['low'].max(),
            'current': self.daily['close'].iloc[-1]
        }

        stats['price'] = {k: float(v) for k, v in stats['price'].items()}

        # distribution statistics
        stats['distribution'] = {
            'skewness': self.daily['rets'].skew(),
            'kurtosis': self.daily['rets'].kurtosis()
        }

        stats['distribution'] = {k: float(v) for k, v in stats['distribution'].items()}

        # TODO:
        # add risk statistics: var95, cvar95, max_drawdown, drawdown_period, downside volatility
        # add risk_adjusted returns:  sharpe ratio, sortino ratio, calmar ratio
        # add distribution: normality test, jarque_bera
        # add trading stats: pos/neg days, best/worst day, avg up/down day, vol_mean, vol_std

        return stats

    def plot_SMA(self, *, window=20, timeframe='1d', r=0., ewm=False, alpha=None, halflife=None, 
                 bollinger_bands=False, num_std=2, interactive=True, filename=None, start_date=None, 
                 end_date=None, resample=None, fig=None, subplot_idx=None):
        
        data = self.rolling_stats(window=window, five_min=True if timeframe != '1d' else False,
                                r=r, ewm=ewm, alpha=alpha, halflife=halflife, 
                                bollinger_bands=bollinger_bands, num_std=num_std)
        
        agg = {
                'close_mean': 'last',
                'adj_close_mean': 'last',
                'close_std': 'mean',
                'adj_close_std': 'mean',
                'rets_mean': 'sum',
                'log_rets_mean': 'sum',
                'rets_std': 'mean',
                'log_rets_std': 'mean',
                'sharpe': 'mean'
            }
        
        boll_agg = {
            'bol_up': 'last',
            'bol_low': 'last'
        }


        if bollinger_bands:
            agg = agg | boll_agg
        
        if start_date is not None:
            data = data[data.index >= start_date]
        if end_date is not None:
            data = data[data.index <= end_date]
        if resample is not None:
            data = data.resample(resample).agg(agg)

        data = data.dropna()

        if alpha is not None:
            param = f'{alpha=}'
        elif halflife is not None:
            param = f'{halflife=}'
        else:
            param = f'{window=}'
        
        if not interactive:

            if fig is None:
                fig, ax = plt.subplots(figsize=(12, 6))
            else:
                ax = fig.axes[subplot_idx] if subplot_idx is not None else fig.gca()

            # Set style
            plt.style.use('seaborn-v0_8')

            # Create the base plot with the main price line
            sns.lineplot(data=data, x=data.index, y='close_mean', 
                        color='#2962FF', linewidth=2, label=f'{self.ticker} MA ({param})',
                        ax=ax)

            if bollinger_bands:
                # Add the Bollinger Bands
                sns.lineplot(data=data, x=data.index, y='bol_up', 
                            color='#FF4081', linestyle='--', linewidth=1, label='Upper Band',
                            ax=ax)
                sns.lineplot(data=data, x=data.index, y='bol_low', 
                            color='#FF4081', linestyle='--', linewidth=1, label='Lower Band',
                            ax=ax)

                # Fill between the bands
                ax.fill_between(data.index, data['bol_low'], data['bol_up'], 
                                alpha=0.1, color='#2962FF')

            # Customize the plot
            ax.set_title(f'{self.ticker} Moving Average {f'with Bollinger Bands ({num_std=})' if bollinger_bands else ''}', pad=20)
            ax.set_xlabel('Date')
            ax.set_ylabel(f'Price ({self.currency})')

            def format_prices(x, p):
                if x >= 1e3:
                    return f'{x/1e3:.1f}K'
                else:
                    return f'{x:.1f}'

            ax.yaxis.set_major_formatter(plt.FuncFormatter(format_prices))

            # Adjust legend
            ax.legend(loc='upper left', framealpha=0.9)

            # Adjust grid settings
            ax.grid(True, alpha=0.2)

            # Rotate x-axis labels if needed
            plt.xticks(rotation=0)

            # Adjust layout
            # fig.tight_layout()

            if filename is not None:
                fig.savefig(filename, dpi=300, bbox_inches='tight', transparent=(True if filename.endswith('.png') else False))

        else:
            standalone = False
            if fig is None:
                fig = go.Figure()
                standalone = True

            # Add main price line with explicit solid style
            fig.add_trace(
                go.Scatter(
                    x=data.index,
                    y=data['close_mean'],
                    line=dict(
                        color='#2962FF',
                        width=2,
                        dash='solid'  # Explicitly set solid line
                    ),
                    name=f'{self.ticker} MA {param}'
                ),
                row=subplot_idx[0] if subplot_idx else None,
                col=subplot_idx[1] if subplot_idx else None
            )

            if bollinger_bands:
                # Add lower band
                fig.add_trace(go.Scatter(
                    x=data.index,
                    y=data['bol_low'],
                    name='Lower Band',
                    line=dict(color='#FF4081', width=1, dash='dash'),
                    mode='lines',
                    showlegend=True
                    ),
                    row=subplot_idx[0] if subplot_idx else None,
                    col=subplot_idx[1] if subplot_idx else None
                )

                # Add upper band with fill
                fig.add_trace(go.Scatter(
                    x=data.index,
                    y=data['bol_up'],
                    name='Upper Band',
                    line=dict(color='#FF4081', width=1, dash='dash'),
                    mode='lines',
                    fill='tonexty',
                    fillcolor='rgba(68, 68, 255, 0.1)',
                    showlegend=True
                    ),
                    row=subplot_idx[0] if subplot_idx else None,
                    col=subplot_idx[1] if subplot_idx else None
                )

            # Update the layout for a cleaner look

            fig.update_layout(
                title=dict(
                    text=f'{self.ticker} Moving Average {f'with Bollinger Bands ({num_std=})' if bollinger_bands else ''}',
                    x=0.5,  # Center the title
                    y=0.95
                ) if standalone else None,
                paper_bgcolor='white',
                plot_bgcolor='rgba(240,240,240,0.95)',  # Light gray background
                xaxis=dict(
                    showgrid=True,
                    gridwidth=1,
                    gridcolor='rgba(128,128,128,0.2)',
                    title=None,  # Remove x-axis title if it's obvious
                ),
                yaxis=dict(
                    showgrid=True,
                    gridwidth=1,
                    gridcolor='rgba(128,128,128,0.2)',
                    title=f'Price ({self.currency})',
                ),
                legend=dict(
                    yanchor="top",
                    y=0.99,
                    xanchor="left",
                    x=0.01,
                    bgcolor='rgba(255,255,255,0.8)'
                ),
                hovermode='x unified'  # Shows all values for a given x-position
            )

            if not standalone:
                fig.update_yaxes(
                    title_text=f'Price ({self.currency})',
                    row=subplot_idx[0] if subplot_idx else None, 
                    col=subplot_idx[1] if subplot_idx else None
                )
                fig.update_xaxes(
                    title_text=f'{self.ticker} Moving Average {f'with Bollinger Bands ({num_std=})' if bollinger_bands else ''}', 
                    row=subplot_idx[0] if subplot_idx else None, 
                    col=subplot_idx[1] if subplot_idx else None
                )
            
            if standalone:
                fig.show()

            if filename is not None:
                fig.write_image(filename)

        return fig
        

    def add_bollinger_bands(self, df, num_std=2):
        df['bol_up'] = df['close_mean'] + num_std * df['close_std']
        df['bol_low'] = df['close_mean'] - num_std * df['close_std']

        return df
    
    def SMA_crossover(self, *, short=20, long=50, timeframe='1d', start_date=None, end_date=None, r=0.,
                        resample=None, return_trace=False, y2=2,
                        ewm=None, short_a=None, long_a=None, short_t=None, long_t=None, show_signal=True,
                        interactive=True, filename=None, fig=None, subplot_idx=None):
            
            
            long_data = self.rolling_stats(window=long, five_min=(True if timeframe != '1d' else False),
                                    r=r, ewm=ewm, alpha=long_a, halflife=long_t,
                                    bollinger_bands=False)
            
            short_data = self.rolling_stats(window=short, five_min=(True if timeframe != '1d' else False),
                                    r=r, ewm=ewm, alpha=short_a, halflife=short_t,
                                    bollinger_bands=False)

            agg = {
                    'close_mean': 'last',
                    'adj_close_mean': 'last',
                    'close_std': 'mean',
                    'adj_close_std': 'mean',
                    'rets_mean': 'sum',
                    'log_rets_mean': 'sum',
                    'rets_std': 'mean',
                    'log_rets_std': 'mean',
                    'sharpe': 'mean'
                }
            
            if start_date is not None:
                long_data = long_data[long_data.index >= start_date]
                short_data = short_data[short_data.index >= start_date]
            if end_date is not None:
                long_data = long_data[long_data.index <= end_date]
                short_data = short_data[short_data.index <= end_date]
            if resample is not None:
                long_data = long_data.resample(resample).agg(agg)
                short_data = short_data.resample(resample).agg(agg)

            long_data = long_data.dropna()
            short_data = short_data.dropna()

            common_index = long_data.index.intersection(short_data.index)
            long_data = long_data.reindex(common_index)
            short_data = short_data.reindex(common_index)
            if show_signal:
                signal = (short_data['close_mean'] > long_data['close_mean']).astype(int)

            if short_a is not None:
                short_param = f'alpha={short_a}'
            elif short_t is not None:
                short_param = f'halflife={short_t}'
            else:
                short_param = f'window={short}'

            if long_a is not None:
                long_param = f'alpha={long_a}'
            elif short_t is not None:
                long_param = f'halflife={long_t}'
            else:
                long_param = f'window={long}'

            if not interactive:

                # Create the plot
                plt.style.use('seaborn-v0_8')
                
                if fig is None:
                    fig, ax1 = plt.subplots(figsize=(12, 8))
                else:
                    ax1 = fig.axes[subplot_idx] if subplot_idx is not None else fig.gca()
                
                # Plot MAs on top subplot
                sns.lineplot(data=short_data, x=short_data.index, y='close_mean', 
                            ax=ax1, color='#2962FF', label=f'{self.ticker} MA ({short_param})')
                sns.lineplot(data=long_data, x=long_data.index, y='close_mean', 
                            ax=ax1, color='red', label=f'{self.ticker} MA ({long_param})')
                
                if show_signal:
                    # Create secondary y-axis
                    ax2 = ax1.twinx()
                    
                    # Plot signal on secondary y-axis
                    sns.lineplot(x=signal.index, y=signal, 
                                ax=ax2, color='green', label='Buy/Sell signal')
                    
                    # Customize secondary axis
                    ax2.set_ylabel('Signal')
                    ax2.set_ylim(-0.1, 1.1)
                    ax2.set_yticks([0, 1])
                    ax2.set_yticklabels(['Sell', 'Buy'])
                    ax2.grid(False)  # Disable grid for secondary axis to avoid overlap
                    lines2, labels2 = ax2.get_legend_handles_labels()

                # Customize primary axis
                ax1.set_title(f'{self.ticker} SMA Crossover ({short}/{long})', pad=20)
                ax1.set_xlabel('Date')
                ax1.set_ylabel(f'Price ({self.currency})')
                ax1.grid(True, alpha=0.3)

                # Combine legends from both axes
                lines1, labels1 = ax1.get_legend_handles_labels()

                lines = lines1 + lines2 if show_signal else lines1
                labels = labels1 + labels2 if show_signal else labels1
                
                ax1.legend(lines, labels, 
                        bbox_to_anchor=(0.5, 1.15), loc='center', ncol=3)
                
                def format_prices(x, p):
                    if x >= 1e3:
                        return f'{x/1e3:.1f}K'
                    else:
                        return f'{x:.1f}'

                ax1.yaxis.set_major_formatter(plt.FuncFormatter(format_prices))
                
                if show_signal:
                    ax2.get_legend().remove()  # Remove redundant legend

                # plt.tight_layout()

                if filename is not None:
                    fig.savefig(filename, dpi=300, bbox_inches='tight', transparent=(True if filename.endswith('.png') else False))

            else:

                # Add short MA line
                trace1 = go.Scatter(
                    x=short_data.index,
                    y=short_data['close_mean'],
                    line=dict(
                        color='#2962FF',
                        width=2,
                        dash='solid'
                    ),
                    name=f'{self.ticker} MA ({short_param})',
                    yaxis='y'
                )

                # Add long MA line
                trace2 = go.Scatter(
                    x=long_data.index,
                    y=long_data['close_mean'],
                    line=dict(
                        color='red',
                        width=2,
                        dash='solid'
                    ),
                    name=f'{self.ticker} MA ({long_param})',
                    yaxis='y'
                )

                if show_signal:
                    trace3 = go.Scatter(
                        x=signal.index,
                        y=signal,
                        line=dict(color='green', width=0.8, dash='solid'),
                        name='Buy/Sell signal',
                        yaxis='y2'
                    )

                if return_trace:
                    if show_signal:
                        return [trace1, trace2, trace3]
                    return [trace1, trace2]

                # Add traces based on whether it's a subplot or not
                standalone = False
                if fig is None:
                    standalone = True
                    fig = go.Figure()
                
                fig.add_trace(trace1,
                            row=subplot_idx[0] if subplot_idx else None,
                            col=subplot_idx[1] if subplot_idx else None)
                
                fig.add_trace(trace2, 
                            row=subplot_idx[0] if subplot_idx else None,
                            col=subplot_idx[1] if subplot_idx else None)

                if show_signal:
                    if standalone:
                        fig.add_trace(trace3)
                    else:
                        fig.add_trace(trace3,
                                    row=subplot_idx[0] if subplot_idx else None,
                                    col=subplot_idx[1] if subplot_idx else None,
                                    secondary_y=True)

                # Update layout with secondary y-axis
                layout = {}

                if standalone:
                    layout['title'] = dict(
                            text=f'{self.ticker} SMA Crossover ({short}/{long})',
                            x=0.5,
                            y=0.95
                        )
                
                layout['xaxis'] = dict(
                        showgrid=True,
                        gridwidth=1,
                        gridcolor='rgba(128,128,128,0.2)',
                        title=None,
                    )
                
                
                layout['yaxis'] = dict(
                        showgrid=True,
                        gridwidth=1,
                        gridcolor='rgba(128,128,128,0.2)',
                        title=f'Price ({self.currency})',
                    )
                
                if show_signal:
                    layout[f'yaxis{y2}'] = dict(
                            title='Signal',
                            overlaying='y',
                            side='right',
                            range=[-0.1, 1.1],  # Give some padding to the 0/1 signal
                            tickmode='array',
                            tickvals=[0, 1],
                            ticktext=['Sell', 'Buy']
                        )
                
                layout['legend'] = dict(
                        yanchor="bottom",
                        y=1.02,
                        xanchor="center",
                        x=0.5,
                        orientation="h",  # horizontal layout
                        bgcolor='rgba(255,255,255,0.8)'
                    )
                
                fig.update_layout(**layout,
                                  paper_bgcolor='white',
                                  plot_bgcolor='rgba(240,240,240,0.95)',
                                  hovermode='x unified')

                if standalone:
                    fig.show()
                else:
                    fig.update_yaxes(
                        title_text=f'Price ({self.currency})',
                        row=subplot_idx[0] if subplot_idx else None, 
                        col=subplot_idx[1] if subplot_idx else None
                    )
                    fig.update_xaxes(
                        title_text=f'{self.ticker} SMA Crossover ({short}/{long})', 
                        row=subplot_idx[0] if subplot_idx else None, 
                        col=subplot_idx[1] if subplot_idx else None
                    )

                if filename is not None:
                    fig.write_image(filename)

            return fig
    
    # TODO:
    # backtest SMA strategy
    # optimize SMA window
    # plot more diagrams
    # add more technical indicators (RSI, MACD, ATR)
    # simple default dashboard
