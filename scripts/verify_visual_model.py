"""Run the production multi-image protocol on synthetic empty-room frames (no private footage)."""
import json
from pathlib import Path
import sys
import time

import cv2
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/vision-worker'))
from worker.core_behaviors import PROMPT, PROMPT_VERSION, validate_review
from worker.visual_client import OpenAICompatibleVisionClient, VisualModelConfig, VisualModelError, encode_jpeg, image_part


def main():
    values={}
    for line in (ROOT/'.env').read_text().splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            key,value=line.split('=',1);values[key.strip()]=value.strip().strip('\"\'')
    config=VisualModelConfig.from_env({**values,'VISUAL_MODEL_TIMEOUT':'30','VISUAL_MODEL_MAX_RETRIES':'0'})
    if config is None:
        raise SystemExit('Visual model is not configured')
    samples=[];content=[]
    for index in range(8):
        frame=np.full((360,640,3),220,dtype=np.uint8)
        for x in (100,360):
            cv2.rectangle(frame,(x,160),(x+140,240),(100,100,100),3)
            cv2.rectangle(frame,(x+30,110),(x+100,155),(60,60,60),3)
        sample={'at':index*.5,'tracks':[],'jpeg':encode_jpeg(frame)}
        samples.append(sample)
        content.extend([{'type':'text','text':json.dumps({'frame_index':index,'capture_time':sample['at'],'tracks':[]})},image_part(sample['jpeg'])])
    report={'model':config.model,'prompt_version':PROMPT_VERSION,'images':8,'input':'synthetic_empty_room','credentials_in_report':False}
    start=time.monotonic()
    try:
        raw=OpenAICompatibleVisionClient(config).complete([{'role':'system','content':PROMPT},{'role':'user','content':content}],max_tokens=1200)
        facts=validate_review(raw,samples)
        report.update(status='passed' if not facts else 'failed',fact_count=len(facts))
    except VisualModelError as exc:
        report.update(status='failed',error_code=exc.code)
    except (ValueError,TypeError,KeyError):
        report.update(status='failed',error_code='INVALID_STRUCTURED_RESULT')
    report['elapsed_seconds']=round(time.monotonic()-start,2)
    destination=ROOT/'tmp/reports/visual-model-smoke.json';destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False))
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':
    raise SystemExit(main())
