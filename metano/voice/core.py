"""Voice/audio: TTS via edge-tts."""
import asyncio
import json
import time
from typing import Optional
from metano.log import logger
from ..paths import AUDIO_DIR

def voice_speak(text: str, voice: str='zh-CN-YunxiNeural', rate: str='+0%', pitch: str='+0Hz', output_format: str='mp3') -> dict:
    """Convert text to speech using edge-tts."""
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