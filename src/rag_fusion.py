from collections import defaultdict

def reciprocal_rank_fusion(results_list, k=60):

    scores = defaultdict(float)
    chunk_map = {}

    for results in results_list:

        for rank, (score, chunk) in enumerate(results):

            chunk_id = chunk["chunk_id"]

            chunk_map[chunk_id] = chunk

            scores[chunk_id] += 1 / (k + rank)

    fused = sorted(
        [(score, chunk_map[cid]) for cid, score in scores.items()],
        key=lambda x: x[0],
        reverse=True
    )

    return fused