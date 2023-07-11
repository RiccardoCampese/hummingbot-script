import asyncio
from enum import Enum
from time import time
from typing import Any, Dict, List, Optional, Tuple

import jsonpickle
from _decimal import Decimal
from dotmap import DotMap

from hummingbot.client.config.config_helpers import ClientConfigAdapter
from hummingbot.connector.gateway.clob_spot.data_sources.clob_api_data_source_base import CLOBAPIDataSourceBase
from hummingbot.connector.gateway.common_types import CancelOrderResult, PlaceOrderResult
from hummingbot.connector.gateway.gateway_in_flight_order import GatewayInFlightOrder
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.core.data_type import in_flight_order
from hummingbot.core.data_type.cancellation_result import CancellationResult
from hummingbot.core.data_type.common import OrderType
from hummingbot.core.data_type.in_flight_order import OrderState, OrderUpdate, TradeUpdate
from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType
from hummingbot.core.data_type.trade_fee import MakerTakerExchangeFeeRates, TokenAmount, TradeFeeBase, TradeFeeSchema
from hummingbot.core.event.events import AccountEvent, MarketEvent, OrderBookDataSourceEvent
from hummingbot.core.gateway.gateway_http_client import GatewayHttpClient
from hummingbot.core.network_iterator import NetworkStatus
from hummingbot.core.utils.async_utils import safe_ensure_future, safe_gather

from .kujira_constants import CONNECTOR, KUJIRA_NATIVE_TOKEN, MARKETS_UPDATE_INTERVAL
from .kujira_helpers import convert_market_name_to_hb_trading_pair, generate_hash
from .kujira_types import OrderStatus as KujiraOrderStatus


class KujiraAPIDataSource(CLOBAPIDataSourceBase):

    def __init__(
        self,
        trading_pairs: List[str],
        connector_spec: Dict[str, Any],
        client_config_map: ClientConfigAdapter,
    ):
        super().__init__(
            trading_pairs=trading_pairs,
            connector_spec=connector_spec,
            client_config_map=client_config_map
        )

        self._chain = connector_spec["chain"]
        self._network = connector_spec["network"]
        self._connector = CONNECTOR
        self._owner_address = connector_spec["wallet_address"]
        self._payer_address = self._owner_address

        self._trading_pair = None
        if self._trading_pairs:
            self._trading_pair = self._trading_pairs[0]

        self._markets = None
        self._market = None

        self._user_balances = None

        self._tasks = DotMap({
            "update_markets": None,
        }, _dynamic=False)

        self._locks = DotMap({
            "place_order": asyncio.Lock(),
            "place_orders": asyncio.Lock(),
            "cancel_order": asyncio.Lock(),
            "cancel_orders": asyncio.Lock(),
            "cancel_all_orders": asyncio.Lock(),
            "settle_market_funds": asyncio.Lock(),
            "settle_markets_funds": asyncio.Lock(),
            "settle_all_markets_funds": asyncio.Lock(),
        }, _dynamic=False)

        self._gateway = GatewayHttpClient.get_instance(self._client_config)

    @property
    def real_time_balance_update(self) -> bool:
        return False

    @property
    def events_are_streamed(self) -> bool:
        return False

    @staticmethod
    def supported_stream_events() -> List[Enum]:
        return [
            MarketEvent.TradeUpdate,
            MarketEvent.OrderUpdate,
            AccountEvent.BalanceEvent,
            OrderBookDataSourceEvent.TRADE_EVENT,
            OrderBookDataSourceEvent.DIFF_EVENT,
            OrderBookDataSourceEvent.SNAPSHOT_EVENT,
        ]

    def get_supported_order_types(self) -> List[OrderType]:
        return [OrderType.LIMIT]

    async def start(self):
        self.logger().setLevel("DEBUG")
        self.logger().debug("start: start")

        await self._update_markets()

        await self.cancel_all_orders()

        self._tasks.update_markets = self._tasks.update_markets or safe_ensure_future(
            coro=self._update_markets_loop()
        )
        self.logger().debug("start: end")

    async def stop(self):
        self.logger().debug("stop: start")
        self._tasks.update_markets and self._tasks.update_markets.cancel()
        self._tasks.update_markets = None

        await self.cancel_all_orders()

        self.logger().debug("stop: end")

    async def place_order(self, order: GatewayInFlightOrder, **kwargs) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        self.logger().debug("place_order: start")

        self._check_markets_initialized() or await self._update_markets()

        async with self._locks.place_order:
            try:
                request = {
                    "connector": self._connector,
                    "chain": self._chain,
                    "network": self._network,
                    "trading_pair": self._trading_pair,
                    "address": self._owner_address,
                    "trade_type": order.trade_type,
                    "order_type": order.order_type,
                    "price": order.price,
                    "size": order.amount,
                    "client_order_id": order.client_order_id,
                }

                self.logger().debug(f"""clob_place_order request:\n "{self._dump(request)}".""")

                response = await self._gateway.clob_place_order(**request)

                self.logger().debug(f"""clob_place_order response:\n "{self._dump(response)}".""")

                transaction_hash = response["txHash"]

                order.exchange_order_id = response["id"]

                self.logger().debug(
                    f"""Order "{order.client_order_id}" / "{order.exchange_order_id}" successfully placed. Transaction hash: "{transaction_hash}"."""
                )
            except Exception as exception:
                self.logger().debug(
                    f"""Placement of order "{order.client_order_id}" failed."""
                )

                raise exception

            if transaction_hash in (None, ""):
                raise Exception(
                    f"""Placement of order "{order.client_order_id}" failed. Invalid transaction hash: "{transaction_hash}"."""
                )

        misc_updates = DotMap({
            "creation_transaction_hash": transaction_hash,
        }, _dynamic=False)

        self.logger().debug("place_order: end")

        return order.exchange_order_id, misc_updates

    async def batch_order_create(self, orders_to_create: List[GatewayInFlightOrder]) -> List[PlaceOrderResult]:
        self.logger().debug("batch_order_create: start")

        self._check_markets_initialized() or await self._update_markets()

        candidate_orders = [in_flight_order]
        client_ids = []
        for order_to_create in orders_to_create:
            order_to_create.client_order_id = generate_hash(order_to_create)
            client_ids.append(order_to_create.client_order_id)

            candidate_order = in_flight_order.InFlightOrder(
                amount=order_to_create.amount,
                client_order_id=order_to_create.client_order_id,
                creation_timestamp=0,
                order_type=order_to_create.order_type,
                trade_type=order_to_create.trade_type,
                trading_pair=self._trading_pair,
            )
            candidate_orders.append(candidate_order)

        async with self._locks.place_orders:
            try:
                request = {
                    "connector": self._connector,
                    "chain": self._chain,
                    "network": self._network,
                    "address": self._owner_address,
                    "orders_to_create": candidate_orders,
                    "orders_to_cancel": [],
                }

                self.logger().debug(f"""clob_batch_order_modify request:\n "{self._dump(request)}".""")

                response = await self._gateway.clob_batch_order_modify(**request)

                self.logger().debug(f"""clob_batch_order_modify response:\n "{self._dump(response)}".""")

                transaction_hash = response["txHash"]

                self.logger().debug(
                    f"""Orders "{client_ids}" successfully placed. Transaction hash: {transaction_hash}."""
                )
            except Exception as exception:
                self.logger().debug(
                    f"""Placement of orders "{client_ids}" failed."""
                )

                raise exception

            if transaction_hash in (None, ""):
                raise RuntimeError(
                    f"""Placement of orders "{client_ids}" failed. Invalid transaction hash: "{transaction_hash}"."""
                )

        place_order_results = []
        for order_to_create, exchange_order_id in zip(orders_to_create, response["ids"]):
            order_to_create.exchange_order_id = None

            place_order_results.append(PlaceOrderResult(
                update_timestamp=time(),
                client_order_id=order_to_create.client_order_id,
                exchange_order_id=exchange_order_id,
                trading_pair=order_to_create.trading_pair,
                misc_updates={
                    "creation_transaction_hash": transaction_hash,
                },
                exception=None,
            ))

        self.logger().debug("batch_order_create: end")

        return place_order_results

    async def cancel_order(self, order: GatewayInFlightOrder) -> Tuple[bool, Optional[Dict[str, Any]]]:
        active_order = self._gateway_order_tracker.active_orders.get(order.client_order_id)

        if active_order and active_order.current_state != OrderState.CANCELED:
            self.logger().debug("cancel_order: start")

            self._check_markets_initialized() or await self._update_markets()

            await order.get_exchange_order_id()

            transaction_hash = None

            async with self._locks.cancel_order:
                try:
                    request = {
                        "connector": self._connector,
                        "chain": self._chain,
                        "network": self._network,
                        "trading_pair": order.trading_pair,
                        "address": self._owner_address,
                        "exchange_order_id": order.exchange_order_id,
                    }

                    self.logger().debug(f"""clob_cancel_order request:\n "{self._dump(request)}".""")

                    response = await self._gateway.clob_cancel_order(**request)

                    self.logger().debug(f"""clob_cancel_order response:\n "{self._dump(response)}".""")

                    transaction_hash = response["txHash"]

                    if transaction_hash in (None, ""):
                        raise Exception(
                            f"""Cancellation of order "{order.client_order_id}" / "{order.exchange_order_id}" failed. Invalid transaction hash: "{transaction_hash}"."""
                        )

                    self.logger().debug(
                        f"""Order "{order.client_order_id}" / "{order.exchange_order_id}" successfully cancelled. Transaction hash: "{transaction_hash}"."""
                    )
                except Exception as exception:
                    if 'No orders with the specified information exist' in str(exception.args):
                        self.logger().debug(
                            f"""Order "{order.client_order_id}" / "{order.exchange_order_id}" already cancelled."""
                        )

                        transaction_hash = "0000000000000000000000000000000000000000000000000000000000000000"  # noqa: mock
                    else:
                        self.logger().debug(
                            f"""Cancellation of order "{order.client_order_id}" / "{order.exchange_order_id}" failed."""
                        )

                        raise exception

            misc_updates = DotMap({
                "cancelation_transaction_hash": transaction_hash,
            }, _dynamic=False)

            self.logger().debug("cancel_order: end")

            order.current_state = OrderState.CANCELED

            return True, misc_updates
        return True, DotMap({}, _dynamic=False)

    async def batch_order_cancel(self, orders_to_cancel: List[GatewayInFlightOrder]) -> List[CancelOrderResult]:
        self.logger().debug("batch_order_cancel: start")

        self._check_markets_initialized() or await self._update_markets()

        client_ids = [order.client_order_id for order in orders_to_cancel]

        in_flight_orders_to_cancel = [
            self._gateway_order_tracker.fetch_tracked_order(client_order_id=order.client_order_id)
            for order in orders_to_cancel
        ]
        exchange_order_ids_to_cancel = await safe_gather(
            *[order.get_exchange_order_id() for order in in_flight_orders_to_cancel],
            return_exceptions=True,
        )
        found_orders_to_cancel = [
            order
            for order, result in zip(orders_to_cancel, exchange_order_ids_to_cancel)
            if not isinstance(result, asyncio.TimeoutError)
        ]

        ids = [order.exchange_order_id for order in found_orders_to_cancel]

        async with self._locks.cancel_orders:
            try:

                request = {
                    "connector": self._connector,
                    "chain": self._chain,
                    "network": self._network,
                    "address": self._owner_address,
                    "orders_to_create": [],
                    "orders_to_cancel": found_orders_to_cancel,
                }

                self.logger().debug(f"""clob_batch_order_modify request:\n "{self._dump(request)}".""")

                response = await self._gateway.clob_batch_order_modify(**request)

                self.logger().debug(f"""clob_batch_order_modify response:\n "{self._dump(response)}".""")

                transaction_hash = response["txHash"]

                self.logger().debug(
                    f"""Orders "{client_ids}" / "{ids}" successfully cancelled. Transaction hash(es): "{transaction_hash}"."""
                )
            except Exception as exception:
                self.logger().debug(
                    f"""Cancellation of orders "{client_ids}" / "{ids}" failed."""
                )

                raise exception

            if transaction_hash in (None, ""):
                raise RuntimeError(
                    f"""Cancellation of orders "{client_ids}" / "{ids}" failed. Invalid transaction hash: "{transaction_hash}"."""
                )

        cancel_order_results = []
        for order_to_cancel in orders_to_cancel:
            cancel_order_results.append(CancelOrderResult(
                client_order_id=order_to_cancel.client_order_id,
                trading_pair=order_to_cancel.trading_pair,
                misc_updates={
                    "cancelation_transaction_hash": transaction_hash
                },
                exception=None,
            ))

        self.logger().debug("batch_order_cancel: end")

        return cancel_order_results

    async def cancel_all_orders(self) -> List[CancellationResult]:
        self.logger().debug("cancel_all_orders: start")

        self._check_markets_initialized() or await self._update_markets()

        async with self._locks.cancel_all_orders:
            try:
                request = {
                    "trading_pair": self._trading_pair,
                    "chain": self._chain,
                    "network": self._network,
                    "connector": self._connector,
                    "address": self._owner_address,
                }

                response = await self._gateway.get_clob_order_status_updates(**request)

                orders = DotMap(response, _dynamic=False).orders

                orders_ids = [order.id for order in orders]

                request = {
                    "connector": self._connector,
                    "chain": self._chain,
                    "network": self._network,
                    "address": self._owner_address,
                    "orders_to_create": [],
                    "orders_to_cancel": orders_ids,
                }

                response = await self._gateway.clob_batch_order_modify(**request)

                self.logger().debug(f"""cancel_all_orders request:\n "{self._dump(request)}".""")

                transaction_hash = response["txHash"]

                self.logger().debug(
                    f"""Orders "{orders_ids}" successfully cancelled. Transaction hash(es): "{transaction_hash}"."""
                )
            except Exception as exception:
                self.logger().debug(
                    """Cancellation of all orders failed."""
                )

                raise exception

            if transaction_hash in (None, "") and orders_ids:
                raise RuntimeError(
                    f"""Cancellation of orders "{orders_ids}" failed. Invalid transaction hash: "{transaction_hash}"."""
                )

        cancel_order_results = []

        self.logger().debug("cancel_all_orders: end")

        return cancel_order_results

    async def get_last_traded_price(self, trading_pair: str) -> Decimal:
        self.logger().debug("get_last_traded_price: start")

        request = {
            "connector": self._connector,
            "chain": self._chain,
            "network": self._network,
            "trading_pair": self._trading_pair,
        }

        self.logger().debug(f"""get_clob_ticker request:\n "{self._dump(request)}".""")

        response = await self._gateway.get_clob_ticker(**request)

        self.logger().debug(f"""get_clob_ticker response:\n "{self._dump(response)}".""")

        ticker = DotMap(response, _dynamic=False).markets[self._trading_pair]

        ticker_price = Decimal(ticker.price)

        self.logger().debug("get_last_traded_price: end")

        return ticker_price

    async def get_order_book_snapshot(self, trading_pair: str) -> OrderBookMessage:
        self.logger().debug("get_order_book_snapshot: start")

        request = {
            "trading_pair": self._trading_pair,
            "connector": self._connector,
            "chain": self._chain,
            "network": self._network,
        }

        self.logger().debug(f"""get_clob_orderbook_snapshot request:\n "{self._dump(request)}".""")

        response = await self._gateway.get_clob_orderbook_snapshot(**request)

        self.logger().debug(f"""get_clob_orderbook_snapshot response:\n "{self._dump(response)}".""")

        order_book = DotMap(response, _dynamic=False)

        price_scale = 1
        size_scale = 1

        timestamp = time()

        bids = []
        asks = []
        for bid in order_book.bids:
            bids.append((Decimal(bid.price) * price_scale, Decimal(bid.quantity) * size_scale))

        for ask in order_book.asks:
            asks.append((Decimal(ask.price) * price_scale, Decimal(ask.quantity) * size_scale))

        snapshot = OrderBookMessage(
            message_type=OrderBookMessageType.SNAPSHOT,
            content={
                "trading_pair": trading_pair,
                "update_id": timestamp,
                "bids": bids,
                "asks": asks,
            },
            timestamp=timestamp
        )

        self.logger().debug("get_order_book_snapshot: end")

        return snapshot

    async def get_account_balances(self) -> Dict[str, Dict[str, Decimal]]:
        self.logger().debug("get_account_balances: start")

        request = {
            "chain": self._chain,
            "network": self._network,
            "address": self._owner_address,
            "connector": self._connector,
        }

        if self._trading_pair:
            request["token_symbols"] = [self._trading_pair.split("-")[0], self._trading_pair.split("-")[1], KUJIRA_NATIVE_TOKEN]
        else:
            request["token_symbols"] = []

        self.logger().debug(f"""get_balances request:\n "{self._dump(request)}".""")

        response = await self._gateway.get_balances(**request)

        self.logger().debug(f"""get_balances response:\n "{self._dump(response)}".""")

        balances = DotMap(response, _dynamic=False).balances

        hb_balances = {}
        for token, balance in balances.items():
            hb_balances[token] = DotMap({}, _dynamic=False)
            hb_balances[token]["total_balance"] = balance
            hb_balances[token]["available_balance"] = balance

        # self.logger().debug("get_account_balances: end")

        return hb_balances

    async def get_order_status_update(self, in_flight_order: GatewayInFlightOrder) -> OrderUpdate:

        active_order = self.gateway_order_tracker.active_orders.get(in_flight_order.client_order_id)

        if active_order:
            self.logger().debug("get_order_status_update: start")

            if active_order.current_state != OrderState.CANCELED:
                await in_flight_order.get_exchange_order_id()

                request = {
                    "trading_pair": self._trading_pair,
                    "chain": self._chain,
                    "network": self._network,
                    "connector": self._connector,
                    "address": self._owner_address,
                    "exchange_order_id": in_flight_order.exchange_order_id,
                }

                self.logger().debug(f"""get_clob_order_status_updates request:\n "{self._dump(request)}".""")

                response = await self._gateway.get_clob_order_status_updates(**request)

                self.logger().debug(f"""get_clob_order_status_updates response:\n "{self._dump(response)}".""")

                order = DotMap(response, _dynamic=False)["orders"][0]

                if order:
                    order_status = KujiraOrderStatus.to_hummingbot(KujiraOrderStatus.from_name(order.state))
                else:
                    order_status = in_flight_order.current_state

                open_update = OrderUpdate(
                    trading_pair=in_flight_order.trading_pair,
                    update_timestamp=time(),
                    new_state=order_status,
                    client_order_id=in_flight_order.client_order_id,
                    exchange_order_id=in_flight_order.exchange_order_id,
                    misc_updates={
                        "creation_transaction_hash": in_flight_order.creation_transaction_hash,
                        "cancelation_transaction_hash": in_flight_order.cancel_tx_hash,
                    },
                )
                self._publisher.trigger_event(event_tag=MarketEvent.OrderUpdate, message=open_update)

                self.logger().debug("get_order_status_update: end")

                return open_update

        no_update = OrderUpdate(
            trading_pair=in_flight_order.trading_pair,
            update_timestamp=time(),
            new_state=in_flight_order.current_state,
            client_order_id=in_flight_order.client_order_id,
            exchange_order_id=in_flight_order.exchange_order_id,
            misc_updates={
                "creation_transaction_hash": in_flight_order.creation_transaction_hash,
                "cancelation_transaction_hash": in_flight_order.cancel_tx_hash,
            },
        )
        self.logger().debug("get_order_status_update: end")
        return no_update

    async def get_all_order_fills(self, in_flight_order: GatewayInFlightOrder) -> List[TradeUpdate]:
        if in_flight_order.exchange_order_id:

            active_order = self.gateway_order_tracker.active_orders.get(in_flight_order.client_order_id)

            if active_order:
                if active_order.current_state != OrderState.CANCELED:
                    self.logger().debug("get_all_order_fills: start")

                    trade_update = None

                    request = {
                        "trading_pair": self._trading_pair,
                        "chain": self._chain,
                        "network": self._network,
                        "connector": self._connector,
                        "address": self._owner_address,
                        "exchange_order_id": in_flight_order.exchange_order_id,
                    }

                    self.logger().debug(f"""get_clob_order_status_updates request:\n "{self._dump(request)}".""")

                    response = await self._gateway.get_clob_order_status_updates(**request)

                    self.logger().debug(f"""get_clob_order_status_updates response:\n "{self._dump(response)}".""")

                    order = DotMap(response, _dynamic=False)["orders"][0]

                    if order:
                        order_status = KujiraOrderStatus.to_hummingbot(KujiraOrderStatus.from_name(order.state))
                    else:
                        order_status = in_flight_order.current_state

                    if order and order_status == OrderState.FILLED:
                        timestamp = time()
                        trade_id = str(timestamp)

                        trade_update = TradeUpdate(
                            trade_id=trade_id,
                            client_order_id=in_flight_order.client_order_id,
                            exchange_order_id=in_flight_order.exchange_order_id,
                            trading_pair=in_flight_order.trading_pair,
                            fill_timestamp=timestamp,
                            fill_price=in_flight_order.price,
                            fill_base_amount=in_flight_order.amount,
                            fill_quote_amount=in_flight_order.price * in_flight_order.amount,
                            fee=TradeFeeBase.new_spot_fee(
                                fee_schema=TradeFeeSchema(),
                                trade_type=in_flight_order.trade_type,
                                flat_fees=[TokenAmount(
                                    amount=Decimal(self._market.fees.taker),
                                    token=self._market.quoteToken.symbol
                                )]
                            ),
                        )

                    self.logger().debug("get_all_order_fills: end")

                    if trade_update:
                        return [trade_update]

        return []

    def is_order_not_found_during_status_update_error(self, status_update_exception: Exception) -> bool:
        self.logger().debug("is_order_not_found_during_status_update_error: start")

        output = str(status_update_exception).startswith("No update found for order")

        self.logger().debug("is_order_not_found_during_status_update_error: end")

        return output

    def is_order_not_found_during_cancelation_error(self, cancelation_exception: Exception) -> bool:
        self.logger().debug("is_order_not_found_during_cancelation_error: start")

        output = False

        self.logger().debug("is_order_not_found_during_cancelation_error: end")

        return output

    async def check_network_status(self) -> NetworkStatus:
        # self.logger().debug("check_network_status: start")

        try:
            await self._gateway.ping_gateway()

            output = NetworkStatus.CONNECTED
        except asyncio.CancelledError:
            raise
        except Exception as exception:
            self.logger().error(exception)

            output = NetworkStatus.NOT_CONNECTED

        # self.logger().debug("check_network_status: end")

        return output

    @property
    def is_cancel_request_in_exchange_synchronous(self) -> bool:
        self.logger().debug("is_cancel_request_in_exchange_synchronous: start")

        output = True

        self.logger().debug("is_cancel_request_in_exchange_synchronous: end")

        return output

    def _check_markets_initialized(self) -> bool:
        # self.logger().debug("_check_markets_initialized: start")

        output = self._markets is not None and bool(self._markets)

        # self.logger().debug("_check_markets_initialized: end")

        return output

    async def _update_markets(self):
        self.logger().debug("_update_markets: start")

        request = {
            "connector": self._connector,
            "chain": self._chain,
            "network": self._network,
        }

        if self._trading_pair:
            request["trading_pair"] = self._trading_pair

        self.logger().debug(f"""get_clob_markets request:\n "{self._dump(request)}".""")

        response = await self._gateway.get_clob_markets(**request)

        self.logger().debug(f"""get_clob_markets response:\n "{self._dump(response)}".""")

        self._markets = DotMap(response, _dynamic=False).markets

        if self._trading_pair:
            self._market = self._markets[self._trading_pair]

        self.logger().debug("_update_markets: end")

        self._markets_info.clear()
        for market in self._markets.values():
            market["hb_trading_pair"] = convert_market_name_to_hb_trading_pair(market.name)

            self._markets_info[market["hb_trading_pair"]] = market

        return self._markets

    def _parse_trading_rule(self, trading_pair: str, market_info: Any) -> TradingRule:
        self.logger().debug("_parse_trading_rule: start")

        trading_rule = TradingRule(
            trading_pair=trading_pair,
            min_order_size=Decimal(market_info.minimumOrderSize),
            min_price_increment=Decimal(market_info.minimumPriceIncrement),
            min_base_amount_increment=Decimal(market_info.minimumBaseAmountIncrement),
            min_quote_amount_increment=Decimal(market_info.minimumQuoteAmountIncrement),
        )

        self.logger().debug("_parse_trading_rule: end")

        return trading_rule

    def _get_exchange_trading_pair_from_market_info(self, market_info: Any) -> str:
        self.logger().debug("_get_exchange_trading_pair_from_market_info: start")

        output = market_info.id

        self.logger().debug("_get_exchange_trading_pair_from_market_info: end")

        return output

    def _get_maker_taker_exchange_fee_rates_from_market_info(self, market_info: Any) -> MakerTakerExchangeFeeRates:
        self.logger().debug("_get_maker_taker_exchange_fee_rates_from_market_info: start")

        fee_scaler = Decimal("1") - Decimal(market_info.fees.serviceProvider)
        maker_fee = Decimal(market_info.fees.maker) * fee_scaler
        taker_fee = Decimal(market_info.fees.taker) * fee_scaler

        output = MakerTakerExchangeFeeRates(
            maker=maker_fee,
            taker=taker_fee,
            maker_flat_fees=[],
            taker_flat_fees=[]
        )

        self.logger().debug("_get_maker_taker_exchange_fee_rates_from_market_info: end")

        return output

    async def _update_markets_loop(self):
        self.logger().debug("_update_markets_loop: start")

        while True:
            self.logger().debug("_update_markets_loop: start loop")

            await self._update_markets()
            await asyncio.sleep(MARKETS_UPDATE_INTERVAL)

            self.logger().debug("_update_markets_loop: end loop")

    async def cancel_all(self, _timeout_seconds: float) -> List[CancellationResult]:
        return await self.cancel_all_orders()

    async def _check_if_order_failed_based_on_transaction(
        self,
        transaction: Any,
        order: GatewayInFlightOrder
    ) -> bool:
        order_id = await order.get_exchange_order_id()

        return order_id.lower() not in transaction.data.lower()

    @staticmethod
    def _dump(target: Any):
        try:
            return jsonpickle.encode(target, unpicklable=True, indent=2)
        except (Exception,):
            return target
