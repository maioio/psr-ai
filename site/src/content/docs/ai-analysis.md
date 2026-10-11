---
title: AI analysis
description: Describe screenshots with a vision model, drop sensitive steps, publish a session to your docs, and analyze a video.
---

These commands work on the output folder `sa stop` writes. A typical order is:

```bash
sa stop
sa review psr_output/psr_20240115_143000_output
sa analyze psr_output/psr_20240115_143000_output
sa pub psr_output/psr_20240115_143000_output
```

Review first, so screenshots you drop are never sent to a model or published.

## Review and drop steps

```bash
sa review SOURCE_DIR
```

For each step, `sa review` shows the timestamp, the program and the action, then asks `Keep this step or drop due to sensitive data? [K/d]`. Enter or `K` keeps the step; `D` drops it. A dropped step is removed from `data.json` and its screenshot is deleted from disk straight away.

When you finish, `report.md` is rebuilt from what is left, and so is `action_replay.gif` if Pillow is installed. It prints how many steps were kept and dropped.

:::caution[When the report is not rebuilt]
With `--no-regen`, or when you drop every step, `report.md` and `action_replay.gif` are left as they were and still show the dropped steps. Delete them, or run `sa review` again without `--no-regen`.
:::

## Describe screenshots

```bash
sa analyze SOURCE_DIR
```

`sa analyze` sends each screenshot to a vision model and stores the reply as `ai_description` on the step in `data.json`. Steps without a screenshot are skipped. Afterwards it rebuilds `report.md`, which shows the description in each step's table; `--no-regen` skips that.

| Option | Default | Meaning |
|---|---|---|
| `-p`, `--provider` | `anthropic` | `anthropic` or `openai`. |
| `-m`, `--model` | `claude-sonnet-4-6` for Anthropic, `gpt-4o` for OpenAI | Use another model. |
| `--no-regen` | off | Do not rebuild `report.md`. |

It needs the `ai` extra and an API key in the environment: `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`.

```bash
pip install -e ".[ai]"
```

:::caution[Screenshots leave your machine]
Every screenshot in the folder is uploaded to the provider you choose. Run `sa review` first if any of them show something that should stay private.
:::

## Publish a session

### sa pub

```bash
sa pub SOURCE_DIR
```

`sa pub` copies `report.md`, `data.json`, `action_replay.gif` and `screenshots/` into `docs/sessions/<folder name>/` under the current directory. `-d`, `--dest` chooses another root. Without other options it only copies.

| Option | What it does |
|---|---|
| `--git` | After copying, run `git add` on that folder, commit and push. The current directory must be inside a Git repository. |
| `-m`, `--message` | Commit message. Default `docs: add PSR session <name> [<date and time>]`. |
| `--remote` | Remote to push to. Default `origin`. |
| `--branch` | Branch to push to. Default the current branch. |
| `--github-api` | Upload `report.md` through the GitHub API instead of local Git. |

`--github-api` needs `GITHUB_TOKEN`, a token that can write to the repository, and `GITHUB_REPO` in the form `owner/repo`. It writes `docs/sessions/<folder name>/report.md` on the branch in `GITHUB_BRANCH`, or `main` if that is not set. Only `report.md` is uploaded: the screenshots are not, so the report's image links will not resolve there.

### sa pipeline

```bash
sa pipeline ZIP_PATH
```

`sa pipeline` takes the recording ZIP and runs four stages in one go:

1. **Parse.** Write `report.md`, `data.json`, the screenshots and the GIF into `<zip name>_output` next to the ZIP. `--no-gif` skips the GIF. Unlike `sa stop`, this stage does not add browser events, element HTML or the OS context.
2. **Analyze.** Only with `--analyze`. `-p` and `-m` work as in `sa analyze`.
3. **Publish.** Copy the folder into `docs/sessions/` under the current directory, then commit and push.
4. **Pull request.** Only with `--pr` and `--branch`.

| Option | What it does |
|---|---|
| `-b`, `--branch` | Create this branch, commit to it and push it. Without it, the commit goes to the current branch. |
| `--no-create-branch` | Use an existing branch instead of creating one. |
| `-M`, `--message` | Commit message. |
| `--remote` | Remote to push to. Default `origin`. |
| `--pr` | Open a pull request through the GitHub API. Needs `--branch`, `GITHUB_TOKEN` and `GITHUB_REPO`. |
| `--pr-base` | Branch the pull request targets. Default `main`; set it if your default branch has another name. |

:::caution[sa pipeline pushes without asking]
When the current directory is inside a Git repository, `sa pipeline` always commits and pushes. There is no dry run. Run `sa review` on the output first, or use `sa stop` and `sa pub` if you want to look before anything is pushed.
:::

## Analyze a video

```bash
sa video-analyze SOURCE
sa video-analyze lecture.mp4 -q "Which settings does the presenter change?"
```

`SOURCE` is a local video file or a URL that `yt-dlp` can download. `sa video-analyze` samples frames, gets a transcript, and sends both to Claude with your question. It answers in a Markdown file.

It needs `ffmpeg` on your `PATH`, the `anthropic` package and `ANTHROPIC_API_KEY`. URLs also need `yt-dlp`. The `analyze` extra adds `yt-dlp` and a local transcription model; the `ai` extra adds `anthropic`:

```bash
pip install -e ".[analyze,ai]"
```

| Option | Default | Meaning |
|---|---|---|
| `-q`, `--question` | A summary: TL;DR, key points, quotes, takeaways | What to ask about the video. |
| `-o`, `--output` | `~/psr_output/video_analysis/` | Where to write the results. |
| `--max-frames` | 100 | Frames to extract. The frame rate is scaled to fit. |
| `--start`, `--end` | whole video | Focus on a range, in seconds, with denser sampling. |
| `--resolution` | 512 | Frame width in pixels. Higher shows more detail and costs more tokens. |

How it works:

1. **Download.** URLs are downloaded with `yt-dlp` at up to 720p, with English captions when the site has them.
2. **Frames.** `ffmpeg` extracts frames into `frames/` in the output folder.
3. **Transcript.** The first source that works is used: the downloaded captions; then the Groq transcription API if `GROQ_API_KEY` is set; then OpenAI's `whisper-1` if `OPENAI_API_KEY` is set; then local `faster-whisper`; then the older local `whisper` package. The text is saved as `transcript.txt`.
4. **Analysis.** Up to 50 of the frames and the transcript go to Claude (`claude-sonnet-4-6`). The answer is saved as `analysis.md`.

If `GROQ_API_KEY` or `OPENAI_API_KEY` is not in the environment, it is also read from `~/.config/watch/.env`.

:::caution[Video content leaves your machine]
Frames are uploaded to Anthropic. When a Groq or OpenAI key is available, the audio is uploaded to that service for transcription. Without those keys, transcription runs locally. Do not analyze a video you are not allowed to share with these services.
:::
