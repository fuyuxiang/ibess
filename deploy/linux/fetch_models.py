"""在服务器上下载语音模型到 audio-worker 期望的目录结构。

目标结构（代码里 local_model() 的约定）：
    /root/.cache/modelscope/models/iic--<模型名>/snapshots/master/<文件>

modelscope 新版把缓存放在 models/iic/<name>/snapshots/<rev>，所以这里先下到临时目录，
再整理成上面这种扁平结构，避免版本差异导致 worker 找不到模型。
"""
import os
import shutil
from pathlib import Path

from modelscope import snapshot_download

MODELS = [
    "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
    "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
    "iic/punc_ct-transformer_cn-en-common-vocab471067-large",
    "iic/speech_campplus_sv_zh-cn_16k-common",
]

ROOT = Path("/root/.cache/modelscope/models")
TMP = Path("/tmp/ms-download")


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    for model_id in MODELS:
        name = model_id.split("/", 1)[1]
        target = ROOT / f"iic--{name}" / "snapshots" / "master"
        if target.is_dir() and any(target.iterdir()):
            print(f"[skip] {name} 已存在", flush=True)
            continue
        print(f"[down] {model_id}", flush=True)
        path = snapshot_download(model_id, cache_dir=str(TMP))
        target.mkdir(parents=True, exist_ok=True)
        for item in Path(path).iterdir():
            dest = target / item.name
            if dest.is_symlink() or dest.exists():
                continue
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)
        print(f"[ok]   {target}  ({sum(f.stat().st_size for f in target.rglob('*') if f.is_file()) / 1e6:.0f} MB)", flush=True)
    shutil.rmtree(TMP, ignore_errors=True)
    print("[done]", flush=True)


if __name__ == "__main__":
    main()
