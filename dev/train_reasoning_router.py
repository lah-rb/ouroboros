#!/usr/bin/env python3
"""Train + serialize the production reasoning router artifact.

Word+char TF-IDF union + class-weighted logistic regression — champion of the
2026-07 bake-off (task-held-out: macro-F1 .622, route-low precision .976 at 38%
of low-savings at threshold 0.4; see dev/ADAPTIVE_REASONING_DECISION_LAYER.md).
The DEPLOY artifact trains on ALL trusted labels (the bake-off numbers are the
held-out estimate; retrain whenever trusted_labels grows).

-> models/reasoning_router_v1.joblib  {vectorizer, clf, medium_idx, meta}
"""

import json
import time
from collections import Counter
from pathlib import Path

import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import FeatureUnion

DATA = "dev/train_dataset_trusted_v1.jsonl"
OUT = Path("models/reasoning_router_v1.joblib")

rows = [json.loads(l) for l in open(DATA)]
texts = [r["text"] for r in rows]
labels = [r["label"] for r in rows]
print(f"training on {len(rows)} trusted labels: {dict(Counter(labels))}")

vectorizer = FeatureUnion(
    [
        (
            "w",
            TfidfVectorizer(max_features=50000, ngram_range=(1, 2), sublinear_tf=True),
        ),
        (
            "c",
            TfidfVectorizer(
                analyzer="char_wb",
                ngram_range=(3, 5),
                max_features=100000,
                sublinear_tf=True,
            ),
        ),
    ]
)
X = vectorizer.fit_transform(texts)
clf = LogisticRegression(class_weight="balanced", max_iter=2000)
clf.fit(X, labels)

t0 = time.time()
for _ in range(200):
    clf.predict_proba(vectorizer.transform([texts[0]]))
lat_ms = (time.time() - t0) / 200 * 1000

OUT.parent.mkdir(exist_ok=True)
joblib.dump(
    {
        "vectorizer": vectorizer,
        "clf": clf,
        "medium_idx": list(clf.classes_).index("medium"),
        "meta": {
            "built": time.strftime("%Y-%m-%d %H:%M"),
            "recipe": "word(1-2,50k)+char_wb(3-5,100k) tfidf union + balanced LogReg",
            "train_rows": len(rows),
            "labels": dict(Counter(labels)),
            "standard": "JUDGE_STANDARD v1.0 retro-gate (trusted_labels_v1)",
            "heldout_reference": "macroF1 .622 | cov@.93 .747 | cov@.976 .417 (task split seed 0)",
            "default_threshold": 0.4,
        },
    },
    OUT,
    compress=3,
)
print(
    f"saved {OUT} ({OUT.stat().st_size/1e6:.1f} MB) | single-text inference {lat_ms:.2f} ms"
)
