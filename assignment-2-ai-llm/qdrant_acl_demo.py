"""Runnable demo of ACL-filtered hybrid search in Qdrant (local in-memory mode).

Shows that documents outside the user's groups, or above their clearance, are
never returned - the access-control filter is applied inside both the dense and
the sparse prefetch before RRF fusion.

Requires the optional dependency:  pip install qdrant-client
Run:                                python assignment-2-ai-llm/qdrant_acl_demo.py

Note: toy 4-dimensional vectors stand in for real embeddings. In local mode,
payload indexes are accepted but have no performance effect (server mode only).
"""

from __future__ import annotations

import warnings

from qdrant_client import QdrantClient, models

COLLECTION = "enterprise_docs"

DOCS = [  # (id, title, acl_groups, classification_level, dense vector)
    (1, "Company holiday calendar", ["grp-all-employees"], 0, [0.9, 0.1, 0.0, 0.1]),
    (2, "Parental leave policy", ["grp-all-employees"], 1, [0.8, 0.3, 0.1, 0.0]),
    (3, "Executive salary bands", ["grp-hr"], 3, [0.7, 0.4, 0.2, 0.1]),
    (4, "XR-200 error E-4021 fix", ["grp-support", "grp-eng"], 1, [0.1, 0.9, 0.3, 0.0]),
    (5, "M&A target shortlist", ["grp-exec"], 4, [0.6, 0.5, 0.1, 0.2]),
]


def build_acl_filter(claims: dict) -> models.Filter:
    """Mandatory filter built from verified identity claims (never from user/LLM text)."""
    return models.Filter(must=[
        models.FieldCondition(key="acl_groups", match=models.MatchAny(any=claims["groups"])),
        models.FieldCondition(key="classification_level",
                              range=models.Range(lte=claims["clearance_level"])),
    ])


def setup() -> QdrantClient:
    """Create an in-memory collection with dense + sparse vectors and sample points."""
    client = QdrantClient(":memory:")
    client.create_collection(
        COLLECTION,
        vectors_config={"dense": models.VectorParams(size=4, distance=models.Distance.COSINE)},
        sparse_vectors_config={"sparse": models.SparseVectorParams()},
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # local mode warns that payload indexes are no-ops
        client.create_payload_index(COLLECTION, "acl_groups", models.PayloadSchemaType.KEYWORD)
        client.create_payload_index(COLLECTION, "classification_level",
                                    models.PayloadSchemaType.INTEGER)
    client.upsert(COLLECTION, [
        models.PointStruct(
            id=i,
            vector={"dense": vec,
                    "sparse": models.SparseVector(indices=[i, 100], values=[1.0, 0.5])},
            payload={"title": title, "acl_groups": groups, "classification_level": level},
        )
        for i, title, groups, level, vec in DOCS
    ])
    return client


def secure_search(client: QdrantClient, claims: dict, k: int = 10) -> list[str]:
    """Hybrid (dense + sparse, RRF) search restricted by the ACL filter."""
    acl = build_acl_filter(claims)
    points = client.query_points(
        COLLECTION,
        prefetch=[
            models.Prefetch(query=[0.8, 0.3, 0.1, 0.1], using="dense", filter=acl, limit=20),
            models.Prefetch(query=models.SparseVector(indices=[100], values=[1.0]),
                            using="sparse", filter=acl, limit=20),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=k,
        with_payload=True,
    ).points
    return sorted(p.payload["title"] for p in points)


def main() -> None:
    """Compare what three different employees can retrieve for the same query."""
    client = setup()
    users = {
        "engineer": {"groups": ["grp-all-employees", "grp-eng"], "clearance_level": 1},
        "hr_manager": {"groups": ["grp-all-employees", "grp-hr"], "clearance_level": 3},
        "contractor": {"groups": [], "clearance_level": 0},
    }
    for name, claims in users.items():
        print(f"{name:<11} -> {secure_search(client, claims)}")


if __name__ == "__main__":
    main()
