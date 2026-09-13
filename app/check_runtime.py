import sys

if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11 or newer is required")
import faster_whisper
import httpx
import opencc
import huggingface_hub.constants as constants

if not hasattr(constants, "HF_HUB_DISABLE_SYMLINKS"):
    raise SystemExit("Update huggingface-hub for Windows cache support")
print("Local runtime is ready")
