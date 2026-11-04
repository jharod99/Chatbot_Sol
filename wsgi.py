import os
import sys

# Agregar carpeta estructurador-erp al path de ejecución
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "estructurador-erp"))

from app import app  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    host = os.environ.get("HOST", "0.0.0.0")
    app.run(host=host, port=port)
