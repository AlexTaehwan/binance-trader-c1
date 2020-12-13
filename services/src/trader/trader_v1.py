import os
import gc
import time
import ccxt
import requests
import urllib3
import joblib
import pandas as pd
import numpy as np
from dataclasses import dataclass

from config import CFG
from trainer.models import PredictorV1
from database.usecase import Usecase
from exchange.custom_client import CustomClient, API_REQUEST_DELAY
from dataset_builder.build_dataset_v1 import (
    _build_feature_by_rawdata,
    preprocess_features,
)
from .utils import nan_to_zero
from logging import getLogger
from common_utils_svc import initialize_trader_logger, Position


logger = getLogger("trader")
initialize_trader_logger()

LAST_ENTRY_AT_FILE_PATH = "/app/storage/trader/last_entry_at.pkl"


@dataclass
class TraderV1:
    usecase = Usecase()
    possible_in_debt = False
    commission = {"entry": 0.0004, "exit": 0.0002, "spread": 0.0004}
    skip_executable_order_check = True  # To prevent api limitation

    def __post_init__(self):
        self.custom_cli = CustomClient()
        self.target_coins = pd.Index(self.custom_cli.target_coins)

        self._set_params()
        self._set_test_params()
        self._build_model()
        self._build_scaler()
        self._load_last_entry_at()
        self._initialize_order_books()

        self.cached_pricing = None

        if self.skip_executable_order_check is True:
            assert self.order_criterion == "capital"

    def _set_params(self):
        # Set params which has dependency on trader logic
        self.base_currency = CFG.REPORT_PARAMS["base_currency"]
        self.position_side = CFG.REPORT_PARAMS["position_side"]
        self.entry_ratio = CFG.REPORT_PARAMS["entry_ratio"]
        self.min_holding_minutes = CFG.REPORT_PARAMS["min_holding_minutes"]
        self.max_holding_minutes = CFG.REPORT_PARAMS["max_holding_minutes"]
        self.compound_interest = CFG.REPORT_PARAMS["compound_interest"]
        self.order_criterion = CFG.REPORT_PARAMS["order_criterion"]
        self.exit_if_achieved = CFG.REPORT_PARAMS["exit_if_achieved"]
        self.achieve_ratio = CFG.REPORT_PARAMS["achieve_ratio"]
        self.achieved_with_commission = CFG.REPORT_PARAMS["achieved_with_commission"]
        self.max_n_updated = CFG.REPORT_PARAMS["max_n_updated"]

        # Currently we accept only 0
        assert self.max_n_updated == 0

        self.exit_q_threshold = CFG.REPORT_PARAMS["exit_q_threshold"]
        self.entry_qay_threshold = CFG.REPORT_PARAMS["entry_qay_threshold"]
        self.entry_qby_threshold = CFG.REPORT_PARAMS["entry_qby_threshold"]
        self.entry_qay_prob_threshold = CFG.REPORT_PARAMS["entry_qay_prob_threshold"]
        self.entry_qby_prob_threshold = CFG.REPORT_PARAMS["entry_qby_prob_threshold"]
        self.sum_probs_above_threshold = CFG.REPORT_PARAMS["sum_probs_above_threshold"]

        self.bins = CFG.BINS
        self.n_bins = CFG.DATASET_PARAMS["n_bins"]

        # Set data builder params
        self.data_builder_params = {}
        self.data_builder_params["features_columns"] = [
            (column[0].replace("-", "/"), column[1])
            for column in CFG.DATASET_PARAMS["features_columns"]
        ]
        self.data_builder_params["base_feature_assets"] = [
            base_feature_asset.replace("-", "/")
            for base_feature_asset in CFG.EXP_DATA_PARAMS["base_feature_assets"]
        ]
        self.data_builder_params["winsorize_threshold"] = CFG.EXP_DATA_PARAMS[
            "winsorize_threshold"
        ]
        self.data_builder_params["asset_to_id"] = {
            key.replace("-", "/"): value
            for key, value in CFG.EXP_PARAMS["asset_to_id"].items()
        }
        self.data_builder_params["id_to_asset"] = {
            value: key.replace("-", "/")
            for key, value in CFG.EXP_PARAMS["asset_to_id"].items()
        }

    def _set_test_params(self):
        if CFG.TEST_MODE is True:
            assert self.custom_cli.test_mode is True

            self.entry_ratio = 0.0001
            self.exit_q_threshold = 8
            self.entry_qay_threshold = 8
            self.entry_qby_threshold = 8
            self.entry_qay_prob_threshold = 0.0
            self.entry_qby_prob_threshold = 0.0
            self.sum_probs_above_threshold = True

    def _build_model(self):
        self.model = PredictorV1(
            exp_dir=CFG.EXP_DIR,
            m_config=CFG.EXP_MODEL_PARAMS,
            d_config=CFG.EXP_DATA_PARAMS,
            device="cpu",
            mode="predict",
        )

    def _build_scaler(self):
        self.scaler = joblib.load(os.path.join(CFG.EXP_DIR, "scaler.pkl"))

    def _store_last_entry_at(self):
        joblib.dump(self.last_entry_at, LAST_ENTRY_AT_FILE_PATH)

    def _load_last_entry_at(self):
        if os.path.exists(LAST_ENTRY_AT_FILE_PATH):
            self.last_entry_at = joblib.load(LAST_ENTRY_AT_FILE_PATH)
            logger.info(f"[O] Info: loaded last_entry_at")
        else:
            self.last_entry_at = {key: None for key in self.target_coins}

        # Initialize
        positions = self.custom_cli.get_position_objects(with_entry_at=True)
        for position in positions:
            self.last_entry_at[position.asset] = max(
                position.entry_at, self.last_entry_at[position.asset]
            )

    def _initialize_order_books(self):
        positions = self.custom_cli.get_position_objects(with_entry_at=False)

        for position in positions:
            orders = self.custom_cli.get_open_orders(symbol=position.asset)

            # When already limit order exists, we skip it.
            if len(orders) >= 1:
                continue

            assert position.entry_price != 0.0
            self.custom_cli.exit_order(
                symbol=position.asset,
                order_type="limit",
                position=position.side,
                amount=position.qty,
                price=self.compute_price_to_achieve(
                    position=position, entry_price=position.entry_price
                ),
            )

        logger.info(f"[O] Info: initialized order books")

    def _build_features(self, pricing):
        features = {}
        for target_coin in self.target_coins:
            rawdata = pricing.xs(target_coin, axis=0, level=1).sort_index()
            features[target_coin] = _build_feature_by_rawdata(rawdata=rawdata)

        features = pd.concat(features, axis=1).sort_index()[
            self.data_builder_params["features_columns"]
        ]

        features = preprocess_features(features=features, scaler=self.scaler)

        return features

    def _build_inputs(self, features):
        base_features = features[self.data_builder_params["base_feature_assets"]]
        features = features[self.target_coins]

        inputs = []
        for target_coin in self.target_coins:
            to_input = pd.concat([base_features, features[target_coin]], axis=1).astype(
                "float32"
            )

            to_input = np.swapaxes(to_input.values, 0, 1)

            if self.data_builder_params["winsorize_threshold"] is not None:
                to_input = to_input.clip(
                    -self.data_builder_params["winsorize_threshold"],
                    self.data_builder_params["winsorize_threshold"],
                )

            inputs.append(to_input)

        inputs = np.stack(inputs, axis=0)
        ids = [
            self.data_builder_params["asset_to_id"][target_coin]
            for target_coin in self.target_coins
        ]

        return inputs, ids

    def build_prediction_dict(self, last_sync_on):
        query_start_on = last_sync_on - pd.Timedelta(
            minutes=(1320 + CFG.EXP_MODEL_PARAMS["lookback_window"] - 1)
        )
        query_end_on = last_sync_on

        if self.cached_pricing is None:
            pricing = self.usecase.get_pricing(
                start_on=query_start_on, end_on=query_end_on
            )
        else:
            # Get extra 1 candle, cause it has potential to be changed.
            pricing = self.usecase.get_pricing(
                start_on=self.cached_pricing.index.levels[0][-1], end_on=query_end_on
            )
            pricing = pd.concat(
                [
                    self.cached_pricing[
                        query_start_on : self.cached_pricing.index.levels[0][-2]
                    ],
                    pricing,
                ]
            ).sort_index()

        self.cached_pricing = pricing

        features = self._build_features(pricing=pricing)
        inputs, ids = self._build_inputs(features=features)

        pred_dict = self.model.predict(
            X=inputs, id=ids, id_to_asset=self.data_builder_params["id_to_asset"]
        )

        return pred_dict

    def build_positive_and_negative_assets(self, pred_dict):
        if self.sum_probs_above_threshold is True:
            positive_qay_probability = pred_dict["qay_probability"][
                pred_dict["qay_probability"].index.get_level_values(1)
                >= self.entry_qay_threshold
            ].sum(axis=0, level=0)
            positive_qby_probability = pred_dict["qby_probability"][
                pred_dict["qby_probability"].index.get_level_values(1)
                >= self.entry_qby_threshold
            ].sum(axis=0, level=0)
            negative_qay_probability = pred_dict["qay_probability"][
                pred_dict["qay_probability"].index.get_level_values(1)
                <= (self.n_bins - 1) - self.entry_qay_threshold
            ].sum(axis=0, level=0)
            negative_qby_probability = pred_dict["qby_probability"][
                pred_dict["qby_probability"].index.get_level_values(1)
                <= (self.n_bins - 1) - self.entry_qby_threshold
            ].sum(axis=0, level=0)
        else:
            positive_qay_probability = pred_dict["qay_probability"].xs(
                self.entry_qay_threshold, axis=0, level=1
            )
            positive_qby_probability = pred_dict["qby_probability"].xs(
                self.entry_qby_threshold, axis=0, level=1
            )
            negative_qay_probability = pred_dict["qay_probability"].xs(
                (self.n_bins - 1) - self.entry_qay_threshold, axis=0, level=1
            )
            negative_qby_probability = pred_dict["qby_probability"].xs(
                (self.n_bins - 1) - self.entry_qby_threshold, axis=0, level=1
            )

        # Set assets which has signals
        positive_mask = (
            (pred_dict["qay_prediction"] >= self.entry_qay_threshold)
            & (pred_dict["qby_prediction"] >= self.entry_qby_threshold)
            & (positive_qay_probability >= self.entry_qay_prob_threshold)
            & (positive_qby_probability >= self.entry_qby_prob_threshold)
        )

        negative_mask = (
            (
                pred_dict["qay_prediction"]
                <= (self.n_bins - 1) - self.entry_qay_threshold
            )
            & (
                pred_dict["qby_prediction"]
                <= (self.n_bins - 1) - self.entry_qby_threshold
            )
            & (negative_qay_probability >= self.entry_qay_prob_threshold)
            & (negative_qby_probability >= self.entry_qby_prob_threshold)
        )

        positive_assets = self.target_coins[positive_mask].tolist()
        negative_assets = self.target_coins[negative_mask].tolist()

        return positive_assets, negative_assets

    def is_executable(self, last_sync_on: pd.Timestamp, now: pd.Timestamp):
        if last_sync_on is None:
            return False

        sync_min_delta = int((now - last_sync_on).total_seconds() // 60)

        if sync_min_delta == 1:
            last_trade_on = self.usecase.get_last_trade_on()
            if last_trade_on is None:
                return True
            else:
                if int((now - last_trade_on).total_seconds() // 60) >= 1:
                    return True

        return False

    def exit_order(self, position):
        self.custom_cli.cancel_orders(symbol=position.asset)
        time.sleep(API_REQUEST_DELAY)

        ordered = self.custom_cli.exit_order(
            symbol=position.asset,
            order_type="market",
            position=position.side,
            amount=position.qty,
        )
        if ordered is None:
            assert CFG.TEST_MODE is True
            return

    def handle_exit(self, positions, positive_assets, negative_assets, now):
        for position_idx, position in enumerate(positions):
            # Keep position if matched
            if (position.side == "long") and (position.asset in positive_assets):
                continue

            if (position.side == "short") and (position.asset in negative_assets):
                continue

            position_entry_at = self.last_entry_at[position.asset]
            passed_minutes = (now - position_entry_at).total_seconds() // 60

            # Handle min_holding_minutes
            if passed_minutes <= self.min_holding_minutes:
                continue

            # Handle max_holding_minutes
            if passed_minutes >= self.max_holding_minutes:
                self.exit_order(position=position)
                positions[position_idx].is_exited = True
                logger.info(f"[-] Exit: {str(position)}, max_holding")
                continue

            # Handle exit signal
            if (position.side == "long") and (position.asset in negative_assets):
                self.exit_order(position=position)
                positions[position_idx].is_exited = True
                logger.info(f"[-] Exit: {str(position)}, opposite")
                continue

            if (position.side == "short") and (position.asset in positive_assets):
                self.exit_order(position=position)
                positions[position_idx].is_exited = True
                logger.info(f"[-] Exit: {str(position)}, opposite")
                continue

        # Delete exited positions
        positions = [
            position for position in positions if position.is_exited is not True
        ]

        return positions

    def check_if_opposite_position_exists(self, positions, order_asset, order_side):
        if order_side == "long":
            opposite_side = "short"
        if order_side == "short":
            opposite_side = "long"

        for exist_position in positions:
            if (exist_position.asset == order_asset) and (
                exist_position.side == opposite_side
            ):
                return True

        return False

    def compute_cost_to_order(self, position):
        cache_to_order = position.entry_price * position.qty
        commission_to_order = cache_to_order * (
            self.commission["entry"] + self.commission["spread"]
        )

        return cache_to_order + commission_to_order

    def check_if_already_have(self, positions, position):
        for exist_position in positions:
            if (exist_position.asset == position.asset) and (
                exist_position.side == position.side
            ):
                return True

        return False

    def check_if_executable_order(self, position):
        if self.skip_executable_order_check is True:
            is_enough_ammount = bool(
                position.qty >= self.custom_cli.ammount_constraints[position.asset]
            )
            return is_enough_ammount

        cache = self.custom_cli.get_cache_dict()["free"]
        cost = self.compute_cost_to_order(position=position)

        is_enough_cache = bool((cache - cost) >= 0)
        is_enough_ammount = bool(
            position.qty >= self.custom_cli.ammount_constraints[position.asset]
        )

        return is_enough_cache & is_enough_ammount

    def compute_price_to_achieve(self, position, entry_price):
        commission = self.commission
        if self.achieved_with_commission is not True:
            commission["entry"] = 0
            commission["exit"] = 0
            commission["spread"] = 0

        if position.side == "long":
            bin_value = self.bins[1:-1][position.asset][self.exit_q_threshold]
            price_to_achieve = (
                entry_price
                * (
                    (bin_value * self.achieve_ratio)
                    + 1
                    + (commission["entry"] + commission["spread"])
                )
                / (1 - (commission["exit"] + commission["spread"]))
            )

        if position.side == "short":
            bin_value = self.bins[1:-1][position.asset][
                (self.n_bins - self.exit_q_threshold)
            ]
            price_to_achieve = (
                entry_price
                * (
                    (bin_value * self.achieve_ratio)
                    + 1
                    - (commission["entry"] + commission["spread"])
                )
                / (1 + (commission["exit"] + commission["spread"]))
            )

        return price_to_achieve

    def entry_order(self, positions, asset, side, cache_to_order, pricing, now):
        if cache_to_order == 0:
            return

        # if opposite position exists, we dont entry
        if (
            self.check_if_opposite_position_exists(
                positions=positions, order_asset=asset, order_side=side
            )
            is True
        ):
            return

        entry_price = pricing[asset]
        qty = cache_to_order / entry_price

        position = Position(
            asset=asset, side=side, qty=qty, entry_price=entry_price, entry_at=now
        )

        # Currently update_position_if_already_have is not supported.
        already_have = self.check_if_already_have(
            positions=positions, position=position
        )
        if already_have is True:
            self.last_entry_at[position.asset] = now
            return

        executable_order = self.check_if_executable_order(position=position)
        if executable_order is True:
            ordered = self.custom_cli.entry_order(
                symbol=position.asset,
                order_type="market",
                position=position.side,
                amount=position.qty,
            )
            if ordered is None:
                assert CFG.TEST_MODE is True
                return

            self.last_entry_at[position.asset] = now

            if self.exit_if_achieved is True:
                self.assets_to_limit_order.append(position.asset)

            logger.info(f"[+] Entry: {str(position)}")

    def handle_entry(
        self, positions, cache_to_order, positive_assets, negative_assets, pricing, now
    ):
        # Set init to handle limit order
        self.assets_to_limit_order = []

        # Entry order
        if self.position_side in ("long", "longshort"):
            for order_asset in positive_assets:
                self.entry_order(
                    positions=positions,
                    asset=order_asset,
                    side="long",
                    cache_to_order=cache_to_order,
                    pricing=pricing,
                    now=now,
                )

        if self.position_side in ("short", "longshort"):
            for order_asset in negative_assets:
                self.entry_order(
                    positions=positions,
                    asset=order_asset,
                    side="short",
                    cache_to_order=cache_to_order,
                    pricing=pricing,
                    now=now,
                )

        # Limit order
        if len(self.assets_to_limit_order) > 0:
            time.sleep(API_REQUEST_DELAY)
            positions = self.custom_cli.get_position_objects(with_entry_at=False)

            for position in positions:
                if position.asset not in self.assets_to_limit_order:
                    continue

                assert position.entry_price != 0.0
                self.custom_cli.exit_order(
                    symbol=position.asset,
                    order_type="limit",
                    position=position.side,
                    amount=position.qty,
                    price=self.compute_price_to_achieve(
                        position=position, entry_price=position.entry_price
                    ),
                )

    def run(self):
        logger.info(f"[O] Start: demon of trader")
        n_traded = 0

        while True:
            # Handle relogin
            if n_traded == 60:
                self.custom_cli = CustomClient()
                n_traded = 0

            # Main
            try:
                # Use timestamp without second info
                now = pd.Timestamp.utcnow().floor("T")
                last_sync_on = self.usecase.get_last_sync_on()

                if self.is_executable(last_sync_on=last_sync_on, now=now) is True:
                    pred_dict = self.build_prediction_dict(last_sync_on=last_sync_on)
                    (
                        positive_assets,
                        negative_assets,
                    ) = self.build_positive_and_negative_assets(pred_dict=pred_dict)

                    # Handle exit
                    positions = self.custom_cli.get_position_objects(
                        with_entry_at=False
                    )
                    positions = self.handle_exit(
                        positions=positions,
                        positive_assets=positive_assets,
                        negative_assets=negative_assets,
                        now=now,
                    )
                    long_positions = [
                        position for position in positions if position.side == "long"
                    ]
                    short_positions = [
                        position for position in positions if position.side == "short"
                    ]

                    # Compute how much use cache to order
                    cache_dict = self.custom_cli.get_cache_dict()
                    capital = cache_dict["total"]
                    cache = cache_dict["free"]

                    logger.info(
                        f"[_] Capital: {capital:.2f}$ | Holds: long({len(long_positions)}), short({len(short_positions)}) | Signals: pos({len(positive_assets)}), neg({len(negative_assets)})"
                    )

                    if self.compound_interest is False:
                        cache_to_order = self.entry_ratio
                    else:
                        if self.order_criterion == "cache":
                            if cache > 0:
                                cache_to_order = nan_to_zero(
                                    value=(cache * self.entry_ratio)
                                )
                            else:
                                cache_to_order = 0

                        elif self.order_criterion == "capital":
                            # Entry with capital base
                            cache_to_order = nan_to_zero(
                                value=(capital * self.entry_ratio)
                            )

                    # Handle entry
                    pricing = self.custom_cli.get_last_pricing()
                    self.handle_entry(
                        positions=positions,
                        cache_to_order=cache_to_order,
                        positive_assets=positive_assets,
                        negative_assets=negative_assets,
                        pricing=pricing,
                        now=now,
                    )

                    # Record traded
                    self.usecase.insert_trade({"timestamp": now})
                    self._store_last_entry_at()

                    n_traded += 1
                else:
                    time.sleep(0.2)

            except Exception as e:
                logger.error("[!] Error: ", exc_info=True)
                raise Exception


if __name__ == "__main__":
    import fire

    fire.Fire(TraderV1)
