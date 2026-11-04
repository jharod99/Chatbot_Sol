# Estructurador Inteligente de Especificaciones ERP (Oracle Fusion Cloud)

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Flask](https://img.shields.io/badge/framework-Flask%203.1-black.svg)](https://flask.palletsprojects.com/)
[![Google Gemini](https://img.shields.io/badge/AI-Google%20Gemini%20Flash-orange.svg)](https://aistudio.google.com/)
[![Standard](https://img.shields.io/badge/ERP-Oracle%20Fusion%20Cloud-red.svg)](https://www.oracle.com/erp/)
[![Standard ISO 8000](https://img.shields.io/badge/compliance-ISO%208000%20%2F%20NCS-green.svg)](https://www.iso.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> Motor inteligente y determinista para normalizar, gobernar y catalogar requerimientos de compras industriales en **Oracle Fusion Cloud ERP**, eliminando duplicados, bloqueando compras físicamente incompatibles y protegiendo el catálogo maestro.

---

## 🎯 El Problema de Negocio

En empresas industriales, mineras y de manufactura, las solicitudes de compras se ingresan en lenguaje coloquial (ej. *"necesito urgente 4 válvulas de bola inox de 2 pulgadas"* o *"tubería de PVC para vapor a 220°C"*).

Los asistentes de IA genéricos sin gobierno cometen tres errores costosos:
1. **Alucinan o inventan datos técnicos** y violan el límite de 240 caracteres de Oracle Fusion.
2. **Incrustan cantidades dentro del texto**, arruinando la Unidad de Medida (UOM) contable.
3. **Aprueban solicitudes físicamente incompatibles** (*artículos Frankenstein*), provocando paradas de planta y compras erradas de miles de dólares.

Este proyecto resuelve este desafío mediante una **arquitectura híbrida**: validaciones deterministas estrictas + cruce de catálogo maestro + **Google Gemini** bajo un pipeline de 6 compuertas de seguridad con salida fuertemente tipada en Pydantic.

---

## 🚀 Módulos Críticos de Lógica Determinista

```
┌────────────────────────────────────────────────────────────────────────┐
│             PIPELINE EN VIVO DE 6 COMPUERTAS DE SEGURIDAD              │
└────────────────────────────────────────────────────────────────────────┘
  1. 🧹 Ingesta y Purga       -> Elimina plazos ('urgente'), destinos y solicitantes.
  2. 📋 Alcance (Combos)      -> Separa bienes (Capex) de servicios (Opex) en cola.
  3. 🔍 Catálogo Maestro      -> Cruce con histórico: atajos en 1 clic y deltas.
  4. 🏷️ Desacople de UOM      -> Expulsa la cantidad (ej. '4') y fija UOM contable.
  5. 🛡️ Anti-Frankenstein     -> Bloquea incompatibilidades (ej. PVC a 220°C) e ISO 8000.
  6. ☁️ Síntesis Oracle       -> Formato final en MAYÚSCULAS con bajo riesgo de reproceso.
```

### 1. 🛡️ Detector Anti-Frankenstein (Seguridad Físico-Química)
Identifica mezclas de materiales y condiciones de operación comercialmente inexistentes o peligrosas.  
*Ejemplo:* Si el usuario solicita **tubería de PVC para vapor de caldera a 220 °C**, el motor frena en seco la creación (límite de 60 °C del PVC), enciende una alerta roja destacada y sugiere alternativas industriales viables (Acero al carbono SCH40).

### 2. 🏷️ Gobernanza de Marcas, Repuestos OEM y Norma ISO 8000 / OTAN (NCS)
Resuelve el dilema entre marcas cautivas y libre competencia:
* **Tipo 1 (Suministros Estándar):** Neutraliza marcas comerciales genéricas como referencia (`REF: MARCA`) y exige dimensiones bajo norma.
* **Tipo 2/3 (Piezas Cautivas de Maquinaria):** Para repuestos exclusivos (ej. *Impulsor Flygt*), **exonera** de medir dimensiones con calibrador vernier en campo y aplica la **Fórmula Canónica de 4 Elementos**:  
  `[Sustantivo específico] + [Atributo macro visible] + OEM [Marca] + N/P [Código de parte]`.

### 3. 🔍 Atajo de Catálogo en 1 Clic con Delta
Cruza la necesidad contra el histórico maestro del ERP. Si encuentra un artículo existente, calcula la intersección técnica y le presenta al usuario los atributos confirmados vs el *delta* adicional, permitiendo adoptar el código oficial en 1 clic para evitar ítems duplicados.

### 4. 🏷️ Desacople Estricto de UOM y Trampa de Empaques
Expulsa automáticamente las cantidades totales del pedido fuera de la descripción corta y clasifica la unidad de medida ERP adecuada (`UND`, `KIT`, `M`, `RLL`).

### 5. 📋 Detector de Combos (Multilínea ERP)
Bifurca requerimientos mixtos que combinan suministros de inventario con servicios de instalación/mantenimiento, organizándolos en una bandeja de cola interactiva para su correcta imputación contable.

---

## ⚡ Telemetría y Viabilidad Financiera

El sistema incluye un monitor en tiempo real de consumo y costos:
* **Consumo promedio:** ~800 tokens entrada / ~350 tokens salida por requerimiento.
* **Costo por consulta:** Menos de **\$0.00004 USD** con Gemini Flash.
* **Proyección:** Procesar **1,000 requerimientos cuesta menos de \$0.05 USD**, amortizando el costo desde la primera compra errada que el motor previene.

---

## 📦 Instalación y Ejecución Local

### Prerrequisitos
* Python 3.10 o superior.
* Clave de API de Google Gemini ([Google AI Studio](https://aistudio.google.com/)).

### Pasos rápidos (Linux / macOS)
```bash
# 1. Clonar repositorio
git clone https://github.com/jharod99/Chatbot_Sol.git
cd Chatbot_Sol

# 2. Configurar variables de entorno
cp .env.example .env
# Edita .env y coloca tu GEMINI_API_KEY=tu_clave_aqui

# 3. Ejecutar
./ejecutar.sh
```
El script creará automáticamente el entorno virtual, instalará las dependencias y abrirá la aplicación en `http://127.0.0.1:5000`.

---

## ☁️ Despliegue en la Nube (1 Clic en Render)

El repositorio incluye soporte nativo para **[Render](https://render.com/)** (Plan gratuito):

1. Haz un fork o sube este repositorio a tu cuenta de GitHub.
2. En Render, crea un nuevo **Web Service** y conecta tu repositorio `Chatbot_Sol`.
3. Render detectará automáticamente el archivo `render.yaml` y `Procfile`.
4. Agrega la variable de entorno:
   * `GEMINI_API_KEY`: tu clave de API de Gemini.
5. Haz clic en **Deploy Web Service** y obtendrás una URL pública y gratuita `https://tu-servicio.onrender.com`.

---

## 🏛️ Estructura del Proyecto

```
Chatbot_Sol/
├── estructurador-erp/
│   ├── app.py                     # Motor principal Flask + Pipeline de 6 etapas + Gemini
│   ├── historico_referencia.csv   # Catálogo maestro histórico con 4 clusters de prueba
│   ├── requirements.txt           # Dependencias Python
│   ├── ejecutar.sh                # Script de arranque automatizado
│   └── templates/
│       └── index.html             # Interfaz interactiva + Árbol en vivo + Telemetría
├── wsgi.py                        # Entrypoint WSGI para servidores de producción
├── Procfile                       # Definición de proceso para plataformas PaaS
├── render.yaml                    # Blueprint de infraestructura como código (Render)
├── requirements.txt               # Dependencias en la raíz para despliegues cloud
├── .env.example                   # Plantilla de variables de entorno
└── README.md                      # Documentación oficial
```

---

## 📄 Licencia

Este proyecto está bajo la Licencia MIT. Consulta el archivo [LICENSE](LICENSE) para más detalles.
