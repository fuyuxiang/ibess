"""Download the existing, pinned OpenCV face models and verify both length and SHA256."""
import hashlib
import json
from pathlib import Path
import urllib.request


def main():
    root = Path(__file__).resolve().parents[1]/'models/face'
    manifest = json.loads((root/'manifest.json').read_text())
    for item in manifest['models']:
        target = root/item['file']
        if target.parent != root or not item['url'].startswith('https://media.githubusercontent.com/media/opencv/opencv_zoo/'):
            raise ValueError('Unexpected model source')
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest()==item['sha256']:
            print(f"Verified {target.name}")
            continue
        partial=target.with_suffix('.onnx.part')
        try:
            with urllib.request.urlopen(item['url'],timeout=60) as source,partial.open('wb') as output:
                while chunk:=source.read(1024*1024):
                    output.write(chunk)
            if partial.stat().st_size!=item['size'] or hashlib.sha256(partial.read_bytes()).hexdigest()!=item['sha256']:
                raise ValueError('Model integrity check failed')
            partial.replace(target)
            print(f"Downloaded and verified {target.name}")
        finally:
            partial.unlink(missing_ok=True)


if __name__=='__main__':
    main()
