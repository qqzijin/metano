"""Text-to-Speech using edge-tts (free Microsoft Edge neural voices).

Single voice module (core.py merged in): exposes both the web-facing
``voice_speak``/``voice_list_voices`` and the MCP-facing ``speak``/``synthesize``.
"""

import asyncio
import json
import subprocess
import tempfile
import time
from pathlib import Path
from ..paths import VOICE_CACHE_DIR, AUDIO_DIR
from metano.log import logger

# Chinese voices
ZH_VOICES = {
    "xiaoxiao": "zh-CN-XiaoxiaoNeural",
    "yunxi": "zh-CN-YunxiNeural",
    "yunyang": "zh-CN-YunyangNeural",
    "xiaoyi": "zh-CN-XiaoyiNeural",
    "yunjian": "zh-CN-YunjianNeural",
}

# English voices
EN_VOICES = {
    "aria": "en-US-AriaNeural",
    "guy": "en-US-GuyNeural",
    "jenny": "en-US-JennyNeural",
    "roger": "en-US-RogerNeural",
}

ALL_VOICES = {**ZH_VOICES, **EN_VOICES}


async def synthesize(text: str, voice: str = "xiaoxiao", output_path: str | None = None) -> str:
    """Synthesize speech from text using edge-tts.

    Args:
        text: Text to speak (max ~5000 chars)
        voice: Voice name (xiaoxiao, yunxi, yunyang, xiaoyi, yunjian, aria, guy, jenny, roger)
        output_path: Output file path (default: temp file)

    Returns:
        Path to the generated MP3 file
    """
    try:
        import edge_tts
    except ImportError:
        raise ImportError("edge-tts not installed. Run: pip install edge-tts")

    voice_id = ALL_VOICES.get(voice, voice)
    if output_path is None:
        voice_cache = VOICE_CACHE_DIR
        voice_cache.mkdir(parents=True, exist_ok=True)
        output_path = str(voice_cache / f"tts_{hash(text[:100])}.mp3")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # Truncate text for TTS limits
    text = text[:5000]

    communicate = edge_tts.Communicate(text, voice_id)
    await communicate.save(output_path)
    return output_path


def speak(text: str, voice: str = "xiaoxiao") -> str:
    """Synthesize and play speech.

    Returns path to the audio file.
    """
    output_path = asyncio.run(synthesize(text, voice))

    # Play audio
    try:
        subprocess.Popen(
            ["mpv", "--no-video", "--really-quiet", output_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        try:
            subprocess.Popen(
                ["aplay", "-q", output_path],
              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass  # No player available, file still saved

    return output_path


def list_voices() -> dict[str, str]:
    """List all available voices."""
    return ALL_VOICES


def voice_speak(text: str, voice: str='zh-CN-YunxiNeural', rate: str='+0%', pitch: str='+0Hz', output_format: str='mp3') -> dict:
    """Convert text to speech using edge-tts (web-facing, persisted to AUDIO_DIR)."""
    try:
        import edge_tts
    except ImportError:
        return {'error': 'edge-tts not installed. Run: pip install edge-tts'}
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    filename = f'tts_{int(time.time())}.{output_format}'
    output_path = AUDIO_DIR / filename
    try:
        async def _speak():
            communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
            await communicate.save(str(output_path))
        try:
            asyncio.get_running_loop()
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(lambda: asyncio.run(_speak()))
                future.result(timeout=60)
        except RuntimeError:
            asyncio.run(_speak())
        return {'status': 'generated', 'path': str(output_path), 'voice': voice, 'text_length': len(text), 'size': output_path.stat().st_size}
    except Exception as e:
        logger.exception()
        return {'error': str(e)}


def voice_list_voices(language: str='') -> dict:
    """List available TTS voices, optionally filtered by language."""
    try:
        import edge_tts
    except ImportError:
        return {'error': 'edge-tts not installed'}
    try:
        async def _list():
            voices = await edge_tts.list_voices()
            if language:
                voices = [v for v in voices if v['Locale'].startswith(language)]
            return voices
        try:
            asyncio.get_running_loop()
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                voices = pool.submit(lambda: asyncio.run(_list())).result(timeout=60)
        except RuntimeError:
            voices = asyncio.run(_list())
        return {'count': len(voices), 'voices': [{'name': v['ShortName'], 'gender': v['Gender'], 'locale': v['Locale']} for v in voices[:50]]}
    except Exception as e:
        logger.exception()
        return {'error': str(e)}