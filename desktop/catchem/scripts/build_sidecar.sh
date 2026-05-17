#!/usr/bin/env bash
# Package the fusion_stack FastAPI server as a single PyInstaller binary
# (--onedir) suitable for shipping inside Catchem.app/Contents/Resources/sidecar/.
#
# Output: desktop/catchem/sidecar-out/fusion-stack-sidecar (executable)
set -euo pipefail
CATCHEM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$CATCHEM_DIR/../.." && pwd)"
OUT="$CATCHEM_DIR/sidecar-out"

cd "$REPO_ROOT"
[ -d .venv ] || { echo "ERROR: $REPO_ROOT/.venv missing"; exit 1; }
# shellcheck disable=SC1091
source .venv/bin/activate

# Install PyInstaller into the dev venv (idempotent).
uv pip install pyinstaller --quiet || pip install --quiet pyinstaller

# Entry shim: a small Python file that imports fusion_stack and runs the server.
cat > "$CATCHEM_DIR/_sidecar_entry.py" <<'PY'
import sys
from fusion_stack.cli import app as _typer_app
def main():
    sys.argv = ["fusion-stack", "serve"]
    _typer_app()
if __name__ == "__main__":
    main()
PY

rm -rf "$OUT"
mkdir -p "$OUT"

pyinstaller \
  --noconfirm \
  --clean \
  --onedir \
  --name fusion-stack-sidecar \
  --distpath "$OUT" \
  --workpath "$CATCHEM_DIR/_sidecar_build" \
  --specpath "$CATCHEM_DIR/_sidecar_build" \
  --collect-all fusion_stack \
  --collect-all sse_starlette \
  --collect-all uvicorn \
  --collect-all fastapi \
  --collect-all multipart \
  --collect-all rapidfuzz \
  --hidden-import fusion_stack \
  --hidden-import fusion_stack.cli \
  "$CATCHEM_DIR/_sidecar_entry.py"

echo
echo "Sidecar built at: $OUT/fusion-stack-sidecar/"
echo "Quick smoke:"
echo "  $OUT/fusion-stack-sidecar/fusion-stack-sidecar &"
echo "  curl -s http://127.0.0.1:8087/healthz"
