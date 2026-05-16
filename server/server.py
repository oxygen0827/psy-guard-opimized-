#!/usr/bin/env python3
"""
psy-guard WebSocket 服务
支持三种 ASR 模式（ASR_PROVIDER 环境变量切换）：

  local   (默认) — FunASR WebSocket + 本地 LLM（OpenAI-compatible）
  api            — 云端 Whisper-compatible STT + 云端 LLM API
  xunfei         — 讯飞实时语音转写 WebSocket（持久流式，边说边出字）

通用 LLM 配置：
  LLM_BASE_URL  e.g. https://dashscope.aliyuncs.com/compatible-mode/v1
  LLM_MODEL     推荐 qwen-flash
  LLM_API_KEY   API Key

讯飞实时 ASR 配置（ASR_PROVIDER=xunfei 时生效）：
  XUNFEI_APPID      讯飞应用 APPID
  XUNFEI_APISECRET  讯飞 APISecret
  XUNFEI_APIKEY     讯飞 APIKey
"""

import asyncio
import array
import base64
import datetime
import hashlib
import hmac
import io
import json
import logging
import os
import ssl
import sys
import time
import traceback
import uuid
import wave
from collections import deque
from email.utils import formatdate
from urllib.parse import urlencode

import aiohttp
import aiohttp.web
import aiosqlite
import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("psy-guard")

# ─────────────────────────────────────────────────────────────
#  配置
# ─────────────────────────────────────────────────────────────
PORT             = int(os.getenv("PORT", "8097"))
ASR_PROVIDER     = os.getenv("ASR_PROVIDER", "local").lower()
FUNASR_WS_URL    = os.getenv("FUNASR_WS_URL", "ws://localhost:10095")
ASR_API_URL      = os.getenv("ASR_API_URL", "https://api.openai.com/v1")
ASR_API_KEY      = os.getenv("ASR_API_KEY", "")
ASR_MODEL        = os.getenv("ASR_MODEL", "whisper-1")
XUNFEI_APPID     = os.getenv("XUNFEI_APPID", "")
XUNFEI_APISECRET = os.getenv("XUNFEI_APISECRET", "")
XUNFEI_APIKEY    = os.getenv("XUNFEI_APIKEY", "")
LLM_BASE_URL     = os.getenv("LLM_BASE_URL", "http://localhost:8086/v1")
LLM_MODEL        = os.getenv("LLM_MODEL", "gemma-4-E4B-it-Q4_K_M.gguf")
LLM_API_KEY      = os.getenv("LLM_API_KEY", "none")

SAMPLE_RATE      = 8000
SAMPLE_WIDTH     = 2
CHANNELS         = 1

# 批处理模式参数（local/api 模式）
WINDOW_SEC       = float(os.getenv("WINDOW_SEC", "5"))
WINDOW_BYTES     = int(WINDOW_SEC * SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)

MIN_TEXT_LEN     = int(os.getenv("MIN_TEXT_LEN", "2"))
CONTEXT_MAX_CHARS= int(os.getenv("CONTEXT_MAX_CHARS", "300"))
LLM_CONCURRENCY  = int(os.getenv("LLM_CONCURRENCY", "1"))

# 流式模式触发 LLM 的文字积累阈值
STREAM_LLM_CHARS = int(os.getenv("STREAM_LLM_CHARS", "10"))

DB_PATH          = os.getenv("DB_PATH", "/data/psy-guard.db")
ADMIN_WEBHOOK_URL= os.getenv("ADMIN_WEBHOOK_URL", "")
AUDIO_SAVE_DIR   = os.getenv("AUDIO_SAVE_DIR", "/data/recordings")
HTTP_PORT        = int(os.getenv("HTTP_PORT", "8098"))

VOICEPRINT_PROVIDER = os.getenv("VOICEPRINT_PROVIDER", "tencent").lower()
VOICEPRINT_GROUP_ID = os.getenv("VOICEPRINT_GROUP_ID", "psy_guard_counselors")
VOICEPRINT_ENROLL_MIN_SEC = float(os.getenv("VOICEPRINT_ENROLL_MIN_SEC", "8"))
VOICEPRINT_VERIFY_MIN_SEC = float(os.getenv("VOICEPRINT_VERIFY_MIN_SEC", "2"))
VOICEPRINT_MAX_SEC = float(os.getenv("VOICEPRINT_MAX_SEC", "30"))

TENCENT_SECRET_ID  = os.getenv("TENCENT_SECRET_ID", "")
TENCENT_SECRET_KEY = os.getenv("TENCENT_SECRET_KEY", "")
TENCENT_REGION     = os.getenv("TENCENT_REGION", "ap-guangzhou")

XFYUN_VOICEPRINT_APPID     = os.getenv("XFYUN_VOICEPRINT_APPID", "")
XFYUN_VOICEPRINT_APIKEY    = os.getenv("XFYUN_VOICEPRINT_APIKEY", "")
XFYUN_VOICEPRINT_APISECRET = os.getenv("XFYUN_VOICEPRINT_APISECRET", "")
XFYUN_VOICEPRINT_SERVICE_ID= os.getenv("XFYUN_VOICEPRINT_SERVICE_ID", "s1aa729d0")
XFYUN_VOICEPRINT_HOST      = os.getenv("XFYUN_VOICEPRINT_HOST", "api.xf-yun.com")

# ─────────────────────────────────────────────────────────────
#  Admin session tracking (globals)
# ─────────────────────────────────────────────────────────────
active_sessions: dict = {}   # session_id → {session_id, client_ip, started_at, pcm_path}
admin_connections: set = set()
voiceprint_registry: dict[tuple[str, str], dict] = {}

async def broadcast_admin(msg: dict):
    """Broadcast a JSON message to all connected admin WebSocket clients."""
    global admin_connections
    if not admin_connections:
        return
    text = json.dumps(msg, ensure_ascii=False)
    dead = set()
    for ws in list(admin_connections):
        try:
            await ws.send(text)
        except Exception:
            dead.add(ws)
    admin_connections.difference_update(dead)

# ─────────────────────────────────────────────────────────────
#  语料库加载 + System Prompt 动态构建
# ─────────────────────────────────────────────────────────────
def _load_corpus() -> list[dict]:
    path = os.path.join(os.path.dirname(__file__), "corpus.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"[corpus] 语料库加载失败，使用内置规则: {e}")
        return []

def _examples(corpus: list[dict], type_name: str, n: int = 6) -> str:
    items = [i["text"] for i in corpus if i.get("type") == type_name]
    return "\n".join(f"  ·{t}" for t in items[:n])

def _build_system_prompt(corpus: list[dict]) -> str:
    harm    = _examples(corpus, "严重危害行为", 6)
    politics= _examples(corpus, "政治/意识形态敏感", 4)
    ethics  = _examples(corpus, "违背心理咨询伦理", 6)
    tech    = _examples(corpus, "技术不当", 6)

    corpus_note = f"""
## 语料库参考示例（各类别典型句式）

### high — 咨询师严重危害行为（鼓励/诱导来访者自伤、轻生或极端行动）
{harm if harm else "  （语料库未加载）"}

### high — 政治/意识形态敏感（任一方发表）
{politics if politics else "  （语料库未加载）"}

### medium — 咨询师违背心理咨询伦理
{ethics if ethics else "  （语料库未加载）"}

### low — 咨询师技术不当（使用无效或有害的咨询技术）
{tech if tech else "  （语料库未加载）"}
""".strip()

    return f"""你是心理咨询室实时安全监控助手，同时监控来访者和咨询师双方的发言，识别危机信号与专业违规行为。

## 核心原则
- 只根据「对话片段」本身的文字内容判断，不推断、联想或延伸
- 来访者与咨询师均可触发预警，需区分发言方
- 如果当前句子本身没有危机信号，必须输出 null，即使历史上文曾出现过危机词汇
- 每句话独立评估，不因历史预警而对后续句子持续触发

## 预警等级

### high（立即触发）

**① 来访者危机信号**
- 来访者以第一人称表达当前或近期的自杀/自伤/伤害他人意图
- 来访者描述正在遭受虐待、性侵（非回顾已解决事件）
- 示例：「我不想活了」「我准备好了刀」「我想杀了他」

**② 咨询师严重危害行为**
- 咨询师向来访者鼓励、诱导或合理化自伤/自杀/极端暴力行为

**③ 政治/意识形态敏感内容**
- 任一方发表涉及政治敏感、危害国家安全或煽动对立的言论

### medium（警示）

**① 来访者深度绝望**
- 来访者表达强烈绝望感、无价值感、反复表达孤立无援

**② 咨询师违背心理咨询伦理**
- 咨询师与来访者发展双重关系、泄露隐私、索取利益、保证疗效、建议停药等违规行为

### low（关注，记录但不推通知）

**① 来访者轻微负面指标**
- 持续失眠、社交退缩、轻微负面词汇，需持续观察

**② 咨询师技术不当**
- 咨询师使用否定、评判、说教、比较等有损咨询关系的不当技术

{corpus_note}

## 不触发预警的情况
- 咨询师在做标准风险评估提问（如"你有没有想过伤害自己"）
- 讨论过去已解决的经历（降级或不触发）
- 学术/理论讨论、文学/影视作品讨论
- 当前句子是正常、积极或中性内容，即使上文有危机词汇

## 输出规则
- 无危机信号：输出 null（不含任何其他内容）
- 检测到信号：输出如下 JSON，不含其他内容
{{"level":"high|medium|low","keyword":"触发词或违规类型","suggestion":"给咨询师的一句话干预建议"}}"""

_CORPUS = _load_corpus()
SYSTEM_PROMPT = _build_system_prompt(_CORPUS)
log.info(f"[corpus] 已加载 {len(_CORPUS)} 条语料，System Prompt 长度 {len(SYSTEM_PROMPT)} 字")

# ─────────────────────────────────────────────────────────────
#  数据库初始化
# ─────────────────────────────────────────────────────────────
async def init_db():
    if not DB_PATH:
        return None
    try:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        db = await aiosqlite.connect(DB_PATH)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id TEXT PRIMARY KEY, session_id TEXT, level TEXT,
                keyword TEXT, text TEXT, suggestion TEXT, timestamp REAL)
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transcripts (
                id TEXT PRIMARY KEY, session_id TEXT, text TEXT, timestamp REAL)
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS voiceprints (
                speaker_id TEXT NOT NULL,
                speaker_name TEXT,
                provider TEXT NOT NULL,
                provider_voiceprint_id TEXT NOT NULL,
                group_id TEXT,
                created_at REAL,
                updated_at REAL,
                PRIMARY KEY (speaker_id, provider)
            )
        """)
        await db.commit()
        log.info(f"[DB] SQLite ready: {DB_PATH}")
        return db
    except Exception as e:
        log.warning(f"[DB] init failed, running without persistence: {e}")
        return None

# ─────────────────────────────────────────────────────────────
#  声纹认证（腾讯云主方案 + 讯飞备选）
# ─────────────────────────────────────────────────────────────
class VoiceprintError(Exception):
    def __init__(self, code: str, message: str, *, detail: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or message


def _voiceprint_provider_name() -> str:
    if VOICEPRINT_PROVIDER in ("tencent", "xfyun", "off"):
        return VOICEPRINT_PROVIDER
    return "off"


def _pcm_duration_sec(pcm: bytes) -> float:
    return len(pcm) / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)


def _pcm_rms(pcm: bytes) -> int:
    if not pcm:
        return 0
    try:
        pcm = pcm[:len(pcm) - (len(pcm) % SAMPLE_WIDTH)]
        samples = array.array("h")
        samples.frombytes(pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        if not samples:
            return 0
        mean_square = sum(int(s) * int(s) for s in samples) / len(samples)
        return int(mean_square ** 0.5)
    except Exception:
        return 0


def _pcm8_to_pcm16(pcm: bytes) -> bytes:
    pcm = pcm[:len(pcm) - (len(pcm) % SAMPLE_WIDTH)]
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return b""
    out = array.array("h")
    for i, sample in enumerate(samples):
        next_sample = samples[i + 1] if i + 1 < len(samples) else sample
        out.append(sample)
        out.append(int((int(sample) + int(next_sample)) / 2))
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


def _wav_bytes(pcm: bytes, sample_rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _validate_voiceprint_audio(pcm: bytes, stage: str) -> dict:
    duration = _pcm_duration_sec(pcm)
    min_sec = VOICEPRINT_ENROLL_MIN_SEC if stage == "enroll" else VOICEPRINT_VERIFY_MIN_SEC
    if duration < min_sec:
        raise VoiceprintError(
            "audio_too_short",
            f"voiceprint audio too short: {duration:.1f}s < {min_sec:.1f}s",
        )
    if duration > VOICEPRINT_MAX_SEC:
        raise VoiceprintError(
            "audio_too_long",
            f"voiceprint audio too long: {duration:.1f}s > {VOICEPRINT_MAX_SEC:.1f}s",
        )
    rms = _pcm_rms(pcm)
    if rms < int(os.getenv("VOICEPRINT_MIN_RMS", "20")):
        raise VoiceprintError("no_human_voice", "voiceprint audio is too quiet")
    return {"duration_sec": round(duration, 1), "rms": rms}


def _json_b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _utf8_limit(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8")[:max_bytes]
    return raw.decode("utf-8", errors="ignore")


class TencentVoiceprintProvider:
    name = "tencent"
    endpoint = "https://asr.tencentcloudapi.com"
    host = "asr.tencentcloudapi.com"
    service = "asr"
    version = "2019-06-14"

    def _check_config(self):
        if not TENCENT_SECRET_ID or not TENCENT_SECRET_KEY:
            raise VoiceprintError("not_configured", "Tencent voiceprint credentials are not configured")

    @staticmethod
    def _sign(secret_key: str, date: str, service: str, string_to_sign: str) -> str:
        def _hmac(key, msg):
            return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

        secret_date = _hmac(("TC3" + secret_key).encode("utf-8"), date)
        secret_service = _hmac(secret_date, service)
        secret_signing = _hmac(secret_service, "tc3_request")
        return hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    async def _call(self, session: aiohttp.ClientSession, action: str, payload: dict) -> dict:
        self._check_config()
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        timestamp = int(time.time())
        date = datetime.datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%d")
        content_type = "application/json; charset=utf-8"
        canonical_headers = f"content-type:{content_type}\nhost:{self.host}\n"
        signed_headers = "content-type;host"
        canonical_request = "\n".join([
            "POST", "/", "", canonical_headers, signed_headers,
            hashlib.sha256(body.encode("utf-8")).hexdigest(),
        ])
        credential_scope = f"{date}/{self.service}/tc3_request"
        string_to_sign = "\n".join([
            "TC3-HMAC-SHA256",
            str(timestamp),
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ])
        signature = self._sign(TENCENT_SECRET_KEY, date, self.service, string_to_sign)
        authorization = (
            "TC3-HMAC-SHA256 "
            f"Credential={TENCENT_SECRET_ID}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        headers = {
            "Authorization": authorization,
            "Content-Type": content_type,
            "Host": self.host,
            "X-TC-Action": action,
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Version": self.version,
            "X-TC-Region": TENCENT_REGION,
        }
        async with session.post(
            self.endpoint,
            data=body.encode("utf-8"),
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=45),
        ) as resp:
            raw = await resp.text()
            try:
                data = json.loads(raw)
            except Exception:
                raise VoiceprintError("bad_response", f"Tencent voiceprint HTTP {resp.status}", detail=raw[:500])
            response = data.get("Response", {})
            if resp.status != 200 or response.get("Error"):
                err = response.get("Error", {})
                code = err.get("Code", f"http_{resp.status}")
                msg = err.get("Message", raw[:300])
                raise VoiceprintError(code, msg)
            return response

    async def enroll(self, session: aiohttp.ClientSession, speaker_id: str, speaker_name: str, pcm8: bytes) -> dict:
        pcm16 = _pcm8_to_pcm16(pcm8)
        wav_data = _wav_bytes(pcm16, sample_rate=16000)
        payload = {
            "VoiceFormat": 1,
            "SampleRate": 16000,
            "Data": _json_b64(wav_data),
            "SpeakerNick": _utf8_limit(speaker_name or speaker_id, 32),
            "GroupId": VOICEPRINT_GROUP_ID,
        }
        response = await self._call(session, "VoicePrintEnroll", payload)
        data = response.get("Data", {})
        voiceprint_id = data.get("VoicePrintId")
        if not voiceprint_id:
            raise VoiceprintError("bad_response", "Tencent enroll response did not include VoicePrintId")
        return {"provider_voiceprint_id": voiceprint_id, "raw": data}

    async def verify(self, session: aiohttp.ClientSession, provider_voiceprint_id: str, pcm8: bytes) -> dict:
        pcm16 = _pcm8_to_pcm16(pcm8)
        wav_data = _wav_bytes(pcm16, sample_rate=16000)
        payload = {
            "VoiceFormat": 1,
            "SampleRate": 16000,
            "VoicePrintId": provider_voiceprint_id,
            "Data": _json_b64(wav_data),
        }
        response = await self._call(session, "VoicePrintVerify", payload)
        data = response.get("Data", {})
        score = float(data.get("Score", 0) or 0)
        verified = int(data.get("Decision", 0) or 0) == 1
        return {"verified": verified, "score": score, "raw": data}

    async def delete(self, session: aiohttp.ClientSession, provider_voiceprint_id: str) -> None:
        await self._call(session, "VoicePrintDelete", {"VoicePrintId": provider_voiceprint_id})


class XfyunVoiceprintProvider:
    name = "xfyun"

    def __init__(self):
        self.service_id = XFYUN_VOICEPRINT_SERVICE_ID
        self.path = f"/v1/private/{self.service_id}"
        self.endpoint = f"https://{XFYUN_VOICEPRINT_HOST}{self.path}"
        self._group_ready = False

    def _check_config(self):
        if not (XFYUN_VOICEPRINT_APPID and XFYUN_VOICEPRINT_APIKEY and XFYUN_VOICEPRINT_APISECRET):
            raise VoiceprintError("not_configured", "Xfyun voiceprint credentials are not configured")

    def _auth_url(self) -> str:
        self._check_config()
        date = formatdate(timeval=None, localtime=False, usegmt=True)
        sign_origin = f"host: {XFYUN_VOICEPRINT_HOST}\ndate: {date}\nPOST {self.path} HTTP/1.1"
        sig = base64.b64encode(
            hmac.new(XFYUN_VOICEPRINT_APISECRET.encode(), sign_origin.encode(), hashlib.sha256).digest()
        ).decode()
        auth = base64.b64encode(
            f'api_key="{XFYUN_VOICEPRINT_APIKEY}", algorithm="hmac-sha256", '
            f'headers="host date request-line", signature="{sig}"'.encode()
        ).decode()
        return f"{self.endpoint}?{urlencode({'authorization': auth, 'date': date, 'host': XFYUN_VOICEPRINT_HOST})}"

    @staticmethod
    def _result_spec(name: str) -> dict:
        return {name: {"encoding": "utf8", "compress": "raw", "format": "json"}}

    def _base_body(self, func: str, params: dict) -> dict:
        return {
            "header": {"app_id": XFYUN_VOICEPRINT_APPID, "status": 3},
            "parameter": {
                self.service_id: {
                    "func": func,
                    **params,
                }
            },
        }

    def _audio_resource(self, pcm8: bytes) -> dict:
        pcm16 = _pcm8_to_pcm16(pcm8)
        return {
            "encoding": "raw",
            "sample_rate": 16000,
            "channels": 1,
            "bit_depth": 16,
            "status": 3,
            "audio": _json_b64(pcm16),
        }

    async def _call(self, session: aiohttp.ClientSession, body: dict, result_key: str | None = None) -> dict:
        url = self._auth_url()
        headers = {
            "content-type": "application/json",
            "host": XFYUN_VOICEPRINT_HOST,
            "appid": XFYUN_VOICEPRINT_APPID,
        }
        async with session.post(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=45),
        ) as resp:
            raw = await resp.text()
            try:
                data = json.loads(raw)
            except Exception:
                raise VoiceprintError("bad_response", f"Xfyun voiceprint HTTP {resp.status}", detail=raw[:500])
            header = data.get("header", {})
            code = int(header.get("code", -1))
            if resp.status != 200 or code != 0:
                raise VoiceprintError(str(code), header.get("message", raw[:300]))
            if not result_key:
                return data
            text = data.get("payload", {}).get(result_key, {}).get("text", "")
            if not text:
                return {}
            try:
                return json.loads(base64.b64decode(text).decode("utf-8"))
            except Exception as e:
                raise VoiceprintError("bad_response", f"Xfyun {result_key} decode failed: {e}")

    async def _ensure_group(self, session: aiohttp.ClientSession):
        if self._group_ready:
            return
        body = self._base_body("createGroup", {
            "groupId": VOICEPRINT_GROUP_ID[:32],
            "groupName": VOICEPRINT_GROUP_ID[:32],
            "groupInfo": "psy-guard counselors",
            **self._result_spec("createGroupRes"),
        })
        try:
            await self._call(session, body, "createGroupRes")
        except VoiceprintError as e:
            if "already" not in e.message.lower() and "存在" not in e.message:
                raise
        self._group_ready = True

    @staticmethod
    def _feature_id(speaker_id: str) -> str:
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
        cleaned = "".join(c if c in allowed else "_" for c in speaker_id)
        if cleaned and len(cleaned) <= 32:
            return cleaned
        digest = hashlib.sha1(speaker_id.encode("utf-8")).hexdigest()[:20]
        prefix = (cleaned[:10].strip("_") or "spk")
        return f"{prefix}_{digest}"[:32]

    async def enroll(self, session: aiohttp.ClientSession, speaker_id: str, speaker_name: str, pcm8: bytes) -> dict:
        await self._ensure_group(session)
        feature_id = self._feature_id(speaker_id)
        body = self._base_body("createFeature", {
            "groupId": VOICEPRINT_GROUP_ID[:32],
            "featureId": feature_id,
            "featureInfo": (speaker_name or speaker_id)[:256],
            **self._result_spec("createFeatureRes"),
        })
        body["payload"] = {"resource": self._audio_resource(pcm8)}
        try:
            result = await self._call(session, body, "createFeatureRes")
        except VoiceprintError as e:
            if "exist" not in e.message.lower() and "存在" not in e.message:
                raise
            body = self._base_body("updateFeature", {
                "groupId": VOICEPRINT_GROUP_ID[:32],
                "featureId": feature_id,
                "featureInfo": (speaker_name or speaker_id)[:256],
                **self._result_spec("updateFeatureRes"),
            })
            body["payload"] = {"resource": self._audio_resource(pcm8)}
            result = await self._call(session, body, "updateFeatureRes")
        return {"provider_voiceprint_id": result.get("featureId", feature_id), "raw": result}

    async def verify(self, session: aiohttp.ClientSession, provider_voiceprint_id: str, pcm8: bytes) -> dict:
        await self._ensure_group(session)
        body = self._base_body("searchScoreFea", {
            "groupId": VOICEPRINT_GROUP_ID[:32],
            "dstFeatureId": provider_voiceprint_id,
            **self._result_spec("searchScoreFeaRes"),
        })
        body["payload"] = {"resource": self._audio_resource(pcm8)}
        result = await self._call(session, body, "searchScoreFeaRes")
        score = float(result.get("score", 0) or 0)
        threshold = float(os.getenv("XFYUN_VOICEPRINT_THRESHOLD", "0.75"))
        return {"verified": score >= threshold, "score": score, "threshold": threshold, "raw": result}

    async def delete(self, session: aiohttp.ClientSession, provider_voiceprint_id: str) -> None:
        await self._ensure_group(session)
        body = self._base_body("deleteFeature", {
            "groupId": VOICEPRINT_GROUP_ID[:32],
            "featureId": provider_voiceprint_id,
            **self._result_spec("deleteFeatureRes"),
        })
        await self._call(session, body, "deleteFeatureRes")


def get_voiceprint_provider():
    provider = _voiceprint_provider_name()
    if provider == "tencent":
        return TencentVoiceprintProvider()
    if provider == "xfyun":
        return XfyunVoiceprintProvider()
    raise VoiceprintError("disabled", "voiceprint provider is disabled")


async def save_voiceprint(db, speaker_id: str, speaker_name: str, provider: str, provider_voiceprint_id: str):
    now = time.time()
    item = {
        "speaker_id": speaker_id,
        "speaker_name": speaker_name,
        "provider": provider,
        "provider_voiceprint_id": provider_voiceprint_id,
        "group_id": VOICEPRINT_GROUP_ID,
        "created_at": now,
        "updated_at": now,
    }
    voiceprint_registry[(speaker_id, provider)] = item
    if not db:
        return
    await db.execute("""
        INSERT INTO voiceprints (
            speaker_id, speaker_name, provider, provider_voiceprint_id,
            group_id, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(speaker_id, provider) DO UPDATE SET
            speaker_name=excluded.speaker_name,
            provider_voiceprint_id=excluded.provider_voiceprint_id,
            group_id=excluded.group_id,
            updated_at=excluded.updated_at
    """, (
        speaker_id, speaker_name, provider, provider_voiceprint_id,
        VOICEPRINT_GROUP_ID, now, now,
    ))
    await db.commit()


async def load_voiceprint(db, speaker_id: str, provider: str) -> dict | None:
    cached = voiceprint_registry.get((speaker_id, provider))
    if cached:
        return cached
    if not db:
        return None
    async with db.execute(
        "SELECT speaker_id, speaker_name, provider, provider_voiceprint_id, group_id, created_at, updated_at "
        "FROM voiceprints WHERE speaker_id=? AND provider=?",
        (speaker_id, provider),
    ) as cursor:
        row = await cursor.fetchone()
    if not row:
        return None
    item = {
        "speaker_id": row[0],
        "speaker_name": row[1] or "",
        "provider": row[2],
        "provider_voiceprint_id": row[3],
        "group_id": row[4],
        "created_at": row[5],
        "updated_at": row[6],
    }
    voiceprint_registry[(speaker_id, provider)] = item
    return item


async def send_json(websocket, payload: dict):
    await websocket.send(json.dumps(payload, ensure_ascii=False))


async def finish_voiceprint_capture(
    websocket,
    http_session: aiohttp.ClientSession,
    db,
    voice_state: dict,
    voice_identity: dict,
    session_id: str,
):
    stage = voice_state.get("stage")
    speaker_id = voice_state.get("speaker_id") or "counselor_default"
    speaker_name = voice_state.get("speaker_name") or "咨询师"
    pcm = bytes(voice_state.get("buffer", b""))
    voice_state.update({"stage": None, "buffer": bytearray(), "speaker_id": "", "speaker_name": ""})

    provider_name = _voiceprint_provider_name()
    try:
        meta = _validate_voiceprint_audio(pcm, stage or "verify")
        provider = get_voiceprint_provider()
        if stage == "enroll":
            result = await provider.enroll(http_session, speaker_id, speaker_name, pcm)
            provider_voiceprint_id = result["provider_voiceprint_id"]
            await save_voiceprint(db, speaker_id, speaker_name, provider.name, provider_voiceprint_id)
            payload = {
                "type": "voiceprint_result",
                "stage": "enroll",
                "provider": provider.name,
                "speaker_id": speaker_id,
                "speaker_name": speaker_name,
                "provider_voiceprint_id": provider_voiceprint_id,
                "enrolled": True,
                **meta,
            }
        elif stage == "verify":
            record = await load_voiceprint(db, speaker_id, provider.name)
            if not record:
                raise VoiceprintError("not_enrolled", f"speaker {speaker_id} is not enrolled for {provider.name}")
            result = await provider.verify(http_session, record["provider_voiceprint_id"], pcm)
            verified = bool(result.get("verified"))
            payload = {
                "type": "voiceprint_result",
                "stage": "verify",
                "provider": provider.name,
                "speaker_id": speaker_id,
                "speaker_name": record.get("speaker_name") or speaker_name,
                "verified": verified,
                "score": result.get("score", 0),
                "threshold": result.get("threshold"),
                **meta,
            }
            voice_identity.clear()
            voice_identity.update({
                "voiceprint_verified": verified,
                "provider": provider.name,
                "score": result.get("score", 0),
                "speaker_id": speaker_id,
                "verified_at": time.time(),
            })
        else:
            raise VoiceprintError("invalid_stage", "voiceprint capture stage is invalid")
        await send_json(websocket, payload)
        asyncio.create_task(broadcast_admin({**payload, "session_id": session_id}))
    except VoiceprintError as e:
        log.warning(f"[Voiceprint/{provider_name}] {stage} failed: {e.code} {e.detail}")
        payload = {
            "type": "voiceprint_error",
            "stage": stage or "unknown",
            "provider": provider_name,
            "speaker_id": speaker_id,
            "message": e.code,
            "detail": e.message,
        }
        await send_json(websocket, payload)
        asyncio.create_task(broadcast_admin({**payload, "session_id": session_id}))
    except Exception as e:
        log.error(f"[Voiceprint/{provider_name}] {stage} crashed: {e}\n{traceback.format_exc()}")
        await send_json(websocket, {
            "type": "voiceprint_error",
            "stage": stage or "unknown",
            "provider": provider_name,
            "speaker_id": speaker_id,
            "message": "internal_error",
            "detail": str(e),
        })


async def handle_voiceprint_command(
    websocket,
    http_session: aiohttp.ClientSession,
    db,
    voice_state: dict,
    voice_identity: dict,
    session_id: str,
    cmd: dict,
) -> bool:
    msg_type = cmd.get("type")
    if msg_type not in (
        "voiceprint_enroll_start", "voiceprint_enroll_stop",
        "voiceprint_verify_start", "voiceprint_verify_stop",
    ):
        return False
    if msg_type.endswith("_start"):
        stage = "enroll" if "enroll" in msg_type else "verify"
        if _voiceprint_provider_name() == "off":
            await send_json(websocket, {
                "type": "voiceprint_error",
                "stage": stage,
                "provider": "off",
                "message": "disabled",
            })
            return True
        voice_state.update({
            "stage": stage,
            "speaker_id": str(cmd.get("speaker_id") or "counselor_default"),
            "speaker_name": str(cmd.get("speaker_name") or "咨询师"),
            "buffer": bytearray(),
            "started_at": time.time(),
        })
        payload = {
            "type": "voiceprint_status",
            "stage": stage,
            "provider": _voiceprint_provider_name(),
            "speaker_id": voice_state["speaker_id"],
            "status": "recording",
        }
        await send_json(websocket, payload)
        asyncio.create_task(broadcast_admin({**payload, "session_id": session_id}))
        return True

    expected_stage = "enroll" if "enroll" in msg_type else "verify"
    if voice_state.get("stage") != expected_stage:
        await send_json(websocket, {
            "type": "voiceprint_error",
            "stage": expected_stage,
            "provider": _voiceprint_provider_name(),
            "message": "not_recording",
        })
        return True
    await finish_voiceprint_capture(websocket, http_session, db, voice_state, voice_identity, session_id)
    return True

# ─────────────────────────────────────────────────────────────
#  ASR — 本地模式（FunASR WebSocket）
# ─────────────────────────────────────────────────────────────
async def transcribe_local(pcm: bytes) -> str:
    log.info(f"[ASR/local] {len(pcm)} bytes ({len(pcm)/SAMPLE_RATE/SAMPLE_WIDTH:.1f}s)")
    try:
        async with websockets.connect(FUNASR_WS_URL, max_size=10*1024*1024, open_timeout=10) as ws:
            config = {
                "mode": "2pass", "wav_name": "audio", "wav_format": "pcm",
                "is_speaking": True, "itn": True, "audio_fs": SAMPLE_RATE,
                "chunk_size": [5, 10, 5], "chunk_interval": 10,
            }
            await ws.send(json.dumps(config))
            chunk = 960 * SAMPLE_WIDTH
            for i in range(0, len(pcm), chunk):
                await ws.send(pcm[i:i+chunk])
            await ws.send(json.dumps({"is_speaking": False}))
            text = ""
            async with asyncio.timeout(30):
                async for msg in ws:
                    data = json.loads(msg)
                    if data.get("is_final") and data.get("mode") == "2pass-offline":
                        text = data.get("text", "").strip()
                        break
                    if data.get("is_final") and not data.get("mode"):
                        break
            log.info(f"[ASR/local] result: {text!r}")
            return text
    except Exception as e:
        log.error(f"[ASR/local] Error: {e}")
        return ""

# ─────────────────────────────────────────────────────────────
#  ASR — API 模式（OpenAI Whisper-compatible）
# ─────────────────────────────────────────────────────────────
async def transcribe_api(pcm: bytes, session: aiohttp.ClientSession) -> str:
    log.info(f"[ASR/api] {len(pcm)} bytes ({len(pcm)/SAMPLE_RATE/SAMPLE_WIDTH:.1f}s)")
    try:
        wav_buf = io.BytesIO()
        with wave.open(wav_buf, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(SAMPLE_WIDTH)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(pcm)
        wav_data = wav_buf.getvalue()
        form = aiohttp.FormData()
        form.add_field("file", wav_data, filename="audio.wav", content_type="audio/wav")
        form.add_field("model", ASR_MODEL)
        form.add_field("language", "zh")
        headers = {"Authorization": f"Bearer {ASR_API_KEY}"}
        url = f"{ASR_API_URL.rstrip('/')}/audio/transcriptions"
        async with session.post(url, data=form, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status == 200:
                data = await resp.json()
                text = data.get("text", "").strip()
                log.info(f"[ASR/api] result: {text!r}")
                return text
            body = await resp.text()
            log.warning(f"[ASR/api] HTTP {resp.status}: {body[:200]}")
            return ""
    except Exception as e:
        log.error(f"[ASR/api] Error: {e}")
        return ""

# ─────────────────────────────────────────────────────────────
#  讯飞鉴权 URL（HMAC-SHA256）
# ─────────────────────────────────────────────────────────────
_XUNFEI_HOST = "ws-api.xfyun.cn"
_XUNFEI_PATH = "/v2/iat"

def _xunfei_auth_url() -> str:
    date = formatdate(timeval=None, localtime=False, usegmt=True)
    sign_origin = f"host: {_XUNFEI_HOST}\ndate: {date}\nGET {_XUNFEI_PATH} HTTP/1.1"
    sig = base64.b64encode(
        hmac.new(XUNFEI_APISECRET.encode(), sign_origin.encode(), hashlib.sha256).digest()
    ).decode()
    auth = base64.b64encode(
        f'api_key="{XUNFEI_APIKEY}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{sig}"'.encode()
    ).decode()
    params = urlencode({"authorization": auth, "date": date, "host": _XUNFEI_HOST})
    return f"wss://{_XUNFEI_HOST}{_XUNFEI_PATH}?{params}"

# ─────────────────────────────────────────────────────────────
#  讯飞流式 ASR 会话（每个客户端一个持久 WebSocket 连接）
# ─────────────────────────────────────────────────────────────
class XunfeiStreamSession:
    """
    持久讯飞 IAT WebSocket 会话。
    单一主循环处理连接、发送、接收和重连，避免竞态。
    """
    CHUNK_SIZE      = 1280   # 80ms @ 8kHz 16bit mono
    SESSION_MAX_SEC = 55     # 接近讯飞 60s 限制前重连
    MAX_BUF_BYTES   = 1280 * 40  # ~3.2s @ 8kHz；覆盖讯飞重连期间（~1-2s），防止音频丢失

    def __init__(self, on_text, on_interim=None):
        self._on_text        = on_text
        self._on_interim     = on_interim
        self._buf            = bytearray()
        self._running        = False
        self._task           = None
        self._sentence_buf: dict[int, str] = {}  # 跨重连保留，stop时 flush
        self._needs_reconnect = False  # 句子完成后主动触发重连
        self._text_tasks: set[asyncio.Task] = set()  # 追踪 on_text LLM 任务，stop 时确保完成

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def feed(self, pcm: bytes):
        if self._running:
            self._buf.extend(pcm)

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._flush_pending()  # 停录时把未确认句子全部输出
        # 等待所有 on_text LLM 任务完成（最多10s），防止 http_session 关闭前漏报
        pending = {t for t in self._text_tasks if not t.done()}
        if pending:
            done, still_pending = await asyncio.wait(pending, timeout=10)
            for t in still_pending:
                t.cancel()
        self._text_tasks.clear()
        log.info("[ASR/stream] session closed")

    async def _flush_pending(self):
        """把 sentence_buf 里所有未收到 ls=True 的句子强制输出，防止丢内容。"""
        for sn in sorted(self._sentence_buf.keys()):
            text = self._sentence_buf.pop(sn, "").strip()
            if text:
                log.info(f"[ASR/stream] flush pending sn={sn}: {text!r}")
                try:
                    await self._on_text(text)
                except Exception:
                    pass

    @staticmethod
    def _make_ssl():
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    @staticmethod
    def _silence_frame() -> bytes:
        """全零 PCM 静音帧，保持连接活跃，允许讯飞 VAD 正常检测静音边界。"""
        return bytes(XunfeiStreamSession.CHUNK_SIZE)

    async def _run(self):
        """主循环：连接 → 持续发音频 → 断连自动重连，永不退出直到 stop()。"""
        while self._running:
            ws = None
            try:
                url = _xunfei_auth_url()
                ws = await websockets.connect(
                    url, max_size=10*1024*1024, open_timeout=10, ssl=self._make_ssl()
                )
                log.info("[ASR/stream] iFlytek session opened")
                # 重连期间积压的旧音频会导致 latency 随时间线性增长；
                # 每次新 session 只保留最近 MAX_BUF_BYTES，丢弃早于此的积压。
                if len(self._buf) > self.MAX_BUF_BYTES:
                    skipped = len(self._buf) - self.MAX_BUF_BYTES
                    del self._buf[:skipped]
                    log.info(f"[ASR/stream] dropped {skipped//32}ms stale audio to maintain real-time latency")
                first    = True
                t_start  = time.time()
                recv_task = asyncio.create_task(self._recv_loop(ws))

                while self._running:
                    # 句子完成后主动重连，不等讯飞超时
                    if self._needs_reconnect:
                        self._needs_reconnect = False
                        log.info("[ASR/stream] sentence done, reconnecting for next utterance...")
                        break
                    # 接近 55s 超时时主动断开重连
                    if time.time() - t_start > self.SESSION_MAX_SEC:
                        log.info("[ASR/stream] session timeout, reconnecting...")
                        break

                    chunk = None
                    if len(self._buf) >= self.CHUNK_SIZE:
                        chunk = bytes(self._buf[:self.CHUNK_SIZE])
                        del self._buf[:self.CHUNK_SIZE]
                    else:
                        chunk = self._silence_frame()

                    status = 0 if first else 1
                    if first:
                        frame = {
                            "common": {"app_id": XUNFEI_APPID},
                            "business": {
                                "language": "zh_cn", "domain": "iat",
                                "accent": "mandarin", "vad_eos": 500,
                            },
                            "data": {
                                "status": status,
                                "format": "audio/L16;rate=8000",
                                "encoding": "raw",
                                "audio": base64.b64encode(chunk).decode(),
                            },
                        }
                        first = False
                    else:
                        frame = {
                            "data": {
                                "status": status,
                                "format": "audio/L16;rate=8000",
                                "encoding": "raw",
                                "audio": base64.b64encode(chunk).decode(),
                            }
                        }
                    await ws.send(json.dumps(frame))
                    await asyncio.sleep(0.04)

                recv_task.cancel()
                try:
                    await recv_task
                except Exception:
                    pass
                await self._flush_pending()  # 重连前 flush，防止跨 session 丢句子
                try:
                    await ws.close()
                except Exception:
                    pass

            except asyncio.CancelledError:
                if ws:
                    try:
                        await ws.close()
                    except Exception:
                        pass
                raise
            except Exception as e:
                log.warning(f"[ASR/stream] connection error: {e}, reconnecting in 0.3s...")
                if ws:
                    try:
                        await ws.close()
                    except Exception:
                        pass
                await asyncio.sleep(0.3)

    async def _recv_loop(self, ws):
        """接收讯飞识别结果，正确处理 pgs/sn/ls，只在句子最终确认后回调。"""
        try:
            async for msg in ws:
                data = json.loads(msg)
                code = data.get("code", -1)
                if code != 0:
                    log.warning(f"[ASR/stream] code={code} msg={data.get('message')}")
                    continue
                result = data.get("data", {}).get("result")
                if not result:
                    continue

                sn  = result.get("sn", 0)
                pgs = result.get("pgs", "apd")  # "apd"=追加 "rpl"=替换本句
                ls  = result.get("ls", False)    # True=本句已最终确认
                ws_list = result.get("ws", [])
                text_chunk = "".join(
                    cw.get("w", "")
                    for w in ws_list
                    for cw in w.get("cw", [])
                )

                if pgs == "rpl":
                    self._sentence_buf[sn] = text_chunk
                else:
                    self._sentence_buf[sn] = self._sentence_buf.get(sn, "") + text_chunk

                log.info(f"[ASR/stream] sn={sn} pgs={pgs} ls={ls} → {self._sentence_buf[sn]!r}")

                # 实时推送中间结果，让 iOS 端即时看到正在识别的文字
                if not ls and self._on_interim:
                    current = self._sentence_buf.get(sn, "").strip()
                    if current:
                        asyncio.create_task(self._on_interim(current))

                if ls:
                    # 先把比当前 sn 更早的孤立句子全部输出
                    for orphan_sn in sorted(k for k in self._sentence_buf if k < sn):
                        orphan_text = self._sentence_buf.pop(orphan_sn, "").strip()
                        if orphan_text:
                            t = asyncio.create_task(self._on_text(orphan_text))
                            self._text_tasks.add(t)
                            t.add_done_callback(self._text_tasks.discard)
                    full_text = self._sentence_buf.pop(sn, "").strip()
                    if full_text:
                        t = asyncio.create_task(self._on_text(full_text))
                        self._text_tasks.add(t)
                        t.add_done_callback(self._text_tasks.discard)
                    # 句子已全部确认，主动触发重连（不等讯飞 10s 超时）
                    if not self._sentence_buf:
                        self._needs_reconnect = True
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning(f"[ASR/stream] recv error: {e}")


# ─────────────────────────────────────────────────────────────
#  统一 transcribe 入口（批处理模式，local/api）
# ─────────────────────────────────────────────────────────────
async def transcribe(pcm: bytes, http_session: aiohttp.ClientSession) -> str:
    if ASR_PROVIDER == "api":
        return await transcribe_api(pcm, http_session)
    return await transcribe_local(pcm)

# ─────────────────────────────────────────────────────────────
#  LLM 分析
# ─────────────────────────────────────────────────────────────
async def analyze(session: aiohttp.ClientSession, context: str, new_text: str) -> dict | None:
    user_content = f"对话片段：\n{new_text}"
    if context:
        user_content = f"历史上文（供参考）：\n{context}\n\n{user_content}"
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_content},
        ],
        "temperature": 0.1,
        "max_tokens": 256,
    }
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    try:
        async with session.post(
            f"{LLM_BASE_URL}/chat/completions",
            json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=45),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                content = data["choices"][0]["message"]["content"].strip()
                log.info(f"[LLM] raw: {content!r}")
                if content.strip().lower() in ("null", "none", ""):
                    return None
                s = content.find("{")
                e = content.rfind("}") + 1
                if s >= 0 and e > s:
                    return json.loads(content[s:e])
                return None
            body = await resp.text()
            log.warning(f"[LLM] HTTP {resp.status}: {body[:200]}")
            return None
    except Exception as ex:
        log.error(f"[LLM] Error: {ex}")
        return None

# ─────────────────────────────────────────────────────────────
#  管理员 Webhook 推送
# ─────────────────────────────────────────────────────────────
async def push_admin(session: aiohttp.ClientSession, alert: dict):
    if not ADMIN_WEBHOOK_URL:
        return
    try:
        if "api.day.app" in ADMIN_WEBHOOK_URL:
            title = {"high": "高危预警", "medium": "警告", "low": "提示"}.get(alert["level"], "预警")
            body  = f"[{alert['keyword']}] {alert['text']}"
            url   = f"{ADMIN_WEBHOOK_URL.rstrip('/')}/{title}/{body}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                log.info(f"[Webhook/Bark] {r.status}")
        else:
            async with session.post(ADMIN_WEBHOOK_URL, json=alert,
                                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                log.info(f"[Webhook] {r.status}")
    except Exception as e:
        log.warning(f"[Webhook] failed: {e}")

# ─────────────────────────────────────────────────────────────
#  处理一段文本（LLM 分析 + 推送）
# ─────────────────────────────────────────────────────────────
ALERT_COOLDOWN_SEC = int(os.getenv("ALERT_COOLDOWN_SEC", "7"))

async def process_text(
    http_session: aiohttp.ClientSession,
    websocket,
    text: str,
    context_buf: deque,
    llm_sem: asyncio.Semaphore,
    session_id: str,
    db,
    alert_cooldown: list,   # [float] — cooldown_until timestamp, mutable so closure can update
    send_transcript: bool = True,
    identity: dict | None = None,
):
    if not text or len(text) < MIN_TEXT_LEN:
        return

    if send_transcript:
        try:
            await websocket.send(json.dumps({"type": "transcript", "text": text}, ensure_ascii=False))
        except Exception:
            pass

    if db:
        try:
            await db.execute(
                "INSERT INTO transcripts VALUES (?,?,?,?)",
                (str(uuid.uuid4()), session_id, text, time.time())
            )
            await db.commit()
        except Exception:
            pass

    # Skip LLM during cooldown period after an alert
    if time.time() < alert_cooldown[0]:
        log.info(f"[LLM] skipped (cooldown): {text!r}")
        return

    context = "".join(context_buf)
    if len(context) > CONTEXT_MAX_CHARS:
        context = context[-CONTEXT_MAX_CHARS:]

    async with llm_sem:
        alert_data = await analyze(http_session, context, text)

    if not alert_data:
        context_buf.append(text)
        total = sum(len(s) for s in context_buf)
        while total > CONTEXT_MAX_CHARS and context_buf:
            removed = context_buf.popleft()
            total -= len(removed)
        return

    context_buf.clear()
    alert_cooldown[0] = time.time() + ALERT_COOLDOWN_SEC

    alert = {
        "type":       "alert",
        "id":         str(uuid.uuid4()),
        "level":      alert_data.get("level", "low"),
        "keyword":    alert_data.get("keyword", ""),
        "text":       text,
        "suggestion": alert_data.get("suggestion", ""),
        "timestamp":  time.time(),
    }
    if identity:
        alert["identity"] = {
            "voiceprint_verified": identity.get("voiceprint_verified"),
            "provider":            identity.get("provider"),
            "score":               identity.get("score"),
            "speaker_id":          identity.get("speaker_id"),
        }
    log.warning(f"[ALERT] level={alert['level']} kw={alert['keyword']!r} text={text!r}")

    try:
        await websocket.send(json.dumps(alert, ensure_ascii=False))
    except Exception:
        pass
    asyncio.create_task(broadcast_admin({**alert, "session_id": session_id}))

    if db:
        try:
            await db.execute(
                "INSERT INTO alerts VALUES (?,?,?,?,?,?,?)",
                (alert["id"], session_id, alert["level"], alert["keyword"],
                 alert["text"], alert["suggestion"], alert["timestamp"])
            )
            await db.commit()
        except Exception:
            pass

    if alert["level"] == "high":
        asyncio.create_task(push_admin(http_session, alert))

# ─────────────────────────────────────────────────────────────
#  批处理模式 process_window（local/api 用）
# ─────────────────────────────────────────────────────────────
async def process_window(
    http_session: aiohttp.ClientSession,
    websocket,
    pcm: bytes,
    context_buf: deque,
    llm_sem: asyncio.Semaphore,
    session_id: str,
    db,
    alert_cooldown: list,
    identity: dict | None = None,
):
    text = await transcribe(pcm, http_session)
    await process_text(
        http_session, websocket, text, context_buf, llm_sem,
        session_id, db, alert_cooldown, identity=identity
    )

# ─────────────────────────────────────────────────────────────
#  流式连接处理（ASR_PROVIDER=xunfei）
# ─────────────────────────────────────────────────────────────
async def handle_stream(websocket, db):
    """讯飞持久流式模式：音频持续推送，边说边出字，低延迟。"""
    client          = websocket.remote_address
    session_id      = str(uuid.uuid4())
    context_buf: deque[str] = deque()
    llm_sem         = asyncio.Semaphore(LLM_CONCURRENCY)
    alert_cooldown  = [0.0]   # [cooldown_until]
    recording       = False
    xf_session      = None
    pcm_file        = None
    voice_state     = {"stage": None, "buffer": bytearray(), "speaker_id": "", "speaker_name": ""}
    voice_identity  = {}

    _ssl_ctx = ssl.create_default_context()
    _ssl_ctx.check_hostname = False
    _ssl_ctx.verify_mode = ssl.CERT_NONE
    _conn = aiohttp.TCPConnector(ssl=_ssl_ctx)
    async with aiohttp.ClientSession(connector=_conn) as http_session:

        async def on_text(sentence: str):
            try:
                await websocket.send(json.dumps({"type": "transcript", "text": sentence}, ensure_ascii=False))
            except Exception:
                pass
            asyncio.create_task(broadcast_admin({
                "type": "transcript", "session_id": session_id, "text": sentence
            }))
            # 直接 await 而非 create_task，确保 stop()/断线时 _flush_pending 触发的最终句子
            # 在 http_session 关闭前完成 LLM 分析，防止最后几句话漏报预警。
            await process_text(http_session, websocket, sentence,
                               context_buf, llm_sem, session_id, db,
                               alert_cooldown, send_transcript=False,
                               identity=voice_identity)

        async def on_interim(text: str):
            try:
                await websocket.send(json.dumps({"type": "interim", "text": text}, ensure_ascii=False))
            except Exception:
                pass
            asyncio.create_task(broadcast_admin({
                "type": "interim", "session_id": session_id, "text": text
            }))

        try:
            async for message in websocket:
                if isinstance(message, str):
                    raw = message.strip()
                    if raw.startswith("{"):
                        try:
                            cmd_obj = json.loads(raw)
                        except Exception:
                            cmd_obj = None
                        if isinstance(cmd_obj, dict):
                            handled = await handle_voiceprint_command(
                                websocket, http_session, db, voice_state,
                                voice_identity, session_id, cmd_obj
                            )
                            if handled:
                                continue
                            continue
                    cmd = raw.upper()
                    if cmd == "START":
                        if xf_session:
                            await xf_session.stop()
                        if pcm_file:
                            pcm_file.close()
                            pcm_file = None
                        # End previous session on admin before starting new one
                        if session_id in active_sessions:
                            asyncio.create_task(broadcast_admin({
                                "type": "session_end", "session_id": session_id
                            }))
                            del active_sessions[session_id]
                        recording  = True
                        session_id = str(uuid.uuid4())
                        context_buf.clear()
                        alert_cooldown[0] = 0.0
                        # Open PCM file for saving audio
                        pcm_path = None
                        if AUDIO_SAVE_DIR:
                            try:
                                os.makedirs(AUDIO_SAVE_DIR, exist_ok=True)
                                pcm_path = os.path.join(AUDIO_SAVE_DIR, f"{session_id}.pcm")
                                pcm_file = open(pcm_path, "wb")
                            except Exception as e:
                                log.warning(f"[Audio] failed to open pcm file: {e}")
                        active_sessions[session_id] = {
                            "session_id": session_id,
                            "client_ip":  str(client),
                            "started_at": time.time(),
                            "pcm_path":   pcm_path,
                        }
                        asyncio.create_task(broadcast_admin({
                            "type":       "session_start",
                            "session_id": session_id,
                            "client_ip":  str(client),
                            "started_at": active_sessions[session_id]["started_at"],
                        }))
                        xf_session = XunfeiStreamSession(on_text, on_interim=on_interim)
                        await xf_session.start()
                        await websocket.send("ACK:START")
                        log.info(f"[WS] {client} START (stream) session={session_id[:8]}")
                    elif cmd == "STOP":
                        recording = False
                        if xf_session:
                            await xf_session.stop()
                            xf_session = None
                        if pcm_file:
                            pcm_file.close()
                            pcm_file = None
                        if session_id in active_sessions:
                            asyncio.create_task(broadcast_admin({
                                "type": "session_end", "session_id": session_id
                            }))
                            del active_sessions[session_id]
                        await websocket.send("ACK:STOP")
                        log.info(f"[WS] {client} STOP (stream)")
                    continue

                if not isinstance(message, bytes):
                    continue

                if voice_state.get("stage"):
                    max_bytes = int((VOICEPRINT_MAX_SEC + 1) * SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)
                    if len(voice_state["buffer"]) < max_bytes:
                        voice_state["buffer"].extend(message)
                    continue

                if not recording:
                    continue

                if xf_session:
                    await xf_session.feed(message)
                if pcm_file:
                    pcm_file.write(message)

        except websockets.exceptions.ConnectionClosed:
            log.info(f"[WS] Disconnected: {client}")
        except Exception as e:
            log.error(f"[WS] Error: {e}\n{traceback.format_exc()}")
        finally:
            if xf_session:
                await xf_session.stop()
            if pcm_file:
                try:
                    pcm_file.close()
                except Exception:
                    pass
            if session_id in active_sessions:
                asyncio.create_task(broadcast_admin({
                    "type": "session_end", "session_id": session_id
                }))
                del active_sessions[session_id]

# ─────────────────────────────────────────────────────────────
#  批处理连接处理（local/api 模式）
# ─────────────────────────────────────────────────────────────
async def handle_batch(websocket, db):
    """每 WINDOW_SEC 秒处理一批音频。"""
    client          = websocket.remote_address
    audio_buf       = bytearray()
    recording       = False
    session_id      = str(uuid.uuid4())
    context_buf: deque[str] = deque()
    llm_sem         = asyncio.Semaphore(LLM_CONCURRENCY)
    alert_cooldown  = [0.0]
    voice_state     = {"stage": None, "buffer": bytearray(), "speaker_id": "", "speaker_name": ""}
    voice_identity  = {}

    _ssl_ctx2 = ssl.create_default_context()
    _ssl_ctx2.check_hostname = False
    _ssl_ctx2.verify_mode = ssl.CERT_NONE
    _conn2 = aiohttp.TCPConnector(ssl=_ssl_ctx2)
    async with aiohttp.ClientSession(connector=_conn2) as http_session:
        try:
            async for message in websocket:
                if isinstance(message, str):
                    raw = message.strip()
                    if raw.startswith("{"):
                        try:
                            cmd_obj = json.loads(raw)
                        except Exception:
                            cmd_obj = None
                        if isinstance(cmd_obj, dict):
                            handled = await handle_voiceprint_command(
                                websocket, http_session, db, voice_state,
                                voice_identity, session_id, cmd_obj
                            )
                            if handled:
                                continue
                            continue
                    cmd = raw.upper()
                    if cmd == "START":
                        recording  = True
                        session_id = str(uuid.uuid4())
                        audio_buf.clear()
                        context_buf.clear()
                        alert_cooldown[0] = 0.0
                        await websocket.send("ACK:START")
                        log.info(f"[WS] {client} START session={session_id[:8]}")
                    elif cmd == "STOP":
                        recording = False
                        await websocket.send("ACK:STOP")
                        log.info(f"[WS] {client} STOP")
                        if len(audio_buf) > SAMPLE_RATE * SAMPLE_WIDTH // 4:
                            asyncio.create_task(
                                process_window(http_session, websocket,
                                               bytes(audio_buf), context_buf,
                                               llm_sem, session_id, db, alert_cooldown,
                                               identity=voice_identity)
                            )
                        audio_buf.clear()
                    continue

                if not isinstance(message, bytes):
                    continue

                if voice_state.get("stage"):
                    max_bytes = int((VOICEPRINT_MAX_SEC + 1) * SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)
                    if len(voice_state["buffer"]) < max_bytes:
                        voice_state["buffer"].extend(message)
                    continue

                if not recording:
                    continue

                audio_buf.extend(message)
                if len(audio_buf) >= WINDOW_BYTES:
                    chunk = bytes(audio_buf[:WINDOW_BYTES])
                    audio_buf = bytearray(audio_buf[WINDOW_BYTES:])
                    asyncio.create_task(
                        process_window(http_session, websocket, chunk,
                                       context_buf, llm_sem, session_id, db, alert_cooldown,
                                       identity=voice_identity)
                    )

        except websockets.exceptions.ConnectionClosed:
            log.info(f"[WS] Disconnected: {client}")
        except Exception as e:
            log.error(f"[WS] Error: {e}\n{traceback.format_exc()}")

# ─────────────────────────────────────────────────────────────
#  管理员 WebSocket（/admin 路径）
# ─────────────────────────────────────────────────────────────
async def handle_admin(websocket):
    admin_connections.add(websocket)
    log.info(f"[Admin] connected: {websocket.remote_address}")
    try:
        await websocket.send(json.dumps({
            "type":     "session_list",
            "sessions": list(active_sessions.values()),
        }, ensure_ascii=False))
        async for _ in websocket:
            pass
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        admin_connections.discard(websocket)
        log.info(f"[Admin] disconnected: {websocket.remote_address}")

# ─────────────────────────────────────────────────────────────
#  WebSocket 入口路由
# ─────────────────────────────────────────────────────────────
async def handle(websocket, db):
    try:
        path = websocket.request.path
    except AttributeError:
        path = getattr(websocket, "path", "/")
    if path == "/admin":
        await handle_admin(websocket)
        return
    log.info(f"[WS] Connected: {websocket.remote_address}")
    if ASR_PROVIDER == "xunfei":
        await handle_stream(websocket, db)
    else:
        await handle_batch(websocket, db)

# ─────────────────────────────────────────────────────────────
#  HTTP 服务（录音下载 + 会话列表）
# ─────────────────────────────────────────────────────────────
async def _http_sessions(request):
    recordings = []
    if AUDIO_SAVE_DIR and os.path.isdir(AUDIO_SAVE_DIR):
        for fname in sorted(os.listdir(AUDIO_SAVE_DIR)):
            if fname.endswith(".pcm"):
                fpath = os.path.join(AUDIO_SAVE_DIR, fname)
                size = os.path.getsize(fpath)
                recordings.append({
                    "session_id":   fname[:-4],
                    "size_bytes":   size,
                    "duration_sec": round(size / (SAMPLE_RATE * SAMPLE_WIDTH), 1),
                })
    return aiohttp.web.Response(
        text=json.dumps(recordings, ensure_ascii=False),
        content_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
    )

async def _http_download(request):
    sid = request.match_info.get("session_id", "")
    if not sid or not all(c.isalnum() or c == "-" for c in sid):
        return aiohttp.web.Response(status=400, text="Invalid session ID")
    if not AUDIO_SAVE_DIR:
        return aiohttp.web.Response(status=503, text="Audio saving disabled")
    fpath = os.path.join(AUDIO_SAVE_DIR, f"{sid}.pcm")
    if not os.path.exists(fpath):
        return aiohttp.web.Response(status=404, text="Recording not found")
    with open(fpath, "rb") as f:
        pcm_data = f.read()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_data)
    return aiohttp.web.Response(
        body=buf.getvalue(),
        content_type="audio/wav",
        headers={
            "Content-Disposition": f'attachment; filename="{sid}.wav"',
            "Access-Control-Allow-Origin": "*",
        },
    )

async def start_http_server():
    app = aiohttp.web.Application()
    app.router.add_get("/sessions", _http_sessions)
    app.router.add_get("/recording/{session_id}", _http_download)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    await aiohttp.web.TCPSite(runner, "0.0.0.0", HTTP_PORT).start()
    log.info(f"[HTTP] Recording download server on port {HTTP_PORT}")

# ─────────────────────────────────────────────────────────────
#  HTTP GET /recording/{sid} on the same WS port（不依赖 8098）
# ─────────────────────────────────────────────────────────────
async def _process_request(path, request_headers):
    """Intercept plain HTTP GET requests on port 8097.
    websockets 12.x legacy API: signature is (path: str, headers: Headers).
    Return None to proceed with WS upgrade, or (HTTPStatus, headers, body) for HTTP response.
    """
    from http import HTTPStatus

    # Serve static web files — only for plain HTTP (not WebSocket upgrade)
    is_ws_upgrade = request_headers.get("Upgrade", "").lower() == "websocket"
    WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
    if not is_ws_upgrade and path in ("/", "/client.html", "/admin.html"):
        fname = "client.html" if path in ("/", "/client.html") else "admin.html"
        fpath = os.path.join(WEB_DIR, fname)
        if os.path.exists(fpath):
            with open(fpath, "rb") as f:
                body = f.read()
            return (HTTPStatus.OK, [
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Length", str(len(body))),
            ], body)

    if not path.startswith("/recording/"):
        return None  # proceed with WebSocket upgrade

    sid = path[len("/recording/"):].strip("/")
    if not sid or not all(c.isalnum() or c == "-" for c in sid):
        return (HTTPStatus.BAD_REQUEST, {}, b"Invalid session ID\n")
    if not AUDIO_SAVE_DIR:
        return (HTTPStatus.SERVICE_UNAVAILABLE, {}, b"Audio saving disabled\n")
    fpath = os.path.join(AUDIO_SAVE_DIR, f"{sid}.pcm")
    if not os.path.exists(fpath):
        return (HTTPStatus.NOT_FOUND, {}, b"Recording not found\n")

    with open(fpath, "rb") as f:
        pcm_data = f.read()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_data)
    body = buf.getvalue()
    return (HTTPStatus.OK, [
        ("Content-Type", "audio/wav"),
        ("Content-Disposition", f'attachment; filename="{sid}.wav"'),
        ("Content-Length", str(len(body))),
        ("Access-Control-Allow-Origin", "*"),
    ], body)


# ─────────────────────────────────────────────────────────────
#  启动
# ─────────────────────────────────────────────────────────────
async def main():
    log.info(f"psy-guard starting on port {PORT}")
    log.info(f"ASR provider: {ASR_PROVIDER}")
    if ASR_PROVIDER == "xunfei":
        log.info(f"ASR xunfei STREAM: appid={XUNFEI_APPID or '(not set)'}")
        log.info(f"LLM trigger: per finalized sentence (MIN_TEXT_LEN={MIN_TEXT_LEN})")
    elif ASR_PROVIDER == "api":
        log.info(f"ASR API: {ASR_API_URL}  model={ASR_MODEL}")
    else:
        log.info(f"FunASR WS: {FUNASR_WS_URL}")
    log.info(f"LLM: {LLM_BASE_URL}  model={LLM_MODEL}")
    log.info(f"Voiceprint: provider={_voiceprint_provider_name()} group={VOICEPRINT_GROUP_ID}")
    log.info(f"Admin webhook: {ADMIN_WEBHOOK_URL or '(disabled)'}")
    log.info(f"DB: {DB_PATH or '(disabled)'}")

    db = await init_db()
    await start_http_server()

    async def _handle(ws):
        await handle(ws, db)

    import ssl as _ssl_mod
    _ssl_ctx = None
    _cert, _key = "/data/cert.pem", "/data/key.pem"
    if os.path.exists(_cert) and os.path.exists(_key):
        _ssl_ctx = _ssl_mod.SSLContext(_ssl_mod.PROTOCOL_TLS_SERVER)
        _ssl_ctx.load_cert_chain(_cert, _key)
        log.info(f"[SSL] TLS enabled → wss://0.0.0.0:{PORT}")
    else:
        log.info(f"[SSL] no cert found, running plain ws://")

    async with websockets.serve(_handle, "0.0.0.0", PORT, max_size=2**20,
                                process_request=_process_request, ssl=_ssl_ctx):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
