import time
import math
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_DOWN
from ..strategy import Strategy
from typing import Tuple
import threading
import os

class BybitDynamicHedgeStrategy(Strategy):
    def __init__(self, exchange, manager, config):
        super().__init__(exchange, config, manager)
        self.manager = manager
        self.last_cancel_time = 0
        self.wallet_exposure_limit = self.config.wallet_exposure_limit
        self.current_wallet_exposure = 1.0
        self.printed_trade_quantities = False

    def calculate_trade_quantity(self, symbol, leverage):
        dex_equity = self.exchange.get_balance_bybit('USDT')
        trade_qty = (float(dex_equity) * self.current_wallet_exposure) / leverage
        return trade_qty

    def adjust_position_wallet_exposure(self, symbol):
        if self.current_wallet_exposure > self.wallet_exposure_limit:
            desired_wallet_exposure = self.wallet_exposure_limit
            # Calculate the necessary position size to achieve the desired wallet exposure
            max_trade_qty = self.calculate_trade_quantity(symbol, 1)
            current_trade_qty = self.calculate_trade_quantity(symbol, 1 / self.current_wallet_exposure)
            reduction_qty = current_trade_qty - max_trade_qty
            # Reduce the position to the desired wallet exposure level
            self.exchange.reduce_position_bybit(symbol, reduction_qty)

    def truncate(self, number: float, precision: int) -> float:
        return float(Decimal(number).quantize(Decimal('0.' + '0'*precision), rounding=ROUND_DOWN))


    def limit_order(self, symbol, side, amount, price, positionIdx, reduceOnly=False):
        params = {"reduceOnly": reduceOnly}
        #print(f"Symbol: {symbol}, Side: {side}, Amount: {amount}, Price: {price}, Params: {params}")
        order = self.exchange.create_limit_order_bybit(symbol, side, amount, price, positionIdx=positionIdx, params=params)
        return order

    def get_open_take_profit_order_quantity(self, orders, side):
        for order in orders:
            if order['side'].lower() == side.lower() and order['reduce_only']:
                return order['qty'], order['id']
        return None, None

    def get_open_take_profit_order_quantities(self, orders, side):
        take_profit_orders = []
        for order in orders:
            if order['side'].lower() == side.lower() and order['reduce_only']:
                take_profit_orders.append((order['qty'], order['id']))
        return take_profit_orders

    def cancel_take_profit_orders(self, symbol, side):
        self.exchange.cancel_close_bybit(symbol, side)

    def calculate_short_take_profit(self, short_pos_price, symbol):
        if short_pos_price is None:
            return None

        five_min_data = self.manager.get_5m_moving_averages(symbol)
        price_precision = int(self.exchange.get_price_precision(symbol))

        if five_min_data is not None:
            ma_6_high = Decimal(five_min_data["MA_6_H"])
            ma_6_low = Decimal(five_min_data["MA_6_L"])

            try:
                short_target_price = Decimal(short_pos_price) - (ma_6_high - ma_6_low)
            except InvalidOperation as e:
                print(f"Error: Invalid operation when calculating short_target_price. short_pos_price={short_pos_price}, ma_6_high={ma_6_high}, ma_6_low={ma_6_low}")
                return None

            try:
                short_target_price = short_target_price.quantize(
                    Decimal('1e-{}'.format(price_precision)),
                    rounding=ROUND_HALF_UP
                )
            except InvalidOperation as e:
                print(f"Error: Invalid operation when quantizing short_target_price. short_target_price={short_target_price}, price_precision={price_precision}")
                return None

            short_profit_price = short_target_price

            return float(short_profit_price)
        return None

    def calculate_long_take_profit(self, long_pos_price, symbol):
        if long_pos_price is None:
            return None

        five_min_data = self.manager.get_5m_moving_averages(symbol)
        price_precision = int(self.exchange.get_price_precision(symbol))

        if five_min_data is not None:
            ma_6_high = Decimal(five_min_data["MA_6_H"])
            ma_6_low = Decimal(five_min_data["MA_6_L"])

            try:
                long_target_price = Decimal(long_pos_price) + (ma_6_high - ma_6_low)
            except InvalidOperation as e:
                print(f"Error: Invalid operation when calculating long_target_price. long_pos_price={long_pos_price}, ma_6_high={ma_6_high}, ma_6_low={ma_6_low}")
                return None

            try:
                long_target_price = long_target_price.quantize(
                    Decimal('1e-{}'.format(price_precision)),
                    rounding=ROUND_HALF_UP
                )
            except InvalidOperation as e:
                print(f"Error: Invalid operation when quantizing long_target_price. long_target_price={long_target_price}, price_precision={price_precision}")
                return None

            long_profit_price = long_target_price

            return float(long_profit_price)
        return None

    def run(self, symbol):
        wallet_exposure = self.config.wallet_exposure
        min_dist = self.config.min_distance
        min_vol = self.config.min_volume
        current_leverage = self.exchange.get_current_leverage_bybit(symbol)
        max_leverage = self.exchange.get_max_leverage_bybit(symbol)
        retry_delay = 5
        max_retries = 5

        print("Setting up exchange")
        self.exchange.setup_exchange_bybit(symbol)

        print("Setting leverage")
        if current_leverage != max_leverage:
            print(f"Current leverage is not at maximum. Setting leverage to maximum. Maximum is {max_leverage}")
            self.exchange.set_leverage_bybit(max_leverage, symbol)

        while True:
            print(f"Bybit hedge strategy running")
            print(f"Min volume: {min_vol}")
            print(f"Min distance: {min_dist}")

            # Get API data
            data = self.manager.get_data()
            one_minute_volume = self.manager.get_asset_value(symbol, data, "1mVol")
            five_minute_distance = self.manager.get_asset_value(symbol, data, "5mSpread")
            thirty_minute_distance = self.manager.get_asset_value(symbol, data, "30mSpread")
            one_hour_distance = self.manager.get_asset_value(symbol, data, "1hSpread")
            trend = self.manager.get_asset_value(symbol, data, "Trend")
            print(f"1m Volume: {one_minute_volume}")
            print(f"5m Spread: {five_minute_distance}")
            print(f"Trend: {trend}")

            price_precision = int(self.exchange.get_price_precision(symbol))

            print(f"Precision: {price_precision}")

            quote_currency = "USDT"

            for i in range(max_retries):
                try:
                    total_equity = self.exchange.get_balance_bybit(quote_currency)
                    break
                except Exception as e:
                    if i < max_retries - 1:
                        print(f"Error occurred while fetching balance: {e}. Retrying in {retry_delay} seconds...")
                        time.sleep(retry_delay)
                    else:
                        raise e
                    
            print(f"Total equity: {total_equity}")

            current_price = self.exchange.get_current_price(symbol)
            market_data = self.get_market_data_with_retry(symbol, max_retries = 5, retry_delay = 5)
            contract_size = self.exchange.get_contract_size_bybit(symbol)
            best_ask_price = self.exchange.get_orderbook(symbol)['asks'][0][0]
            best_bid_price = self.exchange.get_orderbook(symbol)['bids'][0][0]

            print(f"Best bid: {best_bid_price}")
            print(f"Best ask: {best_ask_price}")
            print(f"Current price: {current_price}")

            max_trade_qty = self.calc_max_trade_qty(total_equity,
                                                     best_ask_price,
                                                     max_leverage)  
            
            print(f"Max trade quantity for {symbol}: {max_trade_qty}")

            # debug_data = market_data
            # print(f"Debug market data: {debug_data}")

            # Calculate the dynamic amount
            amount = 0.001 * max_trade_qty

            min_qty = float(market_data["min_qty"])
            min_qty_str = str(min_qty)

            # Get the precision level of the minimum quantity
            if ".0" in min_qty_str:
                # The minimum quantity does not have a fractional part, precision is 0
                precision_level = 0
            else:
                # The minimum quantity has a fractional part, get its precision level
                precision_level = len(min_qty_str.split(".")[1])

            # Calculate the dynamic amount
            amount = 0.001 * max_trade_qty

            # Round the amount to the precision level of the minimum quantity
            amount = round(amount, precision_level)

            print(f"Dynamic amount: {amount}")

            # Check if the amount is less than the minimum quantity allowed by the exchange
            if amount < min_qty:
                print(f"Dynamic amount too small for 0.001x, using min_qty")
                amount = min_qty

            print(f"Min qty: {min_qty}")

            self.check_amount_validity_bybit(amount, symbol)

            self.print_trade_quantities_once_bybit(max_trade_qty)

            #self.exchange.debug_derivatives_markets_bybit()

            #print(f"Market data for {symbol}: {market_data}")

            #self.exchange.debug_derivatives_positions(symbol)

            # Get the 1-minute moving averages
            print(f"Fetching MA data")
            m_moving_averages = self.manager.get_1m_moving_averages(symbol)
            m5_moving_averages = self.manager.get_5m_moving_averages(symbol)
            ma_6_low = m_moving_averages["MA_6_L"]
            ma_3_low = m_moving_averages["MA_3_L"]
            ma_3_high = m_moving_averages["MA_3_H"]
            ma_1m_3_high = self.manager.get_1m_moving_averages(symbol)["MA_3_H"]
            ma_5m_3_high = self.manager.get_5m_moving_averages(symbol)["MA_3_H"]

            position_data = self.exchange.get_positions_bybit(symbol)

            #print(f"Bybit pos data: {position_data}")

            short_pos_qty = position_data["short"]["qty"]
            long_pos_qty = position_data["long"]["qty"]

            print(f"Short pos qty: {short_pos_qty}")
            print(f"Long pos qty: {long_pos_qty}")

            short_upnl = position_data["short"]["upnl"]
            long_upnl = position_data["long"]["upnl"]

            print(f"Short uPNL: {short_upnl}")
            print(f"Long uPNL: {long_upnl}")

            cum_realised_pnl_long = position_data["long"]["cum_realised"]
            cum_realised_pnl_short = position_data["short"]["cum_realised"]

            print(f"Short cum. PNL: {cum_realised_pnl_short}")
            print(f"Long cum. PNL: {cum_realised_pnl_long}")

            short_pos_price = position_data["short"]["price"] if short_pos_qty > 0 else None
            long_pos_price = position_data["long"]["price"] if long_pos_qty > 0 else None

            print(f"Long pos price {long_pos_price}")
            print(f"Short pos price {short_pos_price}")

            # Take profit calc
            short_take_profit = self.calculate_short_take_profit(short_pos_price, symbol)
            long_take_profit = self.calculate_long_take_profit(long_pos_price, symbol)


            should_short = best_bid_price > ma_3_high
            should_long = best_bid_price < ma_3_high

            should_add_to_short = False
            should_add_to_long = False
        
            if short_pos_price is not None:
                should_add_to_short = short_pos_price < ma_6_low
                short_tp_distance_percent = ((short_take_profit - short_pos_price) / short_pos_price) * 100
                short_expected_profit_usdt = short_tp_distance_percent / 100 * short_pos_price * short_pos_qty
                print(f"Short TP price: {short_take_profit}, TP distance in percent: {-short_tp_distance_percent:.2f}%, Expected profit: {-short_expected_profit_usdt:.2f} USDT")

            if long_pos_price is not None:
                should_add_to_long = long_pos_price > ma_6_low
                long_tp_distance_percent = ((long_take_profit - long_pos_price) / long_pos_price) * 100
                long_expected_profit_usdt = long_tp_distance_percent / 100 * long_pos_price * long_pos_qty
                print(f"Long TP price: {long_take_profit}, TP distance in percent: {long_tp_distance_percent:.2f}%, Expected profit: {long_expected_profit_usdt:.2f} USDT")

            print(f"Short condition: {should_short}")
            print(f"Long condition: {should_long}")
            print(f"Add short condition: {should_add_to_short}")
            print(f"Add long condition: {should_add_to_long}")

            if trend is not None and isinstance(trend, str):
                if one_minute_volume is not None and five_minute_distance is not None:
                    if one_minute_volume > min_vol and five_minute_distance > min_dist:

                        if trend.lower() == "long" and should_long and long_pos_qty == 0:
                            print(f"Placing initial long entry")
                            self.limit_order(symbol, "buy", amount, best_bid_price, positionIdx=1, reduceOnly=False)
                            print(f"Placed initial long entry")
                        else:
                            if trend.lower() == "long" and should_add_to_long and long_pos_qty < max_trade_qty and best_bid_price < long_pos_price:
                                print(f"Placed additional long entry")
                                self.limit_order(symbol, "buy", amount, best_bid_price, positionIdx=1, reduceOnly=False)

                        if trend.lower() == "short" and should_short and short_pos_qty == 0:
                            print(f"Placing initial short entry")
                            self.limit_order(symbol, "sell", amount, best_ask_price, positionIdx=2, reduceOnly=False)
                            print("Placed initial short entry")
                        else:
                            if trend.lower() == "short" and should_add_to_short and short_pos_qty < max_trade_qty and best_ask_price > short_pos_price:
                                print(f"Placed additional short entry")
                                self.limit_order(symbol, "sell", amount, best_bid_price, positionIdx=2, reduceOnly=False)
        
            open_orders = self.exchange.get_open_orders(symbol)

            if long_pos_qty > 0 and long_take_profit is not None:
                existing_long_tps = self.get_open_take_profit_order_quantities(open_orders, "sell")
                total_existing_long_tp_qty = sum(qty for qty, _ in existing_long_tps)
                print(f"Existing long TPs: {existing_long_tps}")
                if not math.isclose(total_existing_long_tp_qty, long_pos_qty):
                    try:
                        for qty, existing_long_tp_id in existing_long_tps:
                            if not math.isclose(qty, long_pos_qty):
                                self.exchange.cancel_take_profit_order_by_id(existing_long_tp_id, symbol)
                                print(f"Long take profit {existing_long_tp_id} canceled")
                                time.sleep(0.05)
                    except Exception as e:
                        print(f"Error in cancelling long TP orders {e}")

                if not any(math.isclose(qty, long_pos_qty) for qty, _ in existing_long_tps):
                    try:
                        self.exchange.create_take_profit_order_bybit(symbol, "limit", "sell", long_pos_qty, long_take_profit, positionIdx=1, reduce_only=True)
                        print(f"Long take profit set at {long_take_profit}")
                        time.sleep(0.05)
                    except Exception as e:
                        print(f"Error in placing long TP: {e}")

            if short_pos_qty > 0 and short_take_profit is not None:
                existing_short_tps = self.get_open_take_profit_order_quantities(open_orders, "buy")
                total_existing_short_tp_qty = sum(qty for qty, _ in existing_short_tps)
                print(f"Existing short TPs: {existing_short_tps}")
                if not math.isclose(total_existing_short_tp_qty, short_pos_qty):
                    try:
                        for qty, existing_short_tp_id in existing_short_tps:
                            if not math.isclose(qty, short_pos_qty):
                                self.exchange.cancel_take_profit_order_by_id(existing_short_tp_id, symbol)
                                print(f"Short take profit {existing_short_tp_id} canceled")
                                time.sleep(0.05)
                    except Exception as e:
                        print(f"Error in cancelling short TP orders: {e}")

                if not any(math.isclose(qty, short_pos_qty) for qty, _ in existing_short_tps):
                    try:
                        self.exchange.create_take_profit_order_bybit(symbol, "limit", "buy", short_pos_qty, short_take_profit, positionIdx=2, reduce_only=True)
                        print(f"Short take profit set at {short_take_profit}")
                        time.sleep(0.05)
                    except Exception as e:
                        print(f"Error in placing short TP: {e}")

            # Cancel entries
            current_time = time.time()
            if current_time - self.last_cancel_time >= 60:  # Execute this block every 1 minute
                try:
                    if best_ask_price < ma_1m_3_high or best_ask_price < ma_5m_3_high:
                        self.exchange.cancel_all_entries_bybit(symbol)
                        print(f"Canceled entry orders for {symbol}")
                        time.sleep(0.05)
                except Exception as e:
                    print(f"An error occurred while canceling entry orders: {e}")

                self.last_cancel_time = current_time  # Update the last cancel time

            time.sleep(30)

            # # Call the get_open_take_profit_order_quantity function for the 'buy' side
            # buy_qty, buy_id = self.get_open_take_profit_order_quantity(open_orders, 'buy')

            # # Call the get_open_take_profit_order_quantity function for the 'sell' side
            # sell_qty, sell_id = self.get_open_take_profit_order_quantity(open_orders, 'sell')

            # # Print the results
            # print("Buy Take Profit Order - Quantity: ", buy_qty, "ID: ", buy_id)
            # print("Sell Take Profit Order - Quantity: ", sell_qty, "ID: ", sell_id)
