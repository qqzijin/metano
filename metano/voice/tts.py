"""Text-to-Speech using edge-tts (free Microsoft Edge neural voices)."""

import asyncio
import subprocess
import tempfile
from pathlib import Path

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
        voice_cache = Path.home() / ".claude" / "metano" / "voice_cache"
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