"""The on-disk index stores each file preamble once (#343).

Every chunk's text is the file's path line and preamble, a separator, then its body,
so a file with 50 chunks saved its preamble 50 times: 68% of the chunk text in
index.npz on this repository, 86% on a 20,000-chunk one. _encode_index now writes each
distinct header once and the bodies apart; _decode_index rebuilds the exact texts and
still reads an index written in the old form.
"""

import io
import json

import numpy as np

from llm_light import indexer

SEP = "\n    // ...\n"
HEADER = "// pkg/big.py\n" + "".join(f"import mod_{i}\n" for i in range(40))


def _store():
    store = {f"pkg/big.py::f{i}": {"text": f"{HEADER}{SEP}def f{i}():\n    return {i}\n",
                                   "emb": np.ones(4, dtype=np.float32) * i} for i in range(30)}
    store["pkg/whole.txt"] = {"text": "a whole-file chunk without a separator"}
    store["pkg/two.py::g"] = {"text": f"// pkg/two.py\nimport ü{SEP}def g():\n    x = 1{SEP}more"}
    return store


def test_the_texts_come_back_exactly():
    store = _store()
    back = indexer._decode_index(indexer._encode_index(store))
    assert {cid: e["text"] for cid, e in back.items()} == {cid: e["text"] for cid, e in store.items()}
    assert np.array_equal(back["pkg/big.py::f7"]["emb"], store["pkg/big.py::f7"]["emb"])


def test_a_shared_header_is_written_once():
    data = indexer._encode_index(_store())
    with np.load(io.BytesIO(data), allow_pickle=False) as z:
        meta = z["meta"].tobytes().decode("utf-8")
    assert meta.count("import mod_39") == 1


def test_the_index_gets_smaller():
    store = _store()
    old_meta = json.dumps({"ids": list(store), "texts": [e["text"] for e in store.values()]})
    with np.load(io.BytesIO(indexer._encode_index(store)), allow_pickle=False) as z:
        new_meta = z["meta"].tobytes()
    assert len(new_meta) < len(old_meta.encode("utf-8")) / 3


def test_an_index_in_the_old_form_still_loads():
    store = _store()
    ids = list(store)
    meta = json.dumps({"ids": ids, "texts": [store[c]["text"] for c in ids]}).encode("utf-8")
    buf = io.BytesIO()
    np.savez(buf, meta=np.frombuffer(meta, dtype=np.uint8))
    back = indexer._decode_index(buf.getvalue())
    assert {cid: e["text"] for cid, e in back.items()} == {cid: e["text"] for cid, e in store.items()}
