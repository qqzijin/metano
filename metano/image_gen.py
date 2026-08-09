"""Image generation via API (DALL-E compatible endpoint)."""
import base64
import json
import time
from pathlib import Path
from typing import Optional
from metano.log import logger
IMAGE_DIR = Path.home() / '.claude' / 'metano' / 'images'

def image_generate(prompt: str, size: str='1024x1024', style: str='vivid', model: str='', n: int=1) -> dict:
    """Generate an image from a text prompt using DALL-E compatible API.

    size: 256x256, 512x512, 1024x1024
    style: vivid, natural
    model: model name (optional, uses default from config)
    """
    import requests
    import os
    api_key = os.environ.get('OPENAI_API_KEY', os.environ.get('ANTHROPIC_API_KEY', ''))
    base_url = os.environ.get('OPENAI_BASE_URL', os.environ.get('ANTHROPIC_BASE_URL', 'https://api.openai.com'))
    if base_url.endswith('/v1'):
        base_url = base_url[:-3]
    url = f'{base_url}/v1/images/generations'
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    payload = {'model': model or 'dall-e-3', 'prompt': prompt, 'n': n, 'size': size, 'style': style, 'response_format': 'b64_json'}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        results = []
        for i, img_data in enumerate(data.get('data', [])):
            b64 = img_data.get('b64_json', '')
            if b64:
                filename = f'img_{int(time.time())}_{i}.png'
                path = IMAGE_DIR / filename
                path.write_bytes(base64.b64decode(b64))
                results.append({'path': str(path), 'size': size, 'revised_prompt': img_data.get('revised_prompt', '')})
            elif img_data.get('url'):
                results.append({'url': img_data['url'], 'revised_prompt': img_data.get('revised_prompt', '')})
        return {'status': 'generated', 'prompt': prompt, 'images': results}
    except requests.exceptions.ConnectionError:
        return {'error': 'Cannot connect to image generation API. Check OPENAI_BASE_URL.'}
    except requests.exceptions.HTTPError as e:
        return {'error': f'API error: {e.response.status_code} - {e.response.text[:200]}'}
    except Exception as e:
        logger.exception()
        return {'error': str(e)}

def image_describe(image_path: str, prompt: str='Describe this image in detail.') -> dict:
    """Describe an image using vision API."""
    import requests
    import os
    api_key = os.environ.get('OPENAI_API_KEY', os.environ.get('ANTHROPIC_API_KEY', ''))
    base_url = os.environ.get('OPENAI_BASE_URL', os.environ.get('ANTHROPIC_BASE_URL', 'https://api.openai.com'))
    if base_url.endswith('/v1'):
        base_url = base_url[:-3]
    path = Path(image_path)
    if not path.exists():
        return {'error': f'Image not found: {image_path}'}
    b64 = base64.b64encode(path.read_bytes()).decode()
    ext = path.suffix.lstrip('.')
    mime = f"image/{(ext if ext in ('png', 'jpeg', 'jpg', 'gif', 'webp') else 'png')}"
    url = f'{base_url}/v1/chat/completions'
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    payload = {'model': os.environ.get('ANTHROPIC_MODEL', 'gpt-4o'), 'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': prompt}, {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{b64}'}}]}], 'max_tokens': 1000}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        description = data['choices'][0]['message']['content']
        return {'description': description, 'image_path': str(path)}
    except Exception as e:
        logger.exception()
        return {'error': str(e)}