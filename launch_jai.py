"""JAI Unified Launcher.

Provides simple 1-click startup for JAI across all interaction modes:
1. Interactive Keyboard Console (Type commands directly)
2. Voice Assistant (Microphone wake word: "JAI")
3. Telegram Remote Bridge (/laptop control from phone)
4. Screen Co-Pilot HUD (Global Hotkeys: Win+Alt+J to solve, Win+Alt+K to stop)
5. Full Autonomous Mode (Voice + Telegram /laptop Bridge + Hotkeys + Console)

Usage:
  python launch_jai.py              # Interactive menu
  python launch_jai.py --all        # Starts all services concurrently
  python launch_jai.py --voice      # Starts voice listener only
  python launch_jai.py --telegram   # Starts Telegram bridge only
  python launch_jai.py --console    # Starts interactive console only
  python launch_jai.py --hud        # Starts Screen Co-Pilot HUD only
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

# Ensure UTF-8 console output
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

BANNER = r"""
======================================================================
       _   _   ___ 
      | | /_\ |_ _|       JAI UNIFIED LAUNCHER
   _  | |/ _ \ | |  
  | |_| / _ \ \| |        Brain: Qwen 2.5 14B GPU + Gemini Flash
   \___/_/ \_\___|        Control: Voice | Keyboard | Telegram | Hotkeys
======================================================================
"""


def start_voice_listener():
    """Start headless background voice listener in this process."""
    try:
        from voice_listener import HeadlessVoiceListener
        print("[MIC] Starting Voice Listener (say 'JAI')...")
        listener = HeadlessVoiceListener()
        listener.start()
    except Exception as e:
        print(f"[ERR] Voice listener error: {e}")


def start_telegram_bridge():
    """Start Telegram polling bridge."""
    try:
        import telegram_bridge
        print("[TG] Starting Telegram Bridge (listening for /laptop commands)...")
        bridge = telegram_bridge.TelegramBridge()
        bridge.start(start_inbound_polling=True)
        return bridge
    except Exception as e:
        print(f"[ERR] Telegram bridge error: {e}")
        return None


def start_hud_listener():
    """Start Screen Co-Pilot global hotkey listener."""
    try:
        from src.screen_copilot_hud import screen_copilot_daemon
        screen_copilot_daemon.start_listener()
        print("[ORB] Sovereign Floating AI Orb & Hotkeys Active:")
        print("   * Press [Ctrl + Alt + J] or [Win + Alt + J] to solve question on screen")
        print("   * Press [Ctrl + Alt + T] or [Win + Alt + T] for Stealth Hardware Typing")
        print("   * Press [Ctrl + Shift + Space] or [Ctrl + Alt + P] for Spotlight Quick Prompt Bar")
        print("   * Press [Ctrl + Alt + V] or [Win + Alt + V] for Push-to-Talk Voice")
        print("   * Press [Ctrl + Alt + K] or Right-Click Orb to dismiss")
    except Exception as e:
        print(f"[WARN] Screen Co-Pilot HUD warning: {e}")


def start_voice_gateway():
    """Start decoupled browser audio gateway on port 8765."""
    try:
        from src.voice_gateway_server import VoiceGatewayServer
        gateway = VoiceGatewayServer(port=8765)
        gateway.start(block=False)
        print("[WEB] Browser Voice Gateway Active: http://127.0.0.1:8765/")
        return gateway
    except Exception as e:
        print(f"[WARN] Voice Gateway warning: {e}")
        return None


def run_console(initial_query: str = ""):
    """Run interactive keyboard console loop."""
    from command_jai import JaiConsoleAssistant, BANNER
    print(BANNER)
    assistant = JaiConsoleAssistant()
    if initial_query:
        print(f"\n[QUERY] \"{initial_query}\"\n")
        assistant.process(initial_query)
    else:
        assistant.show_status()

    print("Ready for your commands! (Type 'help' for examples or 'exit' to quit)\n")
    while True:
        try:
            user_input = input("JAI > ").strip()
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit", "q"):
                print("Exiting JAI console. Goodbye!")
                break
            assistant.process(user_input)
        except (KeyboardInterrupt, EOFError):
            print("\nExiting JAI console. Goodbye!")
            break


def run_full_suite():
    """Run all services concurrently: Telegram + Hotkeys + Interactive Console (with Voice option)."""
    print(BANNER)
    print("[*] Initializing Full JAI Autonomous Suite...\n")

    # 1. Start Telegram Bridge in background
    bridge = start_telegram_bridge()

    # 2. Start Screen Co-Pilot HUD hotkeys in background
    start_hud_listener()

    # 3. Start Voice in a background daemon thread
    voice_thread = threading.Thread(target=start_voice_listener, daemon=True)
    voice_thread.start()

    # 4. Start Decoupled Browser Voice Gateway
    gateway = start_voice_gateway()

    print("\n[OK] All JAI services are live and active!")
    print("----------------------------------------------------------------------")
    print("1. [VOICE]    Say 'JAI' into your mic anytime | or use [Ctrl+Alt+V]")
    print("2. [GATEWAY]  Open http://127.0.0.1:8765/ for sandboxed browser voice")
    print("3. [HOTKEYS]  [Ctrl+Alt+J] to solve | [Ctrl+Alt+T] to direct type")
    print("4. [TELEGRAM] Send '/laptop status' or '/laptop <cmd>' from your phone")
    print("5. [CONSOLE]  Starting interactive console below...\n")

    # Launch interactive console in foreground
    try:
        run_console()
    except KeyboardInterrupt:
        print("\nShutting down JAI suite...")
    finally:
        if gateway:
            gateway.stop()
        if bridge:
            bridge.stop()


def run_background_daemon():
    """Run all services persistently in background (zero terminal window, zero stdin needed)."""
    pid_file = BASE_DIR / "data" / "jai_daemon.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    try:
        from voice_listener import play_chime_success, speak_headless
        play_chime_success()
        speak_headless("JAI is active in the background.")
    except Exception:
        pass

    # 1. Start Telegram Bridge in background
    bridge = start_telegram_bridge()

    # 2. Start Screen Co-Pilot HUD hotkeys
    start_hud_listener()

    # 3. Start Decoupled Browser Voice Gateway
    gateway = start_voice_gateway()

    # 4. Start Voice Listener (keeps process alive indefinitely listening for wake word)
    try:
        from voice_listener import HeadlessVoiceListener
        listener = HeadlessVoiceListener()
        listener.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as e:
        # Fallback if mic unavailable: keep Telegram & Hotkeys alive
        try:
            while True:
                time.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            pass
    finally:
        if gateway:
            gateway.stop()
        if bridge:
            bridge.stop()
        if pid_file.exists():
            try:
                pid_file.unlink(missing_ok=True)
            except Exception:
                pass


def interactive_menu():
    print(BANNER)
    print("Choose how you want to run JAI:\n")
    print("  [1] 💻 Interactive Console      (Type commands with keyboard)")
    print("  [2] 🎤 Voice Assistant          (Microphone wake-word: 'JAI')")
    print("  [3] 📱 Telegram Remote Bridge   (Control via /laptop from phone)")
    print("  [4] ⌨️  Screen Co-Pilot HUD      (Global keys: Win+Alt+J to solve)")
    print("  [5] 🚀 Full Autonomous Suite    (All 4 services running together)")
    print("  [0] Exit\n")

    choice = input("Enter choice (1-5) [default: 5]: ").strip()
    if choice in ("1", "console"):
        run_console()
    elif choice in ("2", "voice"):
        start_voice_listener()
    elif choice in ("3", "telegram"):
        bridge = start_telegram_bridge()
        print("Telegram Bridge is running. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            if bridge:
                bridge.stop()
    elif choice in ("4", "hud", "hotkeys"):
        start_hud_listener()
        print("Hotkeys active. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    elif choice in ("5", "", "all"):
        run_full_suite()
    elif choice in ("0", "exit", "quit", "q"):
        print("Goodbye!")
    else:
        # User typed a question or command directly!
        run_console(initial_query=choice)


def main():
    parser = argparse.ArgumentParser(description="JAI Unified Launcher")
    parser.add_argument("--all", action="store_true", help="Launch full autonomous suite")
    parser.add_argument("--daemon", "--background", action="store_true", help="Launch full suite persistently in background (no console/stdin)")
    parser.add_argument("--console", action="store_true", help="Launch interactive keyboard console")
    parser.add_argument("--voice", action="store_true", help="Launch voice assistant listener")
    parser.add_argument("--telegram", action="store_true", help="Launch Telegram remote bridge")
    parser.add_argument("--hud", action="store_true", help="Launch Screen Co-Pilot HUD hotkeys")
    args = parser.parse_args()

    if args.daemon:
        run_background_daemon()
    elif args.all:
        run_full_suite()
    elif args.console:
        run_console()
    elif args.voice:
        start_voice_listener()
    elif args.telegram:
        bridge = start_telegram_bridge()
        print("Telegram Bridge is running. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            if bridge:
                bridge.stop()
    elif args.hud:
        start_hud_listener()
        print("Hotkeys active. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        interactive_menu()


if __name__ == "__main__":
    main()
