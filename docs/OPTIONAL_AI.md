# Optional: local AI (hooks, titles, voiceover)

Everything here runs **on your computer**: no API keys, nothing uploaded. Each part is
optional. Without it, Clipper uses template hooks and no voiceover, and clips still render.

All paths below are inside your storage folder: `~/Clipper/storage` (Windows:
`%USERPROFILE%\Clipper\storage`). Change into it first:

```bash
cd ~/Clipper/storage            # Windows PowerShell: cd $env:USERPROFILE\Clipper\storage
```

## 1. AI story: hook, title, description (Ollama)

1. Install Ollama: <https://ollama.com/download> (macOS, Windows, Linux).
2. Pull the models (about 5 GB each; a GPU or Apple Silicon is recommended):

   ```bash
   ollama pull qwen3:8b        # writes the hook / title / spoken intro
   ollama pull qwen3-vl:8b     # looks at frames so the story matches what's on screen
   ```
3. Keep Ollama running. Clipper finds it at `http://127.0.0.1:11434`. Settings in `.env`:

   ```ini
   STORY_LLM=true
   OLLAMA_MODEL=qwen3:8b
   OLLAMA_VISION_MODEL=qwen3-vl:8b   # empty = transcript only
   ```

Smaller machines: `OLLAMA_MODEL=qwen3:4b` and `OLLAMA_VISION_MODEL=` (empty).

## 2. Voiceover: Kokoro (fast, natural narrator)

Kokoro runs in its **own virtual environment**, because its pronunciation library (espeak-ng)
is GPL and is never loaded into Clipper itself.

macOS / Linux:

```bash
uv venv --python 3.12 tools/tts-venv
uv pip install --python tools/tts-venv/bin/python kokoro-onnx==0.6.1 soundfile
mkdir -p models/kokoro && cd models/kokoro
curl -LO https://huggingface.co/fastrtc/kokoro-onnx/resolve/main/kokoro-v1.0.onnx
curl -LO https://huggingface.co/fastrtc/kokoro-onnx/resolve/main/voices-v1.0.bin
```

Windows (PowerShell):

```powershell
uv venv --python 3.12 tools\tts-venv
uv pip install --python tools\tts-venv\Scripts\python.exe kokoro-onnx==0.6.1 soundfile
New-Item -ItemType Directory -Force models\kokoro | Out-Null; cd models\kokoro
curl.exe -LO https://huggingface.co/fastrtc/kokoro-onnx/resolve/main/kokoro-v1.0.onnx
curl.exe -LO https://huggingface.co/fastrtc/kokoro-onnx/resolve/main/voices-v1.0.bin
```

Pick the voice per stream in the dashboard (*Clip style → Story & voice*: male/female,
American/British).

## 3. Expressive voice: Chatterbox Turbo (laughs, gasps; slower, ~5 GB)

```bash
# macOS / Linux  (Windows: tools\emotion-venv\Scripts\python.exe)
uv venv --python 3.12 tools/emotion-venv
uv pip install --python tools/emotion-venv/bin/python \
  -r "$(python3 -c 'import clipper, os; print(os.path.join(os.path.dirname(clipper.__file__), "tools", "emotion-requirements.lock.txt"))' 2>/dev/null || echo clipper/tools/emotion-requirements.lock.txt)" \
  --require-hashes --index https://download.pytorch.org/whl/cpu --index-strategy unsafe-best-match
tools/emotion-venv/bin/python -c "from huggingface_hub import snapshot_download as d; d('ResembleAI/chatterbox-turbo')"
```

From a source checkout the lock file is at `clipper/tools/emotion-requirements.lock.txt`.
It pins every package to its official hash.

## 4. Action detection in titles (racing, police chase, fight…)

```bash
uv tool install --force --python 3.12 "clipper[actions] @ git+https://github.com/k-a-l/clipper"
```

About 1 GB (PyTorch + OpenCLIP). It uses the GPU (CUDA or Apple MPS) when one is available.
