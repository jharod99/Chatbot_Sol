#!/usr/bin/env bash
set -e

# Ir al directorio del script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=========================================================="
echo "  Iniciando Estructurador Inteligente ERP Oracle Fusion   "
echo "=========================================================="

# 1. Verificar o crear entorno virtual
if [ ! -d "venv" ]; then
    echo "📦 Creando entorno virtual Python (venv)..."
    python3 -m venv venv
fi

# 2. Activar entorno virtual
echo "⚡ Activando entorno virtual..."
source venv/bin/activate

# 3. Comprobar e instalar dependencias si faltan
if ! python -c "import flask, google.genai, pydantic, dotenv" 2>/dev/null; then
    echo "📥 Instalando dependencias de requirements.txt..."
    pip install -r requirements.txt
else
    echo "✅ Dependencias verificadas."
fi

# 4. Verificar archivo .env
if [ ! -f ".env" ]; then
    echo "⚠️ Advertencia: No se encontró el archivo .env."
fi

PORT="${PORT:-5000}"

# 5. Abrir navegador automáticamente en segundo plano cuando el servidor esté listo
(
    sleep 2
    URL="http://127.0.0.1:${PORT}"
    echo "🌐 Abriendo navegador en $URL ..."
    if command -v xdg-open > /dev/null; then
        xdg-open "$URL" > /dev/null 2>&1
    elif command -v gio > /dev/null; then
        gio open "$URL" > /dev/null 2>&1
    else
        python3 -m webbrowser "$URL" > /dev/null 2>&1
    fi
) &

# 6. Ejecutar servidor Flask
echo "🚀 Servidor Flask iniciado en http://127.0.0.1:${PORT}"
echo "   Presiona CTRL+C para detener."
python app.py
