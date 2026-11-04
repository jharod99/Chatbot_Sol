import csv
from difflib import SequenceMatcher
import io
import json
import os
import re
import time
import unicodedata
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

load_dotenv()

app = Flask(__name__)

api_key = os.environ.get("GEMINI_API_KEY")
client = (
    genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=25000))
    if api_key
    else None
)

CSV_DEFAULT_PATH = os.path.join(
    os.path.dirname(__file__), "historico_referencia.csv"
)
CATALOGO_HISTORICO = []

# =====================================================================
# MOTOR DE CARGA Y BÚSQUEDA DE HARD-TOKENS Y COLAPSO DE VARIANTES
# =====================================================================
STOPWORDS = {
    "de",
    "para",
    "con",
    "en",
    "el",
    "la",
    "los",
    "las",
    "un",
    "una",
    "unos",
    "unas",
    "por",
    "del",
    "al",
    "que",
    "se",
    "necesito",
    "requiero",
    "solicito",
    "comprar",
    "pedir",
    "busco",
    "taller",
    "planta",
    "area",
}


def limpiar_tildes(texto: str) -> str:
  return (
      unicodedata.normalize("NFKD", texto)
      .encode("ASCII", "ignore")
      .decode("utf-8")
  )


def normalizar_texto_medidas(texto: str) -> str:
  t = limpiar_tildes(texto.lower())
  # Normalizar pulgadas
  t = re.sub(
      r'(\d+(?:/\d+)?(?:\.\d+)?)\s*(?:"|\'\'|pulgadas?|pulg\.?)\b',
      r"\1 pulg",
      t,
  )
  # Normalizar magnitudes comunes
  t = re.sub(r"(\d+)\s*(amperios?|amps?|a)\b", r"\1a", t)
  t = re.sub(r"(\d+)\s*(hp|caballos?)\b", r"\1hp", t)
  t = re.sub(r"(\d+)\s*(vac|voltios?\s*ac)\b", r"\1vac", t)
  t = re.sub(r"(\d+)\s*(vdc|voltios?\s*dc)\b", r"\1vdc", t)
  t = re.sub(r"(\d+)\s*(v|voltios?)\b", r"\1v", t)
  t = re.sub(r"(\d+)\s*(libras?|lbs?)\b", r"\1lb", t)
  t = re.sub(r"(\d+)\s*(galones?|gln)\b", r"\1gln", t)
  t = re.sub(r"(\d+)\s*(metros?|mts?|m)\b", r"\1m", t)
  return t


def extraer_tokens_completo(texto: str):
  t_norm = normalizar_texto_medidas(texto)

  # Hard-Tokens: números con unidades/fracciones y códigos alfanuméricos
  patron_medidas = re.findall(
      r"\b\d+(?:/\d+)?(?:\.\d+)?\s*(?:pulg|mm2?|cm|m|awg|lb|vac|vdc|v|hp|a|l/s|gln|kg|rpm|h|ip\d{2})\b",
      t_norm,
  )
  patron_codigos = re.findall(
      r"\b[a-z0-9]+(?:-[a-z0-9]+)+\b|\b\d{4,}\b|\b316\b|\b304\b|\bsch40\b|\ba106\b|\biec60309\b|\bral\d{4}\b",
      t_norm,
  )

  hard_tokens = set([m.strip() for m in patron_medidas + patron_codigos])

  dimensiones = {}
  for ht in hard_tokens:
    m = re.match(r"^(\d+(?:/\d+)?(?:\.\d+)?)\s*([a-z]+)$", ht)
    if m:
      val, unit = m.groups()
      dimensiones.setdefault(unit, set()).add(ht)

  palabras = re.findall(r"[a-z0-9]+", t_norm)
  semanticos = {
      p
      for p in palabras
      if len(p) > 2 and p not in STOPWORDS and not any(p in ht for ht in hard_tokens)
  }

  return semanticos, hard_tokens, dimensiones


def cargar_csv_en_memoria(contenido_texto: str) -> int:
  global CATALOGO_HISTORICO
  lineas = [l for l in contenido_texto.splitlines() if l.strip()]
  if not lineas:
    return 0

  primera_linea = lineas[0]
  delimitador = (
      ";" if primera_linea.count(";") > primera_linea.count(",") else ","
  )

  lector = csv.DictReader(io.StringIO(contenido_texto), delimiter=delimitador)
  nuevos_registros = []

  for idx, fila in enumerate(lector, 1):
    valores_limpios = {
        (k or "").strip().lower(): (v or "").strip() for k, v in fila.items()
    }
    descripcion = (
        valores_limpios.get("descripcion_corta_oracle")
        or valores_limpios.get("descripcion")
        or valores_limpios.get("item")
        or next(
            (v for v in valores_limpios.values() if len(v) > 12),
            "",
        )
    )
    if not descripcion:
      continue

    codigo = (
        valores_limpios.get("codigo_erp")
        or valores_limpios.get("codigo")
        or f"HIST-{idx:04d}"
    )
    familia = (
        valores_limpios.get("familia")
        or valores_limpios.get("categoria")
        or "GENERAL"
    )
    uom = (
        valores_limpios.get("uom")
        or valores_limpios.get("unidad")
        or "UND"
    ).upper()

    texto_para_tokens = f"{familia} {descripcion}"
    semanticos, hard_tokens, dimensiones = extraer_tokens_completo(texto_para_tokens)

    nuevos_registros.append({
        "codigo_erp": codigo,
        "familia": familia,
        "uom": uom,
        "descripcion_corta_oracle": descripcion.upper(),
        "semanticos": semanticos,
        "hard_tokens": hard_tokens,
        "dimensiones": dimensiones,
    })

  if nuevos_registros:
    CATALOGO_HISTORICO = nuevos_registros
  return len(nuevos_registros)


def inicializar_csv_por_defecto():
  if os.path.exists(CSV_DEFAULT_PATH):
    with open(CSV_DEFAULT_PATH, "r", encoding="utf-8-sig") as f:
      cargar_csv_en_memoria(f.read())


inicializar_csv_por_defecto()


def buscar_ejemplos_similares(consulta: str, top_k: int = 6) -> list[dict]:
  if not CATALOGO_HISTORICO:
    return []

  q_semanticos, q_hard, q_dimensiones = extraer_tokens_completo(consulta)
  consulta_upper = consulta.upper()
  resultados_puntuados = []

  for reg in CATALOGO_HISTORICO:
    score = 0.0

    # 1. Coincidencia de Tokens Semánticos (sustantivos y adjetivos)
    for ts in q_semanticos:
      raiz = ts[:4]
      for tr in reg["semanticos"]:
        if ts == tr:
          score += 3.0
        elif tr.startswith(raiz) or ts.startswith(tr[:4]):
          score += 1.5

    # 2. Hard-Tokens de Medida y Magnitud (+10 si coincide exacto, -5 si difiere en misma magnitud)
    if q_dimensiones:
      for unit, q_vals in q_dimensiones.items():
        if unit in reg["dimensiones"]:
          if q_vals.intersection(reg["dimensiones"][unit]):
            score += 10.0  # Coincidencia exacta de medida
          else:
            score -= 5.0  # Misma magnitud pero diferente medida (penalización)

    # 3. Coincidencia de otros Hard-Tokens (códigos de parte, grados de acero, normas)
    for ht in q_hard:
      if ht in reg["hard_tokens"]:
        score += 5.0

    # 4. Proximidad de secuencia
    ratio_secuencia = SequenceMatcher(
        None, consulta_upper, reg["descripcion_corta_oracle"]
    ).ratio()
    score += ratio_secuencia * 2.0

    resultados_puntuados.append((score, reg))

  resultados_puntuados.sort(key=lambda x: x[0], reverse=True)

  seleccionados = []
  for score, reg in resultados_puntuados[:top_k]:
    seleccionados.append({
        "codigo_erp": reg["codigo_erp"],
        "familia": reg["familia"],
        "uom": reg["uom"],
        "descripcion_corta_oracle": reg["descripcion_corta_oracle"],
        "relevancia": (
            "ALTA_SIMILITUD" if score >= 8.0 else "REFERENCIA_ESTILO_GENERAL"
        ),
    })
  return seleccionados


# =====================================================================
# ESQUEMAS DE DATOS ESTRICTOS (PYDANTIC)
# =====================================================================
class FiltroIntencion(BaseModel):
  intencion_insuficiente: bool = Field(
      description=(
          "True si el usuario escribió algo vacío de contenido técnico donde no"
          " hay ni siquiera un objeto o servicio identificable (ej. 'urgente"
          " para planta mañana', 'repuesto para máquina', 'materiales varios')."
      )
  )
  mensaje_aclaracion: str = Field(
      description=(
          "Pedido breve de 1 línea para que indique qué artículo o servicio"
          " necesita antes de generar preguntas."
      )
  )


class CandidatoCatalogo(BaseModel):
  codigo_erp: str = Field(description="Código ERP del ítem existente en el CSV.")
  uom: str = Field(
      description="Unidad de medida oficial del catálogo (UND, KIT, etc.)."
  )
  descripcion_corta_oracle: str = Field(
      description="Descripción histórica exacta del CSV."
  )
  atributos_confirmados_usuario: list[str] = Field(
      description=(
          "Lo que el usuario sí mencionó y coincide con este código (ej."
          " ['Válvula de bola', 'Acero Inox', '2 PULG'])."
      )
  )
  delta_no_confirmado: list[str] = Field(
      description=(
          "Atributos específicos que tiene este ítem del CSV pero que el"
          " usuario aún NO mencionó (ej. ['Grado 316', 'Conexión Brida ANSI"
          " 150LB', 'Paso Total'])."
      )
  )


class CruceCatalogoCSV(BaseModel):
  estado_cruce: str = Field(
      description=(
          "Valores permitidos: 'SIN_COINCIDENCIA', 'SOLO_MOLDE_SINTACTICO',"
          " 'CANDIDATO_EN_CATALOGO', 'DUPLICADO_EXACTO_DETECTADO'"
      )
  )
  mensaje_catalogo: str = Field(
      description="Explicación del resultado del cruce con el catálogo."
  )
  candidatos_atajo: list[CandidatoCatalogo] = Field(
      default_factory=list,
      description=(
          "Lista de los códigos del CSV que calzan como candidatos directos"
          " (máximo 5)."
      ),
  )


class ControlUOM(BaseModel):
  cantidad_pedido_extraida: str = Field(
      description=(
          "Si el usuario puso una cantidad total en su texto (ej. '12 válvulas',"
          " '300 metros de cable'), extrae aquí '12' o '300'. Si no puso, deja"
          " ''."
      )
  )
  uom_sugerida: str = Field(
      description="Unidad de medida oficial: UND, KIT, JGO, RLL, M, KG, GLN, SRV."
  )
  regla_empaque_aplicada: str = Field(
      description=(
          "Breve nota explicando la coherencia entre la UOM y el cierre de la"
          " descripción."
      )
  )


class ControlMarcaOEM(BaseModel):
  se_detecto_marca_o_np: bool = Field(
      description="True si se detectó una marca, fabricante o número de parte."
  )
  marca_detectada: str = Field(
      description="Marca o fabricante detectado, o '' si no hay."
  )
  clasificacion_iso: str = Field(
      description=(
          "Valores: 'NINGUNA', 'TIPO_1_SUMINISTRO_NORMA',"
          " 'TIPO_2_3_PIEZA_CAUTIVA_OEM'"
      )
  )
  requiere_decision_usuario: bool = Field(
      description=(
          "True si es una pieza de máquina (TIPO_2_3_PIEZA_CAUTIVA_OEM) y el"
          " usuario aún no ha definido si acepta alternativo o exige repuesto"
          " exclusivo OEM."
      )
  )
  modalidad_actual: str = Field(
      description="Valores: 'REFERENCIA_ABIERTA' o 'EXCLUSIVO_OEM'"
  )
  mensaje_iso: str = Field(
      description="Explicación de cómo se trató la marca según ISO 8000/NCS."
  )


class ControlAlcance(BaseModel):
  es_multiple: bool = Field(
      description=(
          "True si el usuario mezcla múltiples ítems o combina compra de"
          " material con servicio."
      )
  )
  tipo_detectado: str = Field(description="Valores: MATERIAL, SERVICIO o MIXTO")
  mensaje_separacion: str = Field(
      description="Explicación breve si es_multiple es True."
  )
  lineas_detectadas: list[str] = Field(
      description=(
          "Lista de cada requerimiento individual separado para elegir uno."
      )
  )


class ValidacionMercado(BaseModel):
  viabilidad_comercial: str = Field(
      description="Valores: VALIDO, INCOMPATIBLE o MEDIDA_NO_ESTANDAR"
  )
  mensaje_alerta: str = Field(
      description=(
          "Explicación técnica clara si el usuario pide algo inexistente o"
          " peligroso."
      )
  )
  sugerencias_correccion: list[str] = Field(
      description="Alternativas comerciales reales bajo norma."
  )


class TraduccionJerga(BaseModel):
  hubo_traduccion: bool
  termino_original: str
  termino_normalizado: str


class AtributoResuelto(BaseModel):
  nombre_parametro: str
  valor_asignado: str
  origen_dato: str = Field(
      description="Valores: CONFIRMADO, DEDUCIDO_POR_USO o POR_MEDIR_EN_CAMPO"
  )


class AtributoPendiente(BaseModel):
  id: str
  nombre_amigable: str
  por_que_importa: str
  opciones_rapidas: list[str]
  pregunta_funcional: str
  opciones_funcionales: list[str]
  instruccion_fisica_campo: str


class PatronHistoricoAplicado(BaseModel):
  se_uso_patron_csv: bool = Field(
      description=(
          "True si se pudo deducir una fórmula sintáctica a partir de los"
          " ejemplos crudos del CSV."
      )
  )
  codigo_referencia_usado: str = Field(
      description=(
          "Código ERP del CSV tomado como referencia principal (ej. MAT-10021)."
      )
  )
  estructura_adoptada: str = Field(
      description=(
          "Fórmula sintáctica deducida automáticamente por ingeniería inversa"
          " (ej. 'Fórmula deducida: [SUSTANTIVO] + [MONTAJE] + [AMPERAJE] +"
          " [POLOS] + [VOLTAJE] + [IP]')."
      )
  )


class SalidaERP(BaseModel):
  descripcion_corta_oracle: str = Field(
      description=(
          "Descripción en MAYÚSCULAS bajo formato ERP Oracle Fusion (máximo 120"
          " caracteres ideales, tope 240)."
      )
  )
  ficha_ampliada_texto: str
  estado: str = Field(
      description=(
          "Valores: LISTO_PARA_COPIAR, BORRADOR_CON_PENDIENTES,"
          " BLOQUEADO_POR_INCOMPATIBILIDAD, REQUIERE_SEPARAR_LINEAS,"
          " CODIGO_EXISTENTE_EN_CATALOGO"
      )
  )
  riesgo_reproceso: str = Field(description="Valores: BAJO, MEDIO o ALTO")
  notas_trazabilidad: list[str]


class PasoDecision(BaseModel):
  etapa: str = Field(description="Nombre de la etapa (ej. '1. Ingesta, Intención y Purga de Ruido')")
  pregunta_evaluada: str = Field(description="Pregunta clave evaluada (ej. '¿La solicitud contiene información técnica o solo ruido?')")
  veredicto: str = Field(description="Veredicto del motor (ej. 'APROBADO_CON_PURGA', 'BIFURCACION_COMBO', 'ATAJO_ENCONTRADO', 'BLOQUEO_SEGURIDAD')")
  explicacion_para_audiencia: str = Field(description="Explicación didáctica y precisa de cómo y por qué se decidió esto para mostrar al público")
  regla_aplicada: str = Field(description="Regla técnica aplicada (ej. 'Regla 0 (Intención) + Regla 7 (Purga de Ruido)')")
  icono: str = Field(description="Emoji representativo (ej. '🧹', '📋', '🔍', '🏷️', '🛡️', '☁️')")


class RespuestaEstructurador(BaseModel):
  filtro_intencion: FiltroIntencion
  control_alcance: ControlAlcance
  validacion_mercado: ValidacionMercado
  cruce_catalogo_csv: CruceCatalogoCSV
  control_uom: ControlUOM
  control_marca_oem: ControlMarcaOEM
  traduccion_jerga: TraduccionJerga
  patron_historico: PatronHistoricoAplicado
  atributos_resueltos: list[AtributoResuelto]
  atributos_pendientes: list[AtributoPendiente]
  salida_erp: SalidaERP
  arbol_decisiones: list[PasoDecision] = Field(
      description="Lista secuencial de 6 pasos de decisión explicables para proyectar a la audiencia."
  )


# =====================================================================
# SYSTEM PROMPT DETERMINISTA CON LAS 5 REGLAS CRÍTICAS
# =====================================================================
SYSTEM_PROMPT = """
Eres un Arquitecto Senior de Catalogación y Especificaciones Técnicas para Oracle Fusion Cloud ERP.
Tu objetivo es transformar peticiones ambiguas de usuarios en descripciones técnicas estandarizadas, comercialmente existentes y libres de reprocesos.

REGLAS DE BLINDAJE DETERMINISTAS OBLIGATORIAS:

0. FILTRO DE INTENCIÓN INSUFICIENTE:
   - Si el usuario escribió un texto sin contenido técnico donde no hay ni un bien ni un servicio identificable (ej. "urgente para planta mañana", "repuesto para máquina", "materiales varios"), marca `filtro_intencion.intencion_insuficiente = true`, genera un `mensaje_aclaracion` de 1 línea solicitando especificar el artículo y NO generes atributos pendientes ficticios. Estado ERP: `BORRADOR_CON_PENDIENTES`.

1. CRUCE CON CATÁLOGO CSV Y ATAJO EN 1 CLIC:
   - Revisa exhaustivamente los `EJEMPLOS HISTÓRICOS CRUDOS DEL CSV`.
   - Si todos los datos dados por el usuario coinciden con uno o más registros del CSV sin contradicción técnica, pero le faltan datos que sí tiene la fila del CSV (ej. el usuario pide "válvula de bola de acero inoxidable de 2 pulgadas" y el CSV tiene MAT-20410 con Grado 316, Brida ANSI 150LB, Paso Total):
     * Marca `cruce_catalogo_csv.estado_cruce = "CANDIDATO_EN_CATALOGO"`.
     * Lista hasta 5 candidatos en `candidatos_atajo`.
     * Detalla con precisión quirúrgica en `atributos_confirmados_usuario` lo que el usuario sí dijo (ej. ["Válvula de bola", "Acero Inox", "2 PULG"]).
     * Detalla en `delta_no_confirmado` los atributos que tiene el CSV que el usuario aún NO mencionó (ej. ["Grado 316", "Conexión Brida ANSI 150LB", "Paso Total"]).
   - Si el usuario envía la interacción `[ATAJO_CATALOGO_ACEPTADO]`:
     * Cambia `cruce_catalogo_csv.estado_cruce = "DUPLICADO_EXACTO_DETECTADO"`.
     * Vacía `atributos_pendientes = []`.
     * Fija `descripcion_corta_oracle` y `uom_sugerida` idénticas a las del código aceptado.
     * Marca `salida_erp.estado = "CODIGO_EXISTENTE_EN_CATALOGO"` y `riesgo_reproceso = "BAJO"`.
   - HERENCIA SIN REPREGUNTA: Si el usuario envía `[ATAJO_CATALOGO_RECHAZADO]`:
     * Pasa `estado_cruce` a `"SOLO_MOLDE_SINTACTICO"`.
     * PROHIBIDO volver a preguntar los atributos que ya estaban en `atributos_confirmados_usuario`: muévelos a `atributos_resueltos` con `origen_dato = "CONFIRMADO"`.
     * Genera `atributos_pendientes` ÚNICAMENTE para los atributos que estaban en el `delta_no_confirmado` (ej. tipo de conexión o presión requerida).

2. UNIDAD DE MEDIDA (UOM) Y EXPULSIÓN DE CANTIDAD:
   - PROHIBIDO incluir la cantidad total del pedido en `descripcion_corta_oracle` en bienes materiales. Si el usuario pide "4 válvulas", "20 galones de pintura", "50 metros de cable", extrae "4", "20", "50" en `control_uom.cantidad_pedido_extraida` y describe el ítem unitario. (En SERVICIOS sí se incluye el metraje/alcance a intervenir).
   - Ítems discretos unitarios -> UOM = "UND".
   - Ítems continuos en presentación cerrada (rollo 100m, tubo tramo 6m) -> UOM = "RLL" o "UND" y añade sufijo descriptivo (ej. "ROLLO 100M" o "X 6M"). Si se compran al corte -> UOM = "M" o "KG" sin longitud total en la descripción.
   - Ítems multicomponente (kits de pintura A+B, juegos de empaquetaduras) -> UOM = "KIT" o "JGO" y añade sufijo de composición (ej. "PARTE A+B KIT 1 GLN", "JGO X 4 PZAS").

3. REGLA ISO 8000 / NCS PARA MARCAS Y NÚMEROS DE PARTE (SIN MODELO DE MÁQUINA EN DESCRIPCIÓN):
   - Tipo 1 (Suministro Estándar Regido por Norma o "Falso OEM"): Rodamientos (SKF), tomacorrientes IEC (Mennekes), tuberías ASTM, cables, pernos. NUNCA exoneres medidas físicas. Neutraliza la marca al final como `(REF: MARCA)` y exige sus dimensiones/capacidades críticas bajo norma. `clasificacion_iso = "TIPO_1_SUMINISTRO_NORMA"`.
   - Tipo 2/3 (Pieza Cautiva o Repuesto Interno OEM): Impulsores, tarjetas electrónicas, partes propietarias de maquinaria (Flygt, Atlas Copco). `clasificacion_iso = "TIPO_2_3_PIEZA_CAUTIVA_OEM"`.
     * Si el usuario no ha aclarado si acepta equivalente o exige repuesto exclusivo, activa `requiere_decision_usuario = true`.
     * Si el usuario elige o confirma `EXCLUSIVO_OEM`: EXONERA pedir medidas finas con vernier/calibrador (prohibido pedir diámetros o espesores milimétricos).
     * En nuestro ERP el modelo de máquina va en otro campo; NO pidas ni incluyas el modelo de la máquina en la descripción corta. Exige obligatoriamente la Fórmula de 4 Elementos OEM:
       `[1. SUSTANTIVO ESPECÍFICO + SUBTIPO (prohibido usar 'Repuesto')] + [2. ATRIBUTO MACRO VISIBLE O ELÉCTRICO (ej. ACERO INOX 3 ALABES o 24VDC)] + [3. OEM + MARCA] + [4. N/P + CÓDIGO DE PARTE]`.
       Ejemplo: `IMPULSOR CERRADO ACERO INOX 3 ALABES OEM FLYGT N/P 602145`.

4. DETECTOR DE COMBOS (Línea Única):
   - En un ERP nunca se mezclan múltiples ítems distintos ni "Material + Servicio" en una sola línea.
   - Si detectas más de un requerimiento, marca `es_multiple = true`, detalla cada línea por separado en `lineas_detectadas` y pon `estado = "REQUIERE_SEPARAR_LINEAS"`.

5. VALIDADOR ANTI-FRANKENSTEIN (Coherencia Física y Comercial):
   - Si el usuario pide combinaciones inexistentes o físicamente peligrosas (ej. "tubería PVC para vapor a 220°C"), marca `viabilidad_comercial = "INCOMPATIBLE"`, explica la alerta técnica en `mensaje_alerta` y sugiere estándares reales en `sugerencias_correccion`.

6. INGENIERÍA INVERSA DE SINTAXIS:
   - Deducir la fórmula del CSV y expresarla en `patron_historico.estructura_adoptada`.
   - Máximo 4 `atributos_pendientes` discriminantes para no generar interrogatorios infinitos.

7. PURGA AUTOMÁTICA DE RUIDO OPERATIVO Y ADMINISTRATIVO (PROHIBIDO EN LA DESCRIPCIÓN ERP):
   - Todo dato ajeno a la especificación técnica física debe ser completamente ignorado y purgado de `descripcion_corta_oracle`:
     a) Urgencias y plazos: "urgente", "para mañana", "para ayer", "prioridad alta", "ASAP" (corresponde al campo Need-by Date / Urgent Flag en la cabecera de Oracle).
     b) Ubicaciones y destinos de consumo: "para planta 2", "para caldera 3", "para mina", "para almacén", "para taller" (corresponde a Deliver-to Location y Cuenta Contable / Centro de Costos).
     c) Personas y solicitantes: "para el Ing. Carlos", "pedido por Juan", "autorizado por gerencia" (corresponde al campo Requester / Preparer).
     d) Referencias administrativas temporales: "según cotización 123", "comprar al proveedor ABC", "OC anterior 450", "precio $100".
     e) Adjetivos subjetivos o publicitarios: "de buena calidad", "resistente", "económico", "de primera", "o similar".
     f) Fórmulas de cortesía y saludos: "buenos días favor adquirir", "gracias".
   - La descripción en el maestro de artículos de Oracle debe ser 100% técnica, objetiva, perenne y neutral al solicitante o destino de uso.

8. GENERACIÓN DEL ÁRBOL DE DECISIONES EXPLICABLE (`arbol_decisiones`):
   - Genera OBLIGATORIAMENTE una lista de exactamente 6 pasos de decisión secuenciales en `arbol_decisiones`, explicando con claridad y didáctica para una audiencia cómo y por qué el motor tomó cada decisión:
     1) Etapa: "1. Ingesta, Intención y Purga de Ruido" | Icono: "🧹". Pregunta: "¿La solicitud contiene intención técnica real o solo ruido operativo?". Veredicto y explicación de qué sustantivo o bien identificó y qué ruido (urgencias, lugares, personas) purgó.
     2) Etapa: "2. Control de Alcance (Línea Única vs Combo)" | Icono: "📋". Pregunta: "¿Es un requerimiento único o mezcla materiales y servicios?". Veredicto y explicación de si se mantuvo monolínea o se bifurcó a bandeja de combos.
     3) Etapa: "3. Cruce con Catálogo Maestro Histórico" | Icono: "🔍". Pregunta: "¿Existe en el catálogo histórico o requiere catalogación nueva?". Veredicto y explicación de coincidencia (atajo en 1 clic con delta, múltiples variantes, duplicado o nuevo ítem).
     4) Etapa: "4. Desacople de UOM y Empaque" | Icono: "🏷️". Pregunta: "¿Qué UOM ERP corresponde y hay cantidades que expulsar del texto?". Veredicto y explicación de la UOM asignada (ej. UND, KIT, MTR) y la cantidad extraída.
     5) Etapa: "5. Seguridad Anti-Frankenstein e ISO 8000" | Icono: "🛡️". Pregunta: "¿La especificación es físicamente viable y respeta normas de marca OEM?". Veredicto y explicación sobre compatibilidad físico-química y si aplica regla cautiva OEM.
     6) Etapa: "6. Normalización Formato Oracle Fusion" | Icono: "☁️". Pregunta: "¿Cumple con la sintaxis, mayúsculas y límite de 120/240 caracteres de Oracle?". Veredicto y explicación de cómo quedó estructurada la descripción corta y el nivel de riesgo de reproceso.
"""


# Tarifas oficiales por 1 Millón de tokens (USD)
TARIFAS_MODELOS_USD_1M = {
    "gemini-flash-lite-latest": {"input": 0.075, "output": 0.30},
    "gemini-3.1-flash-lite": {"input": 0.075, "output": 0.30},
    "gemini-2.5-flash-lite": {"input": 0.075, "output": 0.30},
    "gemini-3.8-flash": {"input": 0.15, "output": 0.60},
    "gemini-2.5-flash": {"input": 0.15, "output": 0.60},
    "default": {"input": 0.10, "output": 0.40},
}


@app.route("/")
def index():
  return render_template("index.html", total_csv=len(CATALOGO_HISTORICO))


@app.route("/api/cargar_csv", methods=["POST"])
def subir_csv():
  if "archivo_csv" not in request.files:
    return jsonify({"error": "No se envió ningún archivo CSV."}), 400

  archivo = request.files["archivo_csv"]
  if not archivo.filename:
    return jsonify({"error": "Nombre de archivo vacío."}), 400

  raw_bytes = archivo.read()
  try:
    contenido = raw_bytes.decode("utf-8-sig")
  except UnicodeDecodeError:
    contenido = raw_bytes.decode("latin-1", errors="ignore")

  cantidad = cargar_csv_en_memoria(contenido)
  if cantidad == 0:
    return (
        jsonify({
            "error": (
                "No se encontraron filas válidas. Incluye una columna con las"
                " descripciones."
            )
        }),
        400,
    )

  return jsonify({
      "mensaje": (
          f"Se cargaron {cantidad} descripciones históricas exitosamente."
      ),
      "total_registros": cantidad,
  })


@app.route("/api/analizar", methods=["POST"])
def analizar():
  if not client:
    return (
        jsonify({
            "error": "No se encontró GEMINI_API_KEY en tu archivo .env"
        }),
        500,
    )

  payload = request.json or {}
  texto_base = payload.get("texto_base", "").strip()
  interacciones = payload.get("interacciones", [])

  if not texto_base:
    return (
        jsonify({"error": "Por favor ingresa una descripción inicial."}),
        400,
    )

  ejemplos_csv = buscar_ejemplos_similares(texto_base, top_k=6)

  contexto_usuario = (
      "EJEMPLOS HISTÓRICOS CRUDOS DEL CSV DE LA OPERACIÓN (Analiza estas"
      " cadenas de texto, deduce su patrón sintáctico por ingeniería inversa e"
      " imita sus abreviaturas):\n"
  )
  for ej in ejemplos_csv:
    contexto_usuario += (
        f"- [{ej['codigo_erp']}] ({ej['familia']}) [UOM: {ej['uom']}]:"
        f" \"{ej['descripcion_corta_oracle']}\" | Nivel coincidencia:"
        f" {ej['relevancia']}\n"
    )

  contexto_usuario += f'\nSOLICITUD INICIAL DEL USUARIO:\n"{texto_base}"\n\n'
  if interacciones:
    contexto_usuario += (
        "DATOS ADICIONALES Y DECISIONES TOMADAS POR EL USUARIO EN LA"
        " INTERFAZ:\n"
    )
    for idx, item in enumerate(interacciones, 1):
      contexto_usuario += (
          f"{idx}. [{item.get('tipo')}] {item.get('atributo')}:"
          f" {item.get('valor')}\n"
      )

  # Cascada de modelos para garantizar alta disponibilidad ante 503 (sobrecarga) o 404 (deprecados)
  modelos_a_probar = []
  modelo_env = os.environ.get("GEMINI_MODEL")
  if modelo_env:
    modelos_a_probar.append(modelo_env)
  for m in ["gemini-flash-lite-latest", "gemini-3.1-flash-lite", "gemini-3.8-flash"]:
    if m not in modelos_a_probar:
      modelos_a_probar.append(m)

  ultimo_error = None

  for modelo in modelos_a_probar:
    try:
      t_inicio = time.time()
      response = client.models.generate_content(
          model=modelo,
          contents=contexto_usuario,
          config=types.GenerateContentConfig(
              system_instruction=SYSTEM_PROMPT,
              response_mime_type="application/json",
              response_schema=RespuestaEstructurador,
              temperature=0.1,
          ),
      )
      latencia = round(time.time() - t_inicio, 2)
      resultado_json = json.loads(response.text)
      resultado_json["ejemplos_csv_recuperados"] = ejemplos_csv
      resultado_json["modelo_utilizado"] = modelo

      # Extracción y cálculo de telemetría de consumo
      tokens_entrada = (
          getattr(response.usage_metadata, "prompt_token_count", 0) or 0
      )
      tokens_salida = (
          getattr(response.usage_metadata, "candidates_token_count", 0) or 0
      )
      tokens_totales = getattr(
          response.usage_metadata,
          "total_token_count",
          tokens_entrada + tokens_salida,
      ) or (tokens_entrada + tokens_salida)

      tarifas = TARIFAS_MODELOS_USD_1M.get(
          modelo, TARIFAS_MODELOS_USD_1M["default"]
      )
      costo_in = (tokens_entrada / 1_000_000) * tarifas["input"]
      costo_out = (tokens_salida / 1_000_000) * tarifas["output"]
      costo_consulta = round(costo_in + costo_out, 6)
      proyeccion_1k = round(costo_consulta * 1000, 3)

      resultado_json["telemetria"] = {
          "modelo": modelo,
          "tokens_entrada": tokens_entrada,
          "tokens_salida": tokens_salida,
          "tokens_totales": tokens_totales,
          "costo_consulta_usd": costo_consulta,
          "costo_entrada_usd": round(costo_in, 6),
          "costo_salida_usd": round(costo_out, 6),
          "latencia_segundos": latencia,
          "proyeccion_1000_consultas_usd": proyeccion_1k,
          "tarifa_referencia": tarifas,
      }

      return jsonify(resultado_json)
    except Exception as e:
      ultimo_error = e
      continue

  return (
      jsonify({"error": f"Error al procesar con la API de LLM: {str(ultimo_error)}"}),
      500,
  )


if __name__ == "__main__":
  port = int(os.environ.get("PORT", 5000))
  host = os.environ.get("HOST", "0.0.0.0")
  app.run(host=host, port=port, debug=False, use_reloader=False)

