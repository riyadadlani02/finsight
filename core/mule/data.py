"""Transaction data for the mule detector: a synthetic generator and a CSV loader.

Both return a DataFrame with columns
    sender, receiver, amount, timestamp (datetime), is_fraud (0/1 or NaN if unlabeled)
The generator also returns the ground-truth account table.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

COLUMNS = ["sender", "receiver", "amount", "timestamp", "is_fraud"]


def generate(n_accounts: int = 3000, n_rings: int = 8, days: int = 30, seed: int = 7
             ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Synthetic bank transfers with planted mule rings.

    Legitimate traffic includes deliberate hard negatives: small businesses that
    collect many payments and pay suppliers the same day, and people who forward
    most of their salary as rent. Each ring is victims -> layer-1 mules ->
    (layer-2 mules) -> a cash-out account, with funds forwarded within hours
    minus a small cut, sometimes split or cycled between mules.
    """
    rng = np.random.default_rng(seed)
    start = pd.Timestamp("2026-01-01")
    horizon = days * 86400
    ids = np.array([f"A{i:05d}" for i in range(n_accounts)])
    rows: list[tuple] = []

    def add(s, r, amount, t, fraud=0):
        if s != r and 0 <= t < horizon:
            rows.append((s, r, round(float(amount), 2), t, fraud))

    perm = rng.permutation(n_accounts)
    n_merchants, n_employers, n_business = n_accounts // 40, n_accounts // 100, n_accounts // 30
    merchants = ids[perm[:n_merchants]]
    employers = ids[perm[n_merchants:n_merchants + n_employers]]
    businesses = ids[perm[n_merchants + n_employers:n_merchants + n_employers + n_business]]
    people = ids[perm[n_merchants + n_employers + n_business:]]

    # --- legitimate traffic
    for p in people:
        employer = rng.choice(employers)
        salary = rng.lognormal(7.8, 0.4)
        friends = rng.choice(people, size=rng.integers(2, 6), replace=False)
        landlord = rng.choice(people)
        pays_rent_fast = rng.random() < 0.15  # hard negative: quick pass-through of salary
        for payday in np.arange(rng.integers(0, 14) * 86400, horizon, 14 * 86400):
            t = payday + rng.integers(8 * 3600, 18 * 3600)
            add(employer, p, salary / 2, t)
            if pays_rent_fast:
                add(p, landlord, salary / 2 * rng.uniform(0.6, 0.9), t + rng.integers(600, 6 * 3600))
        for _ in range(rng.poisson(days * 0.8)):  # card-style spending
            add(p, rng.choice(merchants), rng.lognormal(3.3, 0.9), rng.integers(0, horizon))
        for _ in range(rng.poisson(days * 0.1)):  # paying friends
            add(p, rng.choice(friends), rng.lognormal(3.8, 0.8), rng.integers(0, horizon))

    for b in businesses:  # hard negative: many payers in, suppliers paid out the same day
        suppliers = rng.choice(np.concatenate([merchants, businesses]), size=3, replace=False)
        for day in range(days):
            takings = 0.0
            for _ in range(rng.poisson(4)):
                amount = rng.lognormal(4.5, 0.7)
                takings += amount
                add(rng.choice(people), b, amount, day * 86400 + rng.integers(9 * 3600, 17 * 3600))
            if takings and rng.random() < 0.5:
                add(b, rng.choice(suppliers), takings * rng.uniform(0.5, 0.9), day * 86400 + rng.integers(17 * 3600, 22 * 3600))

    # --- mule rings
    truth = {a: (0, -1, "normal") for a in ids}
    pool = list(rng.permutation(people))
    for ring in range(n_rings):
        n_l1, n_l2 = rng.integers(3, 7), rng.integers(0, 4)
        l1 = [pool.pop() for _ in range(n_l1)]
        l2 = [pool.pop() for _ in range(n_l2)]
        cashout = pool.pop()
        for a in l1 + l2:
            truth[a] = (1, ring, "mule")
        truth[cashout] = (1, ring, "cash-out")
        active_from = rng.integers(0, max(1, horizon - 10 * 86400))
        for mule in l1:
            for _ in range(rng.integers(3, 9)):  # each victim payment is forwarded on
                victim = rng.choice(people)
                amount = rng.uniform(400, 6000)
                t = active_from + rng.integers(0, 10 * 86400)
                add(victim, mule, amount, t, 1)
                forward = amount * rng.uniform(0.85, 0.97)  # the mule keeps a cut
                t += rng.integers(300, 8 * 3600)
                targets = [rng.choice(l2)] if l2 and rng.random() < 0.7 else [cashout]
                if rng.random() < 0.25 and len(l1) > 1:  # sideways hop makes cycles
                    targets = [rng.choice([m for m in l1 if m != mule])]
                if rng.random() < 0.3:  # split to dodge amount thresholds
                    half = forward * rng.uniform(0.4, 0.6)
                    add(mule, targets[0], half, t, 1)
                    add(mule, cashout, forward - half, t + rng.integers(60, 3600), 1)
                else:
                    add(mule, targets[0], forward, t, 1)
        # layer-2 and sideways recipients sweep what they hold to the cash-out account
        ring_tx = pd.DataFrame(rows, columns=COLUMNS)
        for mule in l2 + l1:
            received = ring_tx[(ring_tx.receiver == mule) & (ring_tx.is_fraud == 1) & ring_tx.sender.isin(l1)]
            for r in received.itertuples():
                add(mule, cashout, r.amount * rng.uniform(0.9, 0.98), r.timestamp + rng.integers(300, 6 * 3600), 1)

    tx = pd.DataFrame(rows, columns=COLUMNS)
    tx["timestamp"] = start + pd.to_timedelta(tx["timestamp"], unit="s")
    tx = tx.sort_values("timestamp").reset_index(drop=True)
    accounts = pd.DataFrame([(a, *v) for a, v in truth.items()], columns=["account", "is_mule", "ring_id", "role"])
    return tx, accounts


# Known public layouts -> our column names.
_ALIASES = {
    "sender": ["sender", "from", "source", "nameorig", "from_account", "account", "payer", "src"],
    "receiver": ["receiver", "to", "target", "namedest", "to_account", "account.1", "payee", "dst", "beneficiary"],
    "amount": ["amount", "amount paid", "amount_paid", "value", "amt"],
    "timestamp": ["timestamp", "time", "date", "datetime", "step"],
    "is_fraud": ["is_fraud", "isfraud", "is laundering", "is_laundering", "label", "fraud"],
}


def load_csv(source, max_rows: int | None = 500_000) -> pd.DataFrame:
    """Load a transaction CSV. Recognises PaySim and IBM AML (HI/LI) layouts.

    Needs sender, receiver and amount. A timestamp is optional (row order is used
    if absent); a fraud label is optional (enables supervised training/evaluation).
    """
    raw = pd.read_csv(source, nrows=max_rows)
    lower = {c.lower().strip(): c for c in raw.columns}
    picked = {}
    for target, names in _ALIASES.items():
        picked[target] = next((lower[n] for n in names if n in lower), None)
    missing = [k for k in ("sender", "receiver", "amount") if not picked[k]]
    if missing:
        raise ValueError(f"Could not find column(s) for {', '.join(missing)}. Found: {list(raw.columns)}")

    tx = pd.DataFrame({"sender": raw[picked["sender"]].astype(str), "receiver": raw[picked["receiver"]].astype(str)})
    # IBM AML: account numbers repeat across banks, so prefix with the bank id.
    if "from bank" in lower and "to bank" in lower:
        tx["sender"] = raw[lower["from bank"]].astype(str) + ":" + tx["sender"]
        tx["receiver"] = raw[lower["to bank"]].astype(str) + ":" + tx["receiver"]
    tx["amount"] = pd.to_numeric(raw[picked["amount"]], errors="coerce")

    ts = raw[picked["timestamp"]] if picked["timestamp"] else pd.Series(range(len(raw)))
    if pd.api.types.is_numeric_dtype(ts):  # PaySim "step" = hours since start
        unit = "h" if picked["timestamp"] and picked["timestamp"].lower() == "step" else "s"
        tx["timestamp"] = pd.Timestamp("2026-01-01") + pd.to_timedelta(ts, unit=unit)
    else:
        tx["timestamp"] = pd.to_datetime(ts, errors="coerce")
    tx["is_fraud"] = pd.to_numeric(raw[picked["is_fraud"]], errors="coerce") if picked["is_fraud"] else np.nan
    tx = tx.dropna(subset=["amount", "timestamp"])
    tx = tx[(tx["amount"] > 0) & (tx["sender"] != tx["receiver"])]
    return tx.sort_values("timestamp").reset_index(drop=True)[COLUMNS]


def account_labels(tx: pd.DataFrame) -> pd.Series | None:
    """Account-level labels from transaction labels, or None if the data is unlabeled.

    An account counts as a mule if it *receives* flagged money: that covers the
    pass-through and cash-out accounts and leaves the victims (who only send) clean.
    """
    if tx["is_fraud"].isna().all():
        return None
    accounts = pd.Index(pd.unique(tx[["sender", "receiver"]].values.ravel()))
    flagged = set(tx.loc[tx["is_fraud"] == 1, "receiver"])
    return pd.Series(accounts.isin(flagged).astype(int), index=accounts, name="is_mule")
