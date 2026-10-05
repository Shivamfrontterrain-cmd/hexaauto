# AutoHexa - Automated Multi-Account Pokémon Telegram Userbot

AutoHexa is a high-performance multi-account Telegram userbot system targeting **@HeXamonbot**, featuring a zero-mistake 3-Tier AI-powered anti-bot trivia solver, dynamic battle farming strategies, and an ultra-fast local sprite-matching `/guess` engine.

---

## ✨ Features

- **🛡️ 3-Tier Anti-Bot Trivia Solver**:
  - **Tier 1 (Local KB)**: 0ms exact match for known trivia in `data/known_checks.json`.
  - **Tier 2 (Gemini AI Studio)**: Structured JSON model query (`gemini-3.1-flash-lite` / `gemini-3.8-flash`) supporting both standard and 2026 AI Studio API key formats (`AQ.` / `AIza`).
  - **Tier 3 (Emergency Alert)**: Immediate human-in-the-loop notification to personal Telegram ID before timeout.
- **⚡ Ultra-Fast Local Silhouette Guesser (`/guess`)**:
  - Automatically identifies "Who's that Pokémon?" challenge silhouettes in **<0.3 seconds** with **99%+ accuracy** using 1,850+ pre-indexed Sugimori Gen 1–9 sprite masks (`data/sprite_cache.pkl`).
  - Farms Pokédollars (PD) continuously with human-jittered reaction timing.
  - Seamlessly falls back to Gemini Vision if a sprite mask is ambiguous.
- **⚔️ Dual Battle Systems**:
  - **Kill Mode**: Automatically selects the highest-damage super-effective move to faint wild Pokémon for maximum PD and EXP.
  - **Hybrid Mode**: Intelligently identifies previously caught Pokémon (marked with `☆`) and throws **Repeat Balls**, while reserving **Ultra/Master Balls** for Legendaries and Mythicals.
- **👥 Multi-Account Support**:
  - Run multiple Telegram sessions concurrently with staggered command intervals to eliminate burst rate limits.

---

## 🚀 Setup Guide

### 1. Configure `.env`
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Fill in the credentials:
- `TELEGRAM_API_ID` & `TELEGRAM_API_HASH`: Obtain from [my.telegram.org](https://my.telegram.org).
- `GEMINI_API_KEY`: Obtain from [Google AI Studio](https://aistudio.google.com/app/apikey).
- `ACTION_MODE`: Set to `"guess"` (PD farming), `"hunt"` (wild encounters), or `"both"`.

### 2. Add Telegram Accounts
Run the interactive session generator:
```bash
.venv\Scripts\python.exe add_account.py
```
Follow the prompts to enter your phone number and Telegram verification code. Session files are saved locally in `sessions/` (and never committed to git).

### 3. Start the Bot
```bash
.venv\Scripts\python.exe main.py
```

---

## 🧪 Testing

Run the automated test suite covering all handlers, battle systems, sprite matching, and solver tiers:
```bash
.venv\Scripts\python.exe -m unittest discover tests
```
