### Indexing phase of RAG system
This Django app is dedicated to handle:

- **Some intuition for the chunking of the `PastPaperAssets` into:**
  - First, regarding the content, make sure that sections are split based on their relative purpose in the document.  
    (e.g. question 1.a., background, answer explanation, rubric, etc.)
  - Then, some sections could potentially go beyond the encoder's context size, so chunking should be carried out **within each content-focused section** while preserving semantic boundaries (e.g., avoid cutting in the middle of a sentence or formula).
  - Its parameters: `chunk_size` and `overlap_size`, depending on the parameters of the encoder used:
    - For 512-token encoders (e.g., **BGE**, **E5**, **Cohere v3**): chunk_size ≈ 200–300 tokens, overlap ≈ 10–20%.
    - For 8k-token encoders (e.g., **text-embedding-3-large**, **Jina v3**, **BGE-M3**): chunk_size ≈ 500–800 tokens, overlap ≈ 10–15%.
  - Each chunk belongs to a **ChunkPlan**, which is tied to a specific `(encoder, chunking policy)` combination, ensuring documents of similar type share consistent chunking rules.

- **Dense Vector Generation**
  - Each `(encoder, chunking policy)` combination corresponds to an **IndexProfile**, which specifies:
    - Encoder configuration: model name, dimension, tokenizer, max tokens.
    - Chunking policy: chunk size, overlap, boundary preferences.
    - Physical index location: Qdrant collection name, metric type.
  - For each `ChunkPlan`:
    1. Batch the chunk texts.
    2. Embed them using the designated encoder.
    3. Upsert the vectors into **a dedicated Qdrant collection** for that IndexProfile:
       - One Qdrant collection per encoder/policy pair (e.g., `pp_bge-m3__p250_20__v1`, `pp_te3-large__p700_12__v1`).
       - Collection configuration:
         - `size`: encoder vector dimension.
         - `distance`: typically Cosine for embeddings.
         - `hnsw_config`: m=32, ef_construct=200.
       - Use stable deterministic point IDs (`plan_id + chunk_id`) for idempotent upserts.
       - Attach payload fields for retrieval and filtering (e.g., `plan_id`, `chunk_id`, `docv_id`, `paper_id`, `hierarchy`, `span`).
  - After all chunks are embedded and stored, mark the `ChunkPlan` as **EMBEDDED**.

- **Indexing lifecycle**
  - **Creation**: When a new DocumentVersion is parsed, choose `(encoder, policy)` pairs to create `ChunkPlan`s.
  - **Embedding**: Process each `ChunkPlan` asynchronously in batches, store embeddings in the Qdrant collection.
  - **Activation**: One `ChunkPlan` may be marked as the active plan for default search; multiple plans can co-exist for experimentation or fallback.
  - **Deletion**: Removing a plan involves deleting its points from Qdrant and optionally dropping the collection if no other plans reference it.

- **Retrieval phase integration**
  - At query time:
    1. Select the `IndexProfile` based on the active plan, explicit query parameters, or routing logic (e.g., subject type → preferred encoder).
    2. Encode the query with the profile's encoder.
    3. Search within the corresponding Qdrant collection, optionally applying filters (e.g., restrict to a specific paper_id).
    4. Use `chunk_id` from the payload to fetch original text and hierarchy from the database.
    5. Optionally, aggregate hits to higher-level sections before passing to the RAG generator.

---