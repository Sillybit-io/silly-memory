# sentence-transformers/all-MiniLM-L6-v2 — preseeded weights

This directory holds the preseeded `sentence-transformers/all-MiniLM-L6-v2`
model files (~91 MB total). The repo ships them so the default offline install
never has to download the MODEL over the network — the weights are already on
disk and SHA-256-verified on every load.

Shipping the weights does **not**, by itself, enable semantic recall. Dense
recall also needs one of the optional embedding libraries importable at runtime:
`torch` + `sentence_transformers`, or `fastembed` (ONNX). If neither is present,
the embedding backend resolves to `noop` and recall runs FTS5-only (full-text,
still functional — just lexical rather than semantic). So "offline" here means
the model is never fetched, not that semantic recall works without the libs.
(Cosmetic quirk: a fresh offline install may LOG `noop` during backend selection
because that step runs before the preseed copy; at runtime, with the weights
cached and a backend lib present, recall uses `sentence-transformers`/`fastembed`.)

Upstream source: https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2

## License & attribution

`all-MiniLM-L6-v2` is created and published by the Sentence-Transformers
(SBERT) project and is distributed under the **Apache License 2.0**. These
preseeded files are redistributed unmodified under that license; no endorsement
by the original authors is implied.

- Model: `sentence-transformers/all-MiniLM-L6-v2`
- Upstream: https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2
- License: Apache-2.0 — the full text is in `LICENSE` in this folder
  (https://www.apache.org/licenses/LICENSE-2.0)

## Files shipped

- `config.json`
- `tokenizer.json`
- `tokenizer_config.json`
- `vocab.txt`
- `special_tokens_map.json`
- `model.safetensors`

SHA-256 pins for all six files live in
`memory/lib/memory_system/backends/embedding/weights_manifest.py` under the
`sentence-transformers/all-MiniLM-L6-v2` entry. `LICENSE` (the Apache License
2.0 text) ships beside them.

## Install behavior

`install.sh` copies these files to
`~/.silly-memory/_embeddings/sentence-transformers-all-MiniLM-L6-v2/` and the
embedding backend verifies every SHA-256 on every load. A tampered or missing
file aborts the backend with
`RuntimeError("weight integrity check failed: …")`. When
`MEMORY_ALLOW_NETWORK=1` is set, a verification failure triggers a
refresh-from-upstream; otherwise it is a hard error.

## Refresh

To replace these files with a freshly downloaded copy from upstream:

1. Re-download the six files above from
   https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2 into this
   directory, overwriting in place.
2. Regenerate the manifest entry:

   ```bash
   python3 -m memory.lib.memory_system.backends.embedding.weights_manifest \
       sentence-transformers/all-MiniLM-L6-v2 \
       memory/weights/all-MiniLM-L6-v2
   ```

3. Paste the printed dict into `WEIGHTS_MANIFEST`, replacing the existing
   `sentence-transformers/all-MiniLM-L6-v2` entry. Do not add `optional: True`
   to any of the six files — they are required.

## Verify locally

```bash
python3 -c "
import sys; sys.path.insert(0, 'memory/lib')
from memory_system.backends.embedding.weights_manifest import verify_weights
verify_weights('sentence-transformers/all-MiniLM-L6-v2', None)
print('OK')
"
```
