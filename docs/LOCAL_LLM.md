# Running Local LLMs for Race Engineer on Windows

This guide explains how to set up local, offline language models to run alongside the race engineer on Windows 11.

---

### Hardware Advisory & Performance Expectations

> [!WARNING]
> **RTX 2050 (4 GB VRAM) Warning**:
> F1 25 consumes nearly all of the 4 GB VRAM on mobile RTX 2050 laptops. Offloading even a 3B LLM to GPU will cause frame drops, audio stutter, and telemetry lag in the game.
>
> **All local models must run on CPU only (`num_gpu: 0`).**
>
> On a modern laptop CPU, a quantized 3B model (`qwen2.5:3b-instruct` or `llama3.2:3b`) produces a 20–30 word radio response in roughly **1 to 3 seconds**.
> Therefore, **Groq (`llama-3.1-8b-instant`) is recommended as the fast primary engine** (~200–300 ms response time), while **Ollama on CPU serves as the free, offline fallback**.

---

### 1. Setting up Ollama (Recommended Local Engine)

#### Step 1: Install Ollama on Windows
1. Download the Windows installer from [ollama.com](https://ollama.com/download/windows).
2. Run the installer and launch Ollama from the Windows Start menu or system tray.

#### Step 2: Pull the Recommended Model
Open PowerShell or Windows Terminal and pull the recommended 3B instruct model:
```powershell
ollama pull qwen2.5:3b-instruct
```

For weaker laptop CPUs where even faster generation is desired, you can pull smaller variants:
```powershell
# Faster alternative on weaker CPUs:
ollama pull llama3.2:3b
# Ultra-fast lightweight alternative:
ollama pull qwen2.5:1.5b-instruct
```

#### Step 3: Configure `config.yaml`
Ensure `config.yaml` is set to run Ollama on CPU:
```yaml
llm:
  primary: groq               # Fast cloud primary
  fallback: ollama            # Local offline fallback
  timeout_s: 4.0
  max_tokens: 60
  ollama:
    base_url: "http://localhost:11434"
    model: "qwen2.5:3b-instruct"
    num_gpu: 0                # Enforces CPU-only execution
```
The app sends `"keep_alive": "60m"` using Ollama's native `/api/chat` endpoint so the model stays resident in RAM between driver transmissions.

---

### 2. Using `llama.cpp` (`llama-server`) or LM Studio

If you prefer `llama.cpp` or LM Studio:

#### Option A: `llama.cpp`
Start `llama-server` on CPU:
```powershell
llama-server -m models/qwen2.5-3b-instruct-q4_k_m.gguf --port 8080 -ngl 0 -c 2048
```
(`-ngl 0` disables GPU layer offloading).

#### Option B: LM Studio
1. In LM Studio, download `Qwen2.5-3B-Instruct` (e.g. Q4_K_M).
2. Set **GPU Acceleration** to **Off** (0 layers offloaded).
3. Start the Local Server on port `8080`.

#### Point Race Engineer to the Local Server
In `config.yaml`:
```yaml
llm:
  primary: openai_compatible
  fallback: null
  openai_compatible:
    base_url: "http://localhost:8080/v1"
    model: "local"
```
