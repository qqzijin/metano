---
name: youtube-content
description: "YouTube transcripts to summaries, threads, blogs."
platforms: [linux, macos, windows]
---

# YouTube Content Tool

## When to use

Use when the user shares a YouTube URL or video link, asks to summarize a video, requests a transcript, or wants to extract and reformat content from any YouTube video. Transforms transcripts into structured content (chapters, summaries, threads, blog posts).

Extract transcripts from YouTube videos and convert them into useful formats.

## Setup

```bash
pip install youtube-transcript-api
```

## Fetching the Transcript

metano 不内置 `scripts/fetch_transcript.py`；用 `code_run(language="python")` 内联调用
`youtube-transcript-api`（兼容任何标准 YouTube URL、youtu.be 短链、shorts、embed、直播链接或裸 11 位 video ID）：

```python
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound

video_id = "VIDEO_ID"          # 或从 URL 解析出 11 位 ID
try:
    transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=["tr", "en"])
    for seg in transcript:
        t = int(seg["start"])
        print(f"{t//60:02d}:{t%60:02d} {seg['text']}")
except TranscriptsDisabled:
    print("TRANSCRIPT_DISABLED")
except NoTranscriptFound:
    print("NO_LANGUAGE_MATCH")
```

`transcript` 每项形如 `{"text": "...", "start": 0.0, "duration": 5.0}`；
需要纯文本时只输出 `seg["text"]`，需要时间戳时格式化 `seg["start"]`。

## Output Formats

After fetching the transcript, format it based on what the user asks for:

- **Chapters**: Group by topic shifts, output timestamped chapter list
- **Summary**: Concise 5-10 sentence overview of the entire video
- **Chapter summaries**: Chapters with a short paragraph summary for each
- **Thread**: Twitter/X thread format — numbered posts, each under 280 chars
- **Blog post**: Full article with title, sections, and key takeaways
- **Quotes**: Notable quotes with timestamps

### Example — Chapters Output

```
00:00 Introduction — host opens with the problem statement
03:45 Background — prior work and why existing solutions fall short
12:20 Core method — walkthrough of the proposed approach
24:10 Results — benchmark comparisons and key takeaways
31:55 Q&A — audience questions on scalability and next steps
```

## Workflow

1. **Fetch** the transcript with the inline `youtube-transcript-api` snippet above (text + timestamps).
2. **Validate**: confirm the output is non-empty and in the expected language. If empty, retry without `--language` to get any available transcript. If still empty, tell the user the video likely has transcripts disabled.
3. **Chunk if needed**: if the transcript exceeds ~50K characters, split into overlapping chunks (~40K with 2K overlap) and summarize each chunk before merging.
4. **Transform** into the requested output format. If the user did not specify a format, default to a summary.
5. **Verify**: re-read the transformed output to check for coherence, correct timestamps, and completeness before presenting.

## Error Handling

- **Transcript disabled**: tell the user; suggest they check if subtitles are available on the video page.
- **Private/unavailable video**: relay the error and ask the user to verify the URL.
- **No matching language**: retry without `--language` to fetch any available transcript, then note the actual language to the user.
- **Dependency missing**: run `pip install youtube-transcript-api` and retry.
