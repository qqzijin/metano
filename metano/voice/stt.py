"""Speech-to-Text using faster-whisper (local, offline)."""
import tempfile
import subprocess
import time
from pathlib import Path
from metano.log import logger
DEFAULT_MODEL = 'medium'
RECORDING_DIR = Path.home() / '.claude' / 'metano' / 'voice_cache'

def record_audio(duration: int=5, output_path: str | None=None) -> str:
    """Record audio from microphone using arecord.

    Args:
        duration: Recording duration in seconds
        output_path: Output WAV file path

    Returns:
        Path to the recorded WAV file
    """
    if output_path is None:
        RECORDING_DIR.mkdir(parents=True, exist_ok=True)
        output_path = str(RECORDING_DIR / f'recording_{int(time.time())}.wav')
    subprocess.run(['arecord', '-f', 'S16_LE', '-r', '16000', '-c', '1', '-d', str(duration), output_path], check=True, capture_output=True)
    return output_path

def transcribe(audio_path: str, model: str=DEFAULT_MODEL, language: str='zh') -> dict:
    """Transcribe audio file to text using faster-whisper.

    Args:
        audio_path: Path to audio file (WAV, MP3, etc.)
        model: Whisper model size (tiny, base, small, medium, large-v3)
        language: Language code (zh, en, ja, etc.)

    Returns:
        dict with 'text', 'language', 'segments'
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return {'error': 'faster-whisper not installed. Run: pip install faster-whisper', 'text': ''}
    try:
        wm = WhisperModel(model, device='cpu', compute_type='int8')
    except Exception as e:
        logger.exception()
        return {'error': f'Failed to load whisper model: {e}', 'text': ''}
    try:
        segments, info = wm.transcribe(audio_path, language=language, beam_size=5, vad_filter=True)
        text_parts = []
        segment_list = []
        for seg in segments:
            text_parts.append(seg.text)
            segment_list.append({'start': seg.start, 'end': seg.end, 'text': seg.text})
        return {'text': ''.join(text_parts).strip(), 'language': info.language, 'language_probability': info.language_probability, 'segments': segment_list}
    except Exception as e:
        logger.exception()
        return {'error': f'Transcription failed: {e}', 'text': ''}

def listen(duration: int=5, model: str=DEFAULT_MODEL, language: str='zh') -> dict:
    """Record audio and transcribe it in one step.

    Args:
        duration: Recording duration in seconds
        model: Whisper model size
        language: Language code

    Returns:
        dict with 'text', 'audio_path', 'language'
    """
    audio_path = record_audio(duration=duration)
    result = transcribe(audio_path, model=model, language=language)
    result['audio_path'] = audio_path
    return result