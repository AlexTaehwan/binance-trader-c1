import os
import json
from glob import glob
import pandas as pd
import numpy as np
from tqdm import tqdm
from functools import partial
from itertools import combinations
from sklearn import preprocessing
import joblib
from common_utils import make_dirs
from joblib import Parallel, delayed
from typing import Callable, List, Dict


CONFIG = {
    "rawdata_dir": "../../storage/dataset/rawdata/csv/",
    "data_store_dir": "../../storage/dataset/dataset_10m_v1/",
    "lookahead_window": 10,
    "q_threshold": 9,
    "train_ratio": 0.7,
    "scaler_type": "RobustScaler",
    "column_pairs": [("close", "high"), ("close", "low")],
    "n_jobs": 4,
}
COLUMNS = ["open", "high", "low", "close"]


def parallelize(func: Callable, params: List[Dict], n_jobs: int = 64) -> List:
    outputs = []

    for param in params:

        outputs.append(delayed(func)(**param))

    outputs = Parallel(n_jobs=n_jobs, verbose=1)(outputs)

    return outputs


def load_rawdata(file_name):
    rawdata = pd.read_csv(file_name, header=0, index_col=0)[COLUMNS]
    rawdata.index = pd.to_datetime(rawdata.index)

    return rawdata


def _build_feature_by_rawdata(rawdata):
    returns = (
        rawdata.pct_change(1, fill_method=None)
        .iloc[1:]
        .rename(columns={key: key + "_return" for key in COLUMNS})
    )

    inner_changes = []
    for column_pair in sorted(list(combinations(COLUMNS, 2))):
        inner_changes.append(
            rawdata[list(column_pair)]
            .pct_change(1, axis=1, fill_method=None)[column_pair[-1]]
            .rename("_".join(column_pair) + "_change")
        )

    inner_changes = pd.concat(inner_changes, axis=1).reindex(returns.index)

    return pd.concat([returns, inner_changes], axis=1).sort_index()


def _build_fwd_returns_by_rawdata(
    rawdata, lookahead_window, column_pairs=[("close", "high"), ("close", "low")]
):
    fwd_returns = []
    for column_pair in column_pairs:
        partial_fwd_returns = []
        for window in range(1, lookahead_window + 1):
            colum_pair_df = rawdata[list(column_pair)].copy().sort_index()
            colum_pair_df.columns = [0, 1]

            colum_pair_df[1] = colum_pair_df[1].shift(-window)
            partial_fwd_return = colum_pair_df.pct_change(1, axis=1, fill_method=None)[
                1
            ].rename(f"fwd_return({window})")
            partial_fwd_returns.append(partial_fwd_return)

        partial_fwd_returns = pd.concat(partial_fwd_returns, axis=1).sort_index()
        partial_fwd_returns.columns = [
            "_".join(column_pair) + "_" + column
            for column in partial_fwd_returns.columns
        ]
        fwd_returns.append(partial_fwd_returns)

    return pd.concat(fwd_returns, axis=1).sort_index()


def _build_bins(rawdata, lookahead_window):
    column_pair = ("close", "close")

    # build fwd_return(window)
    colum_pair_df = rawdata[list(column_pair)].copy().sort_index()
    colum_pair_df.columns = [0, 1]

    colum_pair_df[1] = colum_pair_df[1].shift(-lookahead_window)
    fwd_return = (
        colum_pair_df.pct_change(1, axis=1, fill_method=None)[1]
        .rename(f"fwd_return({lookahead_window})")
        .sort_index()
    )

    _, bins = pd.qcut(
        fwd_return[fwd_return != 0].dropna(), 10, retbins=True, labels=False
    )
    bins = np.concatenate([[-np.inf], bins[1:-1], [np.inf]])

    return bins


def _build_label_by_rawdata(rawdata, lookahead_window, q_threshold, column_pairs):
    fwd_returns = _build_fwd_returns_by_rawdata(
        rawdata=rawdata, lookahead_window=lookahead_window, column_pairs=column_pairs
    )
    bins = _build_bins(rawdata=rawdata, lookahead_window=lookahead_window)

    quantile_df = fwd_returns.dropna().apply(
        lambda x: x.apply(partial(compute_quantile, bins=bins))
    )

    total_positive_moving = (quantile_df >= q_threshold).any(axis=1)
    total_negative_moving = (quantile_df <= 9 - q_threshold).any(axis=1)
    static_moving = ~total_positive_moving & ~total_negative_moving

    positive_negative_moving = total_positive_moving & total_negative_moving
    positive_moving = total_positive_moving & ~positive_negative_moving
    negative_moving = total_negative_moving & ~positive_negative_moving

    # (0: Increases, 1: Decreases, 2: (Increases, Decreases), 3: Static)
    label = (
        pd.concat(
            [
                positive_moving[positive_moving].astype(int) * 0,
                negative_moving[negative_moving].astype(int) * 1,
                positive_negative_moving[positive_negative_moving].astype(int) * 2,
                static_moving[static_moving].astype(int) * 3,
            ]
        )
        .rename("label")
        .sort_index()
    )

    assert not any(label.index.duplicated())

    return label


def compute_quantile(x, bins):
    for idx in range(len(bins) - 1):
        if bins[idx] < x <= bins[idx + 1]:
            return idx

    raise RuntimeError("unreachable")


def build_features(file_names, n_jobs=1):
    def __build_feature(file_name):
        coin_pair = file_name.split("/")[-1].split(".")[0]

        rawdata = load_rawdata(file_name=file_name)
        feature = _build_feature_by_rawdata(rawdata=rawdata)
        feature.columns = sorted([(coin_pair, column) for column in feature.columns])
        return feature

    features = parallelize(
        func=__build_feature,
        params=[{"file_name": file_name} for file_name in file_names],
        n_jobs=n_jobs,
    )
    features = pd.concat(features, axis=1).dropna().sort_index()
    features.columns = range(features.shape[1])

    return features


def build_labels(file_names, lookahead_window, q_threshold, column_pairs, n_jobs=1):
    def __build_label(file_name, lookahead_window, q_threshold, column_pairs):
        coin_pair = file_name.split("/")[-1].split(".")[0]

        rawdata = load_rawdata(file_name=file_name)
        return _build_label_by_rawdata(
            rawdata=rawdata,
            lookahead_window=lookahead_window,
            q_threshold=q_threshold,
            column_pairs=column_pairs,
        ).rename(coin_pair)

    labels = parallelize(
        func=__build_label,
        params=[
            {
                "file_name": file_name,
                "lookahead_window": lookahead_window,
                "q_threshold": q_threshold,
                "column_pairs": column_pairs,
            }
            for file_name in file_names
        ],
        n_jobs=n_jobs,
    )

    return pd.concat(labels, axis=1).dropna().sort_index()


def build_pricing(file_names, n_jobs=1):
    def __build_pricing(file_name):
        coin_pair = file_name.split("/")[-1].split(".")[0]
        return load_rawdata(file_name=file_name)["close"].rename(coin_pair)

    pricing = parallelize(
        func=__build_pricing,
        params=[{"file_name": file_name} for file_name in file_names],
        n_jobs=n_jobs,
    )

    return pd.concat(pricing, axis=1).dropna().sort_index()


def build_scaler(features, scaler_type):
    scaler = getattr(preprocessing, scaler_type)()
    scaler.fit(features[features != 0])

    return scaler


def build_all_bins(file_names, lookahead_window, n_jobs=1):
    def __build_all_bin(file_name, lookahead_window):
        rawdata = load_rawdata(file_name=file_name)
        return _build_bins(rawdata=rawdata, lookahead_window=lookahead_window)

    all_bins = parallelize(
        func=__build_all_bin,
        params=[
            {"file_name": file_name, "lookahead_window": lookahead_window}
            for file_name in file_names
        ],
        n_jobs=n_jobs,
    )
    all_bins = {
        file_name.split("/")[-1].split(".")[0]: all_bin
        for file_name, all_bin in zip(file_names, all_bins)
    }

    return pd.DataFrame(all_bins)


def preprocess_features(features, scaler):
    index = features.index
    columns = features.columns

    processed_features = pd.DataFrame(
        scaler.transform(features), index=index, columns=columns
    )

    return processed_features


def store_artifacts(
    features, labels, pricing, scaler, bins, train_ratio, params, data_store_dir
):
    # Make dirs
    train_data_store_dir = os.path.join(data_store_dir, "train")
    test_data_store_dir = os.path.join(data_store_dir, "test")
    make_dirs([train_data_store_dir, test_data_store_dir])

    # Store
    boundary_index = int(len(features.index) * train_ratio)
    features.iloc[:boundary_index].to_csv(
        os.path.join(train_data_store_dir, "X.csv"), compression="gzip"
    )
    features.iloc[boundary_index:].to_csv(
        os.path.join(test_data_store_dir, "X.csv"), compression="gzip"
    )

    labels.iloc[:boundary_index].to_csv(
        os.path.join(train_data_store_dir, "Y.csv"), compression="gzip"
    )
    labels.iloc[boundary_index:].to_csv(
        os.path.join(test_data_store_dir, "Y.csv"), compression="gzip"
    )

    pricing.iloc[:boundary_index].to_csv(
        os.path.join(train_data_store_dir, "pricing.csv"), compression="gzip"
    )
    pricing.iloc[boundary_index:].to_csv(
        os.path.join(test_data_store_dir, "pricing.csv"), compression="gzip"
    )

    joblib.dump(scaler, os.path.join(data_store_dir, "scaler.pkl"))
    bins.to_csv(os.path.join(data_store_dir, "bins.csv"))

    with open(os.path.join(data_store_dir, "tradable_coins.txt"), "w") as f:
        f.write("\n".join(pricing.columns.tolist()))

    with open(os.path.join(data_store_dir, "params.json"), "w") as f:
        json.dump(params, f)

    print(f"[+] Artifacts are stored")


def build_dataset(
    rawdata_dir=CONFIG["rawdata_dir"],
    data_store_dir=CONFIG["data_store_dir"],
    lookahead_window=CONFIG["lookahead_window"],
    q_threshold=CONFIG["q_threshold"],
    train_ratio=CONFIG["train_ratio"],
    scaler_type=CONFIG["scaler_type"],
    column_pairs=CONFIG["column_pairs"],
    n_jobs=CONFIG["n_jobs"],
):
    assert scaler_type in ("RobustScaler", "StandardScaler")

    # Make dirs
    make_dirs([data_store_dir])

    # Set file_names
    file_names = sorted(glob(os.path.join(rawdata_dir, "*")))

    # Build features
    features = build_features(file_names, n_jobs=n_jobs)
    scaler = build_scaler(features=features, scaler_type=scaler_type)

    features = preprocess_features(features=features, scaler=scaler)

    # Build labels
    labels = build_labels(
        file_names=file_names,
        lookahead_window=lookahead_window,
        q_threshold=q_threshold,
        column_pairs=column_pairs,
        n_jobs=n_jobs,
    )

    # Build pricing
    pricing = build_pricing(file_names=file_names, n_jobs=n_jobs)

    # Build bins
    bins = build_all_bins(
        file_names=file_names, lookahead_window=lookahead_window, n_jobs=n_jobs
    )

    # Masking with common index
    common_index = features.index & labels.index
    features = features.reindex(common_index).sort_index()
    labels = labels.reindex(common_index).sort_index()
    pricing = pricing.reindex(common_index).sort_index()

    params = {
        "lookahead_window": lookahead_window,
        "q_threshold": q_threshold,
        "train_ratio": train_ratio,
        "scaler_type": scaler_type,
        "column_pairs": tuple(column_pairs),
    }

    # Store Artifacts
    store_artifacts(
        features=features,
        labels=labels,
        pricing=pricing,
        scaler=scaler,
        bins=bins,
        train_ratio=train_ratio,
        params=params,
        data_store_dir=data_store_dir,
    )


if __name__ == "__main__":
    import fire

    fire.Fire(build_dataset)
