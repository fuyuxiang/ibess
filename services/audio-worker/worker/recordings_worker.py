"""私有文件录音队列：ASR、说话人分段、相对时间与原音保存。"""
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import time
from datetime import datetime, timedelta, timezone
import uuid
import wave

import numpy as np
import redis
import soundfile as sf
from scipy.signal import resample_poly
from sqlalchemy import MetaData, Table, create_engine, delete, insert, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from worker.diarization import normalize_segments

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('audio-worker')


def now():
    return datetime.now(timezone.utc)


def tables(engine):
    metadata = MetaData()
    return (Table('audio_recordings', metadata, autoload_with=engine),
            Table('audio_segments', metadata, autoload_with=engine))


def safe_path(root, name):
    root = Path(root).resolve()
    path = (root / name).resolve()
    if path.parent != root or path.name != name:
        raise ValueError('invalid private audio path')
    return path


def enqueue_source(engine, recordings, directory, source_file, source_id):
    path = Path(source_file)
    if not path.is_file():
        return
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError('audio source too large')
    data = path.read_bytes()
    with wave.open(io.BytesIO(data), 'rb') as w:
        duration = round(w.getnframes() * 1000 / w.getframerate())
        if duration <= 0 or duration > 600000:
            raise ValueError('invalid source duration')
    digest = hashlib.sha256(data).hexdigest()
    key = source_id + ':' + digest
    with engine.connect() as c:
        if c.scalar(select(recordings.c.recording_id).where(recordings.c.source_key == key)):
            return
    identifier = str(uuid.uuid4())
    private = safe_path(directory, identifier + '.original.wav')
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_bytes(data)
    try:
        with engine.begin() as c:
            c.execute(insert(recordings).values(recording_id=identifier, title=path.name[:128], source_id=source_id,
                source_type='file', source_key=key, status='queued', duration_ms=duration,
                created_at=now(), segment_count=0, sha256=digest, original_path=private.name))
        log.info('文件源已登记；同一来源和内容只分析一次')
    except IntegrityError:
        private.unlink(missing_ok=True)
    except Exception:
        private.unlink(missing_ok=True)
        raise


def claim_recording(engine, recordings):
    with engine.begin() as c:
        stale = now() - timedelta(minutes=15)
        row = c.execute(select(recordings).where(or_(recordings.c.status == 'queued',
            (recordings.c.status == 'processing') & (recordings.c.claim_at < stale)))
            .order_by(recordings.c.created_at).with_for_update(skip_locked=True).limit(1)).mappings().first()
        if row is None:
            return None
        claim_id = str(uuid.uuid4())
        c.execute(update(recordings).where(recordings.c.recording_id == row['recording_id']).values(
            status='processing', claim_at=now(), claim_id=claim_id, error=None))
        return dict(row) | {'claim_id':claim_id, 'status':'processing'}


def local_model(name):
    root = Path(os.environ.get('AUDIO_MODEL_CACHE', '/root/.cache/modelscope/models'))
    path = root / ('iic--' + name) / 'snapshots/master'
    if not path.is_dir():
        raise FileNotFoundError('local audio model missing')
    return str(path)


class SpeechPipeline:
    def __init__(self):
        from funasr import AutoModel
        self.model_name = 'Paraformer+FSMN-VAD+CT-Punc'
        self.speaker_model = 'CAM++'
        self.model = AutoModel(
            model=local_model('speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch'),
            vad_model=local_model('speech_fsmn_vad_zh-cn-16k-common-pytorch'),
            punc_model=local_model('punc_ct-transformer_cn-en-common-vocab471067-large'),
            spk_model=local_model('speech_campplus_sv_zh-cn_16k-common'),
            device=os.environ.get('AUDIO_DEVICE','cuda'), disable_update=True,
            disable_pbar=True, disable_log=True)
        threshold = float(os.environ.get('SPEAKER_MERGE_THRESHOLD','0.65'))
        if not 0 < threshold <= 1:
            raise ValueError('invalid speaker merge threshold')
        self.model.cb_model.model_config['merge_thr'] = threshold

    def analyze(self, normalized):
        # 谱聚类初始化需可复现，同一录音不会在重试时任意换分组。
        np.random.seed(0)
        results = self.model.generate(input=str(normalized), batch_size_s=60, return_spk_res=True, disable_pbar=True)
        return results[0] if results else {}


def normalize_audio(source, destination):
    samples, rate = sf.read(str(source), dtype='float32', always_2d=True)
    if len(samples) == 0 or len(samples) / rate > 600 or not np.isfinite(samples).all():
        raise ValueError('invalid audio samples')
    mono = samples.mean(axis=1)
    if rate != 16000:
        import math
        divisor = math.gcd(rate,16000)
        mono = resample_poly(mono,16000//divisor,rate//divisor).astype(np.float32)
    sf.write(str(destination),mono,16000,subtype='PCM_16',format='WAV')
    return round(len(mono) * 1000 / 16000)


def process_recording(engine, recordings, segments, directory, record, pipeline):
    identifier, claim_id = record['recording_id'], record['claim_id']
    output_name = identifier + '.' + claim_id.replace('-', '') + '.pcm.wav'
    output = safe_path(directory, output_name)
    try:
        source = safe_path(directory, record['original_path'])
        if hashlib.sha256(source.read_bytes()).hexdigest() != record['sha256']:
            raise ValueError('audio digest mismatch')
        duration = normalize_audio(source, output)
        raw = pipeline.analyze(output)
        rows = normalize_segments(raw, duration)
        if str(raw.get('text') or '').strip() and not rows:
            raise ValueError('transcription has no valid time segments')
        known_speakers = {row['speaker'] for row in rows if row['speaker'] is not None}
        speaker_count = len(known_speakers) if known_speakers or not rows else None
        with engine.begin() as c:
            current = c.scalar(select(recordings.c.claim_id).where(recordings.c.recording_id == identifier).with_for_update())
            if current != claim_id:
                output.unlink(missing_ok=True)
                return None
            c.execute(delete(segments).where(segments.c.recording_id == identifier))
            for index, row in enumerate(rows):
                c.execute(insert(segments).values(segment_id=str(uuid.uuid4()), recording_id=identifier, ordinal=index, **row))
            c.execute(update(recordings).where(recordings.c.recording_id == identifier).values(
                status='ready', duration_ms=duration, processed_at=now(), pcm_path=output.name,
                model=pipeline.model_name, speaker_model=pipeline.speaker_model,
                speaker_count=speaker_count, segment_count=len(rows), error=None))
        log.info('录音处理完成：片段=%d 匿名说话人=%s',len(rows),speaker_count)
        return rows
    except Exception as exc:
        output.unlink(missing_ok=True)
        message = '录音处理失败（' + type(exc).__name__ + '），请检查音频、模型文件和处理服务后重试'
        with engine.begin() as c:
            c.execute(update(recordings).where(recordings.c.recording_id == identifier, recordings.c.claim_id == claim_id).values(
                status='failed',error=message,processed_at=now()))
        log.error('录音处理失败，类型=%s',type(exc).__name__)
        return None


SENSITIVE_LLM_REVIEW = os.getenv('SENSITIVE_LLM_REVIEW', '1') == '1'
LLM_BASE_URL = os.getenv('SEMANTIC_MODEL_BASE_URL', '')
LLM_API_KEY = os.getenv('SEMANTIC_MODEL_API_KEY', '')
LLM_MODEL = os.getenv('SEMANTIC_MODEL_NAME', 'MiniMax-M3.1-Flash')
LLM_TIMEOUT = float(os.getenv('SEMANTIC_REVIEW_TIMEOUT', '20'))
LLM_QUESTION = ('下面是评标室现场的一段对话。请判断：说话人是否在表达倾向性意见、施压、'
                '或泄露评审信息？只回答两段：「有风险」或「正常讨论」，再用不超过 20 字说明理由。不要解释。')


def llm_review_sensitive(text, word, speaker):
    """敏感词命中后交给大模型判断是不是真的违规，返回 (是否保留, 模型理由)。"""
    if not (SENSITIVE_LLM_REVIEW and LLM_BASE_URL and LLM_API_KEY):
        return True, ''
    prompt = LLM_QUESTION + '\n命中词：' + word + '\n说话人：' + str(speaker or '未知') + '\n原话：' + text
    body = {'model': LLM_MODEL, 'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': 600, 'temperature': 0}
    import urllib.error
    import urllib.request
    request = urllib.request.Request(LLM_BASE_URL.rstrip('/') + '/chat/completions',
                                     data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json',
                                              'Authorization': 'Bearer ' + LLM_API_KEY})
    try:
        with urllib.request.urlopen(request, timeout=LLM_TIMEOUT) as response:
            payload = json.loads(response.read().decode())
        content = str(payload['choices'][0]['message'].get('content') or '').strip()
    except Exception as exc:
        log.warning('敏感词语义复核失败（按保留处理）：%s', type(exc).__name__)
        return True, ''
    if not content:
        return True, ''
    head = content.lstrip()
    if head.startswith(('正常讨论', '正常', '无风险')):
        log.info('语义复核判定正常讨论，丢弃事件：%s | %s', word, content[:40])
        return False, content[:80]
    return True, content[:80]


def load_lexicon(engine):
    """敏感词库：优先读 sensitive_lexicon 表（可配置），表不可用则回退环境变量 SENSITIVE_WORDS。"""
    if engine is not None:
        try:
            with engine.connect() as c:
                rows = c.execute(text("select word, risk_level from sensitive_lexicon where enabled = true order by word")).mappings().all()
                if rows:
                    return [(row['word'], row['risk_level']) for row in rows]
        except Exception as exc:
            log.warning('敏感词库表读取失败，回退环境变量：%s', type(exc).__name__)
    return [(w.strip(), 'high') for w in os.environ.get('SENSITIVE_WORDS', '达摩院').split(',') if w.strip()]


def publish_observations(client, record, rows, engine=None):
    if client is None or not rows:
        return
    source = record['source_id'] if record['source_type']=='file' else 'audio-upload'
    lexicon = load_lexicon(engine)
    text = ''.join(row['text'] for row in rows)
    live = str(record.get('source_id') or '').startswith('camera-')
    # 现场拾音每 20 秒一片，全文事件会把事件列表刷满；转写本身已落库，可在音频监测里看
    entries = [] if live else [('transcript_text',{'text':text,'recording_id':record['recording_id']})]
    for row in rows:
        hit = next(((word, level) for (word, level) in lexicon if word in (row['text'] or '')),None)
        if hit:
            word, level = hit
            kept, verdict = llm_review_sensitive(row['text'] or '', word, row.get('speaker'))
            if not kept:
                continue
            entries.append(('sensitive_word',{'word':word,'risk_level':level,'context':row['text'],'speaker':row['speaker'],
                'start_ms':row['start_ms'],'end_ms':row['end_ms'],'recording_id':record['recording_id'],
                'reason_detail':f"录音《{record.get('title','')}》片段 {(row['start_ms'] or 0)/1000:.3f}—{(row['end_ms'] or 0)/1000:.3f} 秒（{row['speaker'] or '说话人'}）：{row['text']}"
                    + (f"（语义复核：{verdict}）" if verdict else '')}))
    if os.environ.get('TALKING_ALLOWED','false').lower()!='true':
        threshold_ms = float(os.environ.get('TALKING_THRESHOLD','5'))*1000
        start,end = rows[0]['start_ms'],rows[0]['end_ms']
        intervals=[]
        for row in rows[1:]:
            if row['start_ms']-end<=500:
                end=max(end,row['end_ms'])
            else:
                intervals.append((start,end));start,end=row['start_ms'],row['end_ms']
        intervals.append((start,end))
        for start,end in intervals:
            if end-start>threshold_ms:
                entries.append(('long_talking',{'duration_sec':(end-start)/1000,'start_ms':start,'end_ms':end,'recording_id':record['recording_id']}))
    try:
        for kind,payload in entries:
            client.xadd('observations',{'type':kind,'source_id':source,'session_id':os.environ.get('SESSION_ID','session-001'),
                'occurred_at':time.time(),'payload':json.dumps(payload,ensure_ascii=False)},maxlen=2000,approximate=True)
    except Exception as exc:
        log.warning('录音结果已保存，观察推送暂不可用：%s',type(exc).__name__)


def main():
    engine = create_engine(os.environ['DATABASE_URL'], pool_pre_ping=True)
    directory = Path(os.environ.get('AUDIO_DATA_DIR','/audio-data'))
    directory.mkdir(parents=True,exist_ok=True)
    while True:
        try:
            recordings,segments=tables(engine)
            break
        except Exception:
            log.info('等待API初始化录音表');time.sleep(3)
    # 现场拾音：配置了 AUDIO_STREAM_URL 就从 RTSP 流切片入队，与上传录音走同一条链路
    try:
        from worker.stream_capture import maybe_start
        maybe_start(directory)
    except Exception as exc:
        log.warning('现场拾音启动失败：%s', type(exc).__name__)
    source_file=os.environ.get('AUDIO_SOURCE','/assets/asr_zh.wav')
    source_id=os.environ.get('AUDIO_ID','audio-001')
    client=redis.from_url(os.environ.get('REDIS_URL','redis://redis:6379/0'),socket_timeout=3)
    pipeline=None
    last_source_check=0
    while True:
        try:
            if time.monotonic()-last_source_check>30:
                try:
                    enqueue_source(engine,recordings,directory,source_file,source_id)
                except Exception as exc:
                    log.warning('预制文件源不可用，继续处理上传录音：%s',type(exc).__name__)
                last_source_check=time.monotonic()
            record=claim_recording(engine,recordings)
            if record is None:
                time.sleep(2);continue
            if pipeline is None:
                try:
                    pipeline=SpeechPipeline()
                except Exception as exc:
                    with engine.begin() as c:
                        c.execute(update(recordings).where(recordings.c.recording_id==record['recording_id'],recordings.c.claim_id==record['claim_id']).values(
                            status='failed',processed_at=now(),error='音频模型未就绪，请检查本地模型缓存后重试'))
                    log.error('音频模型加载失败：%s',type(exc).__name__);time.sleep(2);continue
            rows=process_recording(engine,recordings,segments,directory,record,pipeline)
            publish_observations(client,record,rows,engine)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            log.error('音频队列暂不可用：%s',type(exc).__name__);time.sleep(3)


if __name__=='__main__':
    main()
