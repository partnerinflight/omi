"""Standalone runner in a separate SpeechBrain environment, using local ECAPA weights."""

import argparse
import array
import hashlib
import json
import os
from pathlib import Path
import sys
import wave


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--model", type=Path, required=True)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    model = args.model.resolve()
    names = [
        "hyperparams.yaml",
        "embedding_model.ckpt",
        "mean_var_norm_emb.ckpt",
        "classifier.ckpt",
        "label_encoder.txt",
    ]
    digest = hashlib.sha256(b"ecapa-16k-unnormalized-v1")
    for name in names:
        with (model / name).open("rb") as f:
            digest.update(hashlib.file_digest(f, "sha256").digest())
    os.environ["HF_HUB_OFFLINE"] = "1"
    import torch
    from speechbrain.inference.classifiers import EncoderClassifier
    from speechbrain.utils.fetching import LocalStrategy

    torch.set_num_threads(2)
    # Read weights in place: no symlink privilege and no model copy per recording.
    encoder = EncoderClassifier.from_hparams(
        source=str(model),
        savedir=None,
        overrides={"pretrained_path": str(model).replace("\\", "/")},
        local_strategy=LocalStrategy.NO_LINK,
        run_opts={"device": "cpu"},
    )
    vectors = {}
    for filename in json.loads(args.input.read_text(encoding="utf-8")):
        with wave.open(filename, "rb") as f:
            if (f.getnchannels(), f.getframerate(), f.getsampwidth()) != (1, 16000, 2):
                raise ValueError("Expected mono 16 kHz PCM16 review audio")
            data = array.array("h", f.readframes(f.getnframes()))
        if sys.byteorder != "little":
            data.byteswap()
        signal = torch.tensor(data, dtype=torch.float32).unsqueeze(0) / 32768
        # Silence is not a voice reference.
        if signal.numel() < 32000 or signal.square().mean().sqrt().item() < 0.003:
            continue
        with torch.inference_mode():
            vector = encoder.encode_batch(signal).flatten()
            vector = vector / vector.norm()
        if torch.isfinite(vector).all():
            vectors[filename] = vector.tolist()
    args.output.write_text(json.dumps({"model": digest.hexdigest(), "vectors": vectors}), encoding="utf-8")


if __name__ == "__main__":
    main()
