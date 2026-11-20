"""GraphSAGE mule classifier (PyTorch Geometric).

Each account is a node carrying its behaviour features; message passing lets the
model use the neighbourhood too ("this account forwards fast AND so do the
accounts it pays"), which is what separates a ring member from a lone
pass-through account such as a small business.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import torch
import torch.nn.functional as F
from sklearn.metrics import average_precision_score, precision_recall_fscore_support
from sklearn.model_selection import train_test_split
from torch_geometric.data import Data
from torch_geometric.nn import SAGEConv

from .graph import FEATURES


class MuleSAGE(torch.nn.Module):
    def __init__(self, n_features: int, hidden: int = 64):
        super().__init__()
        self.conv1 = SAGEConv(n_features, hidden)
        self.conv2 = SAGEConv(hidden, hidden)
        self.out = torch.nn.Linear(hidden, 2)

    def forward(self, x, edge_index):
        x = F.relu(self.conv1(x, edge_index))
        x = F.dropout(x, 0.3, self.training)
        x = F.relu(self.conv2(x, edge_index))
        return self.out(x)


def to_data(features: pd.DataFrame, tx: pd.DataFrame, labels: pd.Series | None = None) -> Data:
    """Build the PyG graph. Features are log-scaled then standardised per graph, so a
    model trained on one dataset can be applied to another with different currency/scale."""
    x = np.log1p(features[FEATURES].to_numpy(dtype=np.float64))
    x = (x - x.mean(0)) / (x.std(0) + 1e-9)
    pos = pd.Series(np.arange(len(features)), index=features.index)
    pairs = tx[["sender", "receiver"]].drop_duplicates()
    src, dst = pos.loc[pairs["sender"]].to_numpy(), pos.loc[pairs["receiver"]].to_numpy()
    # both directions: a mule is defined by who pays it *and* who it pays
    edge_index = torch.tensor(np.vstack([np.concatenate([src, dst]), np.concatenate([dst, src])]), dtype=torch.long)
    data = Data(x=torch.tensor(x, dtype=torch.float), edge_index=edge_index)
    if labels is not None:
        data.y = torch.tensor(labels.reindex(features.index).fillna(0).to_numpy(), dtype=torch.long)
    return data


def train(data: Data, epochs: int = 150, seed: int = 0, progress=None) -> tuple[MuleSAGE, dict]:
    """Train on 60% of accounts, pick the best epoch on 20%, report on the final unseen 20%."""
    torch.manual_seed(seed)
    y = data.y.numpy()
    idx = np.arange(len(y))
    train_idx, rest = train_test_split(idx, test_size=0.4, stratify=y, random_state=seed)
    val_idx, test_idx = train_test_split(rest, test_size=0.5, stratify=y[rest], random_state=seed)

    model = MuleSAGE(data.num_features)
    opt = torch.optim.Adam(model.parameters(), lr=0.01, weight_decay=5e-4)
    # mules are rare, so weight them up (capped to avoid flagging everything)
    pos_weight = min(float((y == 0).sum() / max((y == 1).sum(), 1)), 50.0)
    weight = torch.tensor([1.0, pos_weight])

    best_ap, best_state = -1.0, None
    for epoch in range(epochs):
        model.train()
        opt.zero_grad()
        loss = F.cross_entropy(model(data.x, data.edge_index)[train_idx], data.y[train_idx], weight=weight)
        loss.backward()
        opt.step()
        if epoch % 5 == 0 or epoch == epochs - 1:
            ap = average_precision_score(y[val_idx], predict(model, data)[val_idx])
            if ap > best_ap:
                best_ap, best_state = ap, {k: v.clone() for k, v in model.state_dict().items()}
            if progress:
                progress(epoch + 1, epochs)
    model.load_state_dict(best_state)

    prob = predict(model, data)
    p, r, f1, _ = precision_recall_fscore_support(y[test_idx], prob[test_idx] >= 0.5, average="binary", zero_division=0)
    metrics = {"precision": float(p), "recall": float(r), "f1": float(f1),
               "avg_precision": float(average_precision_score(y[test_idx], prob[test_idx])),
               "test_accounts": int(len(test_idx)), "test_mules": int(y[test_idx].sum())}
    return model, metrics


@torch.no_grad()
def predict(model: MuleSAGE, data: Data) -> np.ndarray:
    """Probability that each account is a mule."""
    model.eval()
    return F.softmax(model(data.x, data.edge_index), dim=1)[:, 1].numpy()
