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


class AuditoriaMagnitud(BaseModel):
  parametro: str = Field(
      description=(
          "Nombre técnico de la magnitud o escala evaluada (ej. 'Grano"
          " abrasivo', 'Calibre conductor', 'Diámetro nominal tubería')."
      )
  )
  valor_solicitado: str = Field(
      description="Valor numérico o calibre exacto pedido por el usuario."
  )
  norma_industrial_aplicable: str = Field(
      description=(
          "Norma internacional o estándar de fabricación que rige esta"
          " serie discreta (ej. 'FEPA / ISO 6344', 'ASTM B258 AWG',"
          " 'ASME B36.10 NPS', 'ISO 261', 'IEC 60269')."
      )
  )
  serie_comercial_vecina: list[str] = Field(
      default_factory=list,
      description=(
          "Lista ordenada de al menos 4 valores discretos comerciales REALES"
          " que se fabrican bajo esa norma alrededor del valor solicitado"
          " (incluyendo los escalones estándar inmediatamente inferior y"
          " superior). Prohibido incluir el valor_solicitado si no es un"
          " escalón estándar fabricado comercialmente."
      ),
  )
  existe_en_serie_estandar: bool = Field(
      description=(
          "True ÚNICAMENTE si valor_solicitado pertenece a los escalones"
          " discretos reales de la norma. False si cae en un hueco"
          " intermedio no estandarizado."
      )
  )


class ValidacionMercado(BaseModel):
  viabilidad_comercial: str = Field(
      description="Valores: VALIDO, INCOMPATIBLE o MEDIDA_NO_ESTANDAR"
  )
  mensaje_alerta: str = Field(
      description=(
          "Explicación técnica clara si el usuario pide algo inexistente,"
          " fuera de serie discreta normativa o físicamente peligroso."
      )
  )
  sugerencias_correccion: list[str] = Field(
      description="Alternativas comerciales reales bajo norma."
  )
  auditoria_magnitudes: list[AuditoriaMagnitud] = Field(
      default_factory=list,
      description=(
          "Auditoría normativa de cada magnitud, calibre, grano o dimensión"
          " numérica presente en la solicitud del usuario."
      ),
  )


class TraduccionJerga(BaseModel):
  hubo_traduccion: bool
  termino_original: str = Field(
      description=(
          "Únicamente el vocablo coloquial, regionalismo o marca genérica"
          " detectada (sin absorber el sustantivo ni la función primaria)."
      )
  )
  termino_normalizado: str = Field(
      description=(
          "Equivalente técnico estandarizado que reemplaza SOLO al"
          " termino_original, preservando intactos el sustantivo y la"
          " función primaria del artículo."
      )
  )


class EstructuraTaxonomica(BaseModel):
  sustantivo_nucleo: str = Field(
      description=(
          "Sustantivo técnico base en singular y mayúsculas que define QUÉ"
          " ES el artículo o servicio (ej. 'CONECTOR', 'VALVULA', 'GUANTE',"
          " 'DISCO')."
      )
  )
  modificador_funcional: str = Field(
      description=(
          "Función primaria o subtipo funcional expresado por el usuario"
          " que define PARA QUÉ SIRVE o QUÉ ACCIÓN REALIZA (ej."
          " 'DERIVACION', 'RETENCION', 'CORTE', 'SEGURIDAD ELECTRICA',"
          " 'BOLA'). Obligatorio extraerlo si el usuario lo expresó."
      )
  )
  mecanismo_o_forma: str = Field(
      description=(
          "Mecanismo físico, geometría o principio constructivo normalizado"
          " (ej. 'PERFORACION AISLANTE', 'CLAPETA OSCILANTE', 'FINO')."
          " Dejar '' si no aplica o si ya está en modificador_funcional."
      )
  )
  atributos_confirmados_compactos: list[str] = Field(
      default_factory=list,
      description=(
          "Lista de especificaciones, medidas y materiales que el usuario"
          " SÍ confirmó explícitamente en su entrada o interacciones, en"
          " formato compacto sin cláusulas 'PARA ...' ni palabras de"
          " relleno como 'CUERPO'. Prohibido incluir atributos supuestos o"
          " no confirmados."
      ),
  )


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
  firma_semantica_unspsc: str = Field(
      default="",
      description=(
          "Firma taxonómica conceptual limpia [SUSTANTIVO + FUNCIÓN +"
          " MECANISMO] sin medidas numéricas ni marcas, optimizada para"
          " clasificación vectorial UNSPSC aguas abajo."
      ),
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
  estructura_taxonomica: EstructuraTaxonomica
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
     * COLAPSO DE VARIANTES SIN MEDIDA: Si el usuario NO especificó medida (ej. solo pidió la familia y material como "válvula de bola de acero inoxidable") y todos los atributos que sí mencionó coinciden con varias filas del CSV sin contradicción, clasifica OBLIGATORIAMENTE `estado_cruce = "CANDIDATO_EN_CATALOGO"` y lista hasta 5 variantes de distintas medidas del CSV en `candidatos_atajo` (indicando la medida y demás atributos faltantes en `delta_no_confirmado`).
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

5. VALIDADOR ANTI-FRANKENSTEIN Y DISCRETIZACIÓN DE SERIES INDUSTRIALES:
   El motor Anti-Frankenstein actúa como guardián de gobernanza industrial contra dos tipos de aberraciones:

   A) Incompatibilidad Físico-Química y de Seguridad:
      - Combinaciones de materiales y condiciones de servicio físicamente imposibles, destructivas o peligrosas (por ejemplo, polímeros de baja temperatura expuestos a vapor de alta presión, sellos incompatibles con fluidos corrosivos o voltajes/presiones fuera de rango).
      - Marca `viabilidad_comercial = "INCOMPATIBLE"`, bloquea `salida_erp.estado = "BLOQUEADO_POR_INCOMPATIBILIDAD"`, explica el riesgo físico en `mensaje_alerta` y ofrece reemplazos estándar en `sugerencias_correccion`.

   B) Protocolo de Introspección Normativa Estructurada (Escalas Discretas):
      - En manufactura e ingeniería, las magnitudes, calibres, gradaciones, tamaños nominales o amperajes nominales NO son valores continuos; se fabrican únicamente en series discretas estandarizadas por normas internacionales (FEPA, CAMI, ASTM, ASME, ISO, IEC, NEMA, ANSI).
      - Para TODA magnitud constructiva nominal del artículo (grado, calibre, diámetro nominal, amperaje o tamaño de fabricación, excluyendo códigos OEM y condiciones operativas de proceso evaluadas en 5A como temperatura de fluido), debes poblar obligatoriamente `validacion_mercado.auditoria_magnitudes`:
        1) Identificar la `norma_industrial_aplicable` que rige esa familia de artículos.
        2) Listar en `serie_comercial_vecina` al menos 4 escalones discretos reales fabricados bajo esa norma alrededor del número solicitado (incluyendo el escalón estándar inmediatamente inferior y superior). PROHIBIDO incluir el `valor_solicitado` dentro de `serie_comercial_vecina` si NO es un escalón estándar real fabricado bajo esa norma.
        3) Evaluar `existe_en_serie_estandar`: `true` ÚNICAMENTE si `valor_solicitado` coincide exactamente con un escalón comercial real; `false` si cae en un hueco intermedio no fabricado.
      - Si `existe_en_serie_estandar = false`:
        * Marca `viabilidad_comercial = "MEDIDA_NO_ESTANDAR"`, fija `salida_erp.estado = "BLOQUEADO_POR_INCOMPATIBILIDAD"` y `riesgo_reproceso = "ALTO"`.
        * Explica en `mensaje_alerta` qué norma rige la magnitud y cuáles son los escalones comerciales adyacentes.
        * Genera en `sugerencias_correccion` exactamente las 2 descripciones completas correspondientes al escalón estándar inmediatamente inferior y superior.
        * Emite en la Etapa 5 de `arbol_decisiones` el veredicto `BLOQUEO_MEDIDA_NO_ESTANDAR`.

6. ARQUITECTURA SINTÁCTICA NOUN-MODIFIER, CERO ALUCINACIÓN E INGENIERÍA INVERSA DEL CSV:
   - Llenado Ortogonal Obligatorio de `estructura_taxonomica`:
     * `sustantivo_nucleo`: Qué es el artículo en singular técnico.
     * `modificador_funcional`: Función primaria o subtipo funcional expresado por el usuario (NUNCA lo elimines ni lo fusiones al traducir jerga física o constructiva en `traduccion_jerga`).
     * `mecanismo_o_forma`: Mecanismo físico, geometría o término técnico normalizado de la jerga constructiva.
     * `atributos_confirmados_compactos`: Solo dimensiones, capacidades y materiales expresamente confirmados por el usuario o en interacciones.
   - INVARIANTE DE CERO ALUCINACIÓN: Ningún atributo (material de fabricación, tensión BT/MT, aplicación o fluido) que NO haya sido mencionado por el usuario o aceptado en el catálogo CSV puede insertarse en `atributos_confirmados_compactos` ni en `descripcion_corta_oracle`. Si falta material, tensión o conexión, envíalo exclusivamente a `atributos_pendientes` (máximo 4 discriminantes).
   - ORDEN SINTÁCTICO Y CONSISTENCIA:
     * Si el CSV histórico aporta un molde con `ALTA_SIMILITUD`, deduce la fórmula en `patron_historico.estructura_adoptada` y sigue su estilo.
     * Cuando el CSV no imponga un patrón con `ALTA_SIMILITUD`, usa obligatoriamente el orden canónico:
       `[1. SUSTANTIVO_NUCLEO] + [2. MODIFICADOR_FUNCIONAL] + [3. MECANISMO_O_FORMA] + [4. ATRIBUTOS_CONFIRMADOS_COMPACTOS]`.
     * PROHIBIDO repetir la misma raíz morfológica dos veces en `descripcion_corta_oracle` y PROHIBIDO agregar cláusulas preposicionales verbosas como `"PARA RED..."` o palabras de relleno como `"CUERPO..."` cuando pueda omitirse o expresarse en forma compacta.

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
     5) Etapa: "5. Seguridad Anti-Frankenstein e ISO 8000" | Icono: "🛡️". Pregunta: "¿La especificación es físicamente viable y respeta normas de marca OEM?". Veredicto y explicación sobre compatibilidad físico-química, pertenencia a series discretas estándar y si aplica regla cautiva OEM.
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


# =====================================================================
# MOTOR DE INVARIANTES DETERMINISTAS (SIN PROMPT-HARDCODING)
# =====================================================================
UNIDADES_ESTANDAR_ERP = {
    "MM", "MM2", "CM", "M", "PULG", "AWG", "KV", "VAC", "VDC", "AMP",
    "HP", "RPM", "HZ", "IP", "LB", "N/P", "OEM", "CLASE", "TIPO",
    "GRADO", "SERIE", "PASO", "SCH", "UND", "KIT", "JGO", "ROLLO",
    "GLN", "PZAS", "PARTE", "TRAMO",
}


def _parse_valor_numerico(texto: str) -> float | None:
  """Extrae el valor escalar numérico de magnitudes o fracciones (ej. '1-1/2', '3.5', '12 AWG')."""
  if not texto:
    return None
  s = limpiar_tildes(str(texto)).strip().upper()
  m_mix = re.search(r"\b(\d+)\s*[-\s]\s*(\d+)\s*/\s*(\d+)\b", s)
  if m_mix:
    den = float(m_mix.group(3))
    return float(m_mix.group(1)) + float(m_mix.group(2)) / den if den else None
  m_frac = re.search(r"\b(\d+)\s*/\s*(\d+)\b", s)
  if m_frac:
    den = float(m_frac.group(2))
    return float(m_frac.group(1)) / den if den else None
  if re.search(r"\b\d+(?:\.\d+)?\s*-\s*\d+(?:\.\d+)?\b", s):
    return None
  m_num = re.search(r"(\d+(?:\.\d+)?)", s)
  return float(m_num.group(1)) if m_num else None


def _raiz_compartida(palabra: str, palabras_base: list[str]) -> bool:
  """Detecta si una palabra repite raíz de 4 letras o prefijo de 5 letras en palabras_base."""
  if len(palabra) < 4:
    return False
  r4 = palabra[:4]
  r5 = palabra[:5] if len(palabra) >= 5 else ""
  for pb in palabras_base:
    if len(pb) >= 4 and pb[:4] == r4:
      return True
    if r5 and len(pb) >= 5 and r5 in pb:
      return True
  return False


def _aplicar_invariante_serie_discreta(
    resultado_json: dict, interacciones: list
) -> None:
  """Verifica matemáticamente si alguna magnitud evaluada viola su serie discreta normativa."""
  if resultado_json.get("filtro_intencion", {}).get("intencion_insuficiente"):
    return
  if resultado_json.get("control_alcance", {}).get("es_multiple"):
    return
  if (
      resultado_json.get("control_marca_oem", {}).get("clasificacion_iso")
      == "TIPO_2_3_PIEZA_CAUTIVA_OEM"
  ):
    return
  cruce = resultado_json.get("cruce_catalogo_csv", {})
  if cruce.get("estado_cruce") == "DUPLICADO_EXACTO_DETECTADO" or any(
      i.get("tipo") == "ATAJO_CATALOGO_ACEPTADO" for i in interacciones
  ):
    return

  validacion = resultado_json.setdefault("validacion_mercado", {})
  if validacion.get("viabilidad_comercial") == "INCOMPATIBLE":
    return

  salida = resultado_json.setdefault("salida_erp", {})
  auditorias = validacion.get("auditoria_magnitudes") or []
  alerta_upper = limpiar_tildes(str(validacion.get("mensaje_alerta", ""))).upper()
  if re.search(
      r"INCOMPATIBILIDAD\s+FISIC|TEMPERATURA\s+MAXIMA|TEMPERATURA\s+DE\s+OPERACION|NO\s+ES\s+APTO\s+PARA\s+(?:LINEAS\s+DE\s+)?VAPOR|FUSION|ROTURA|EXPLOSION",
      alerta_upper,
  ) or (
      auditorias
      and all(
          "TEMPERAT" in limpiar_tildes(str(a.get("parametro", ""))).upper()
          or "TERMICO" in limpiar_tildes(str(a.get("parametro", ""))).upper()
          for a in auditorias
          if not a.get("existe_en_serie_estandar", True)
      )
      and any(not a.get("existe_en_serie_estandar", True) for a in auditorias)
  ):
    validacion["viabilidad_comercial"] = "INCOMPATIBLE"
    salida["estado"] = "BLOQUEADO_POR_INCOMPATIBILIDAD"
    salida["riesgo_reproceso"] = "ALTO"
    return

  for aud in auditorias:
    param_upper = limpiar_tildes(str(aud.get("parametro", ""))).upper()
    if "TEMPERAT" in param_upper or "TERMICO" in param_upper:
      continue
    val_str = str(aud.get("valor_solicitado", "")).strip()
    val_num = _parse_valor_numerico(val_str)
    serie_pares = []
    for item in aud.get("serie_comercial_vecina") or []:
      num = _parse_valor_numerico(item)
      if num is not None:
        serie_pares.append((num, str(item).strip()))

    fuera_de_serie = not aud.get("existe_en_serie_estandar", True)
    if not fuera_de_serie and val_num is not None and len(serie_pares) >= 2:
      nums = [sn for sn, _ in serie_pares]
      if min(nums) < val_num < max(nums) and all(
          abs(sn - val_num) > 1e-5 for sn in nums
      ):
        fuera_de_serie = True

    if not fuera_de_serie:
      continue

    aud["existe_en_serie_estandar"] = False
    validacion["viabilidad_comercial"] = "MEDIDA_NO_ESTANDAR"
    salida["estado"] = "BLOQUEADO_POR_INCOMPATIBILIDAD"
    salida["riesgo_reproceso"] = "ALTO"

    norma = aud.get("norma_industrial_aplicable") or "estándar industrial"
    if not validacion.get("mensaje_alerta"):
      validacion["mensaje_alerta"] = (
          f"El valor '{val_str}' no existe como escalón discreto bajo la"
          f" norma {norma}."
      )

    if (
        not validacion.get("sugerencias_correccion")
        and val_num is not None
        and serie_pares
    ):
      vecinos = sorted(serie_pares, key=lambda p: (abs(p[0] - val_num), p[0]))[
          :2
      ]
      vecinos.sort(key=lambda p: p[0])
      desc_base = salida.get("descripcion_corta_oracle", "").strip()
      val_num_tok = str(int(val_num)) if val_num.is_integer() else str(val_num)
      val_tiene_letras = bool(re.search(r"[A-Z]", val_str.upper()))
      sugerencias = []
      for sn, v_str in vecinos:
        v_upper = re.sub(r"\s+", " ", v_str.upper()).strip()
        v_scalar = str(int(sn)) if sn.is_integer() else str(sn)
        reemplazo = v_upper if val_tiene_letras else v_scalar
        if desc_base and val_str.upper() in desc_base:
          sugerencias.append(desc_base.replace(val_str.upper(), reemplazo, 1))
        elif desc_base and re.search(rf"\b{re.escape(val_num_tok)}\b", desc_base):
          sugerencias.append(
              re.sub(
                  rf"\b{re.escape(val_num_tok)}\b",
                  v_scalar,
                  desc_base,
                  count=1,
              )
          )
        else:
          sust = (
              resultado_json.get("estructura_taxonomica", {})
              .get("sustantivo_nucleo", "")
              .strip()
              .upper()
          )
          sugerencias.append(f"{sust} {v_upper}".strip())
      validacion["sugerencias_correccion"] = sugerencias

    for idx, paso in enumerate(resultado_json.get("arbol_decisiones") or []):
      if "5" in str(paso.get("etapa", "")) or idx == 4:
        paso["veredicto"] = "BLOQUEO_MEDIDA_NO_ESTANDAR"
        if not paso.get("explicacion_para_audiencia"):
          paso["explicacion_para_audiencia"] = validacion["mensaje_alerta"]
    break


def _aplicar_invariante_conservacion_funcional(
    resultado_json: dict, desc: str
) -> str:
  """Garantiza que sustantivo + modificador funcional primario no se pierdan al traducir jerga."""
  est_tax = resultado_json.get("estructura_taxonomica") or {}
  sust = limpiar_tildes(str(est_tax.get("sustantivo_nucleo", ""))).strip().upper()
  func = (
      limpiar_tildes(str(est_tax.get("modificador_funcional", "")))
      .strip()
      .upper()
  )
  if not func or not desc:
    return desc

  desc_clean = limpiar_tildes(desc).upper()
  raices_desc = {w[:4] for w in re.findall(r"[A-Z]{4,}", desc_clean)}
  palabras_func = [
      w
      for w in re.findall(r"[A-Z]{4,}", func)
      if w.lower() not in STOPWORDS and w not in {"PARA", "TIPO", "USO"}
  ]
  if not palabras_func or any(w[:4] in raices_desc for w in palabras_func):
    return desc

  if sust and desc_clean.startswith(sust):
    resto = desc[len(sust) :].strip()
    return f"{sust} {func} {resto}".strip()
  if sust and re.search(rf"\b{re.escape(sust)}\b", desc_clean):
    return re.sub(
        rf"\b({re.escape(sust)})\b",
        rf"\1 {func}",
        desc,
        count=1,
        flags=re.IGNORECASE,
    )
  return f"{desc} {func}".strip()


def _construir_palabras_ancladas(
    resultado_json: dict, texto_base: str
) -> tuple[set[str], set[str]]:
  """Reúne palabras y raíces de 4 letras explícitamente respaldadas por el usuario o taxonomía base."""
  est_tax = resultado_json.get("estructura_taxonomica") or {}
  jerga = resultado_json.get("traduccion_jerga") or {}
  oem = resultado_json.get("control_marca_oem") or {}

  fuentes = [
      texto_base,
      jerga.get("termino_normalizado", "") if jerga.get("hubo_traduccion") else "",
      est_tax.get("sustantivo_nucleo", ""),
      est_tax.get("modificador_funcional", ""),
      est_tax.get("mecanismo_o_forma", ""),
      oem.get("marca_detectada", ""),
  ]
  texto_anclado = limpiar_tildes(" ".join(str(f) for f in fuentes if f)).upper()
  palabras = set(re.findall(r"[A-Z0-9/-]+", texto_anclado))
  raices = {w[:4] for w in re.findall(r"[A-Z]{4,}", texto_anclado)}
  # Si el usuario escribió abreviatura estándar 'INOX', autorizar expansión 'ACERO'
  if "INOX" in palabras:
    palabras.add("ACERO")
    raices.add("ACER")
  return palabras, raices


def _aplicar_invariante_sanitizacion_morfologica(
    resultado_json: dict, desc: str, texto_base: str, interacciones: list
) -> str:
  """Elimina rellenos ('CUERPO'), cláusulas 'PARA' con raíz repetida y atributos finales no confirmados."""
  if not desc:
    return desc

  # Preservar etiqueta final (REF: ...) durante la limpieza del cuerpo
  ref_tail = ""
  m_ref = re.search(r"(\s*\(REF:\s*[^)]+\))$", desc, flags=re.IGNORECASE)
  body = desc
  if m_ref:
    ref_tail = m_ref.group(1)
    body = desc[: m_ref.start()].strip()

  body = re.sub(r"(?<!^)\bCUERPO\s+(?:DE\s+)?", "", body, flags=re.IGNORECASE)

  palabras_ancladas, raices_ancladas = _construir_palabras_ancladas(
      resultado_json, texto_base
  )
  cruce = resultado_json.get("cruce_catalogo_csv", {})
  sin_coincidencia_inicial = (
      cruce.get("estado_cruce") == "SIN_COINCIDENCIA" and not interacciones
  )

  # Eliminar cláusula 'PARA <objeto>' si repite raíz morfológica previa o si no está anclada
  m_para = re.search(r"\bPARA\s+(.+)$", body)
  if m_para:
    parte_base = body[: m_para.start()].strip()
    clausula = m_para.group(1).strip()
    palabras_base = re.findall(r"[A-Z]{4,}", limpiar_tildes(parte_base).upper())
    tokens_clausula = clausula.split()

    # Separar el sintagma nominal inmediato posterior a PARA hasta la primera cifra/clase
    idx_corte = len(tokens_clausula)
    for i, tok in enumerate(tokens_clausula):
      tok_u = limpiar_tildes(tok).upper()
      if re.search(r"\d", tok_u) or tok_u in {"CLASE", "TIPO", "GRADO", "SERIE"}:
        idx_corte = i
        break

    sintagma_para = tokens_clausula[:idx_corte]
    resto_post_para = tokens_clausula[idx_corte:]
    tiene_raiz_repetida = any(
        _raiz_compartida(limpiar_tildes(t).upper(), palabras_base)
        for t in sintagma_para
    )
    palabras_contenido = [
        limpiar_tildes(t).upper()
        for t in sintagma_para
        if len(limpiar_tildes(t)) >= 2
        and limpiar_tildes(t).lower() not in STOPWORDS
    ]
    todo_no_anclado = sin_coincidencia_inicial and bool(palabras_contenido) and all(
        w not in palabras_ancladas
        and (len(w) < 4 or w[:4] not in raices_ancladas)
        for w in palabras_contenido
    )
    if tiene_raiz_repetida or todo_no_anclado:
      # Conservar términos explícitamente anclados de la cláusula si hubiera alguno no redundante
      if resto_post_para or not parte_base:
        body = f"{parte_base} {' '.join(resto_post_para)}".strip()
      else:
        body = parte_base

  # Recortar tokens huérfanos alucinados al final cuando no hay patrón CSV ni interacciones
  tipo_alcance = resultado_json.get("control_alcance", {}).get(
      "tipo_detectado", ""
  )
  if sin_coincidencia_inicial and tipo_alcance != "SERVICIO":
    tokens = body.split()
    preps = {"DE", "DEL", "EN", "CON", "SIN", "PARA", "AL", "POR", "Y", "X"}
    while tokens:
      ultimo = limpiar_tildes(tokens[-1]).upper()
      if ultimo in preps:
        tokens.pop()
        continue
      if re.search(r"\d", ultimo) or ultimo in UNIDADES_ESTANDAR_ERP:
        break
      esta_anclado = (
          ultimo in palabras_ancladas
          or (len(ultimo) >= 4 and ultimo[:4] in raices_ancladas)
      )
      if esta_anclado:
        break
      tokens.pop()
    if tokens:
      body = " ".join(tokens)

  body = re.sub(r"\s+", " ", body).strip()
  return f"{body}{ref_tail}".strip()


def _ensamblar_firma_semantica_unspsc(resultado_json: dict) -> str:
  """Construye una firma conceptual limpia (sin cifras ni marcas) para RAG UNSPSC."""
  est_tax = resultado_json.get("estructura_taxonomica") or {}
  partes = [
      est_tax.get("sustantivo_nucleo", ""),
      est_tax.get("modificador_funcional", ""),
      est_tax.get("mecanismo_o_forma", ""),
  ]
  bruto = limpiar_tildes(" ".join(str(p) for p in partes if p)).upper()
  # Remover medidas numéricas y unidades acopladas
  bruto = re.sub(
      r"\b\d+(?:[.,/-]\d+)*\s*(?:MM2?|PULG|CM|M|AWG|LB|VAC|VDC|V|HP|A|L/S|GLN|KG|RPM|HZ|W)?\b",
      " ",
      bruto,
  )
  tokens_limpios = []
  raices_vistas = set()
  for tok in re.findall(r"[A-Z]+", bruto):
    if tok.lower() in STOPWORDS and tok not in {"SIN"}:
      continue
    if len(tok) >= 4:
      r4 = tok[:4]
      if r4 in raices_vistas or _raiz_compartida(tok, tokens_limpios):
        continue
      raices_vistas.add(r4)
    elif tok in tokens_limpios:
      continue
    tokens_limpios.append(tok)
  return " ".join(tokens_limpios)


def _aplicar_invariante_subconjunto_catalogo(
    resultado_json: dict, texto_base: str, interacciones: list
) -> None:
  """Garantiza el colapso de variantes del CSV cuando el pedido es subconjunto exacto sin medida."""
  if resultado_json.get("filtro_intencion", {}).get("intencion_insuficiente"):
    return
  if resultado_json.get("control_alcance", {}).get("es_multiple"):
    return
  if (
      resultado_json.get("validacion_mercado", {}).get("viabilidad_comercial")
      != "VALIDO"
  ):
    return
  if any(
      i.get("tipo") in ("ATAJO_CATALOGO_RECHAZADO", "ATAJO_CATALOGO_ACEPTADO")
      for i in interacciones
  ):
    return

  cruce = resultado_json.setdefault("cruce_catalogo_csv", {})
  if cruce.get("candidatos_atajo"):
    return

  q_semanticos, q_hard, q_dims = extraer_tokens_completo(texto_base)
  if not q_semanticos or q_dims or q_hard:
    return

  coincidentes = []
  for reg in CATALOGO_HISTORICO:
    reg_sem = reg.get("semanticos", set())
    if all(
        any(
            ts == tr or (len(ts) >= 4 and len(tr) >= 4 and ts[:4] == tr[:4])
            for tr in reg_sem
        )
        for ts in q_semanticos
    ):
      coincidentes.append(reg)

  if not coincidentes:
    return

  cruce["estado_cruce"] = "CANDIDATO_EN_CATALOGO"
  confirmados = [t.upper() for t in sorted(q_semanticos)]
  candidatos = []
  for reg in coincidentes[:5]:
    desc_reg = reg["descripcion_corta_oracle"]
    tokens_delta = [
        tok
        for tok in desc_reg.split()
        if not any(
            (
                limpiar_tildes(tok).lower() == ts
                or (
                    len(ts) >= 4
                    and limpiar_tildes(tok).lower().startswith(ts[:4])
                )
            )
            for ts in q_semanticos
        )
    ]
    candidatos.append({
        "codigo_erp": reg["codigo_erp"],
        "uom": reg["uom"],
        "descripcion_corta_oracle": desc_reg,
        "atributos_confirmados_usuario": confirmados,
        "delta_no_confirmado": (
            [" ".join(tokens_delta)]
            if tokens_delta
            else ["Especificación estándar de catálogo"]
        ),
    })
  cruce["candidatos_atajo"] = candidatos


def aplicar_invariantes_deterministas(
    resultado_json: dict, texto_base: str, interacciones: list
) -> dict:
  """Orquesta los invariantes matemáticos, morfológicos y semánticos en código Python puro."""
  _aplicar_invariante_subconjunto_catalogo(
      resultado_json, texto_base, interacciones
  )
  _aplicar_invariante_serie_discreta(resultado_json, interacciones)

  salida = resultado_json.setdefault("salida_erp", {})
  cruce = resultado_json.get("cruce_catalogo_csv", {})
  es_existente = (
      cruce.get("estado_cruce") == "DUPLICADO_EXACTO_DETECTADO"
      or salida.get("estado") == "CODIGO_EXISTENTE_EN_CATALOGO"
  )
  es_vago_o_combo = bool(
      resultado_json.get("filtro_intencion", {}).get("intencion_insuficiente")
      or resultado_json.get("control_alcance", {}).get("es_multiple")
  )

  if not es_existente and not es_vago_o_combo:
    desc = limpiar_tildes(
        str(salida.get("descripcion_corta_oracle", ""))
    ).upper()
    desc = _aplicar_invariante_conservacion_funcional(resultado_json, desc)
    desc = _aplicar_invariante_sanitizacion_morfologica(
        resultado_json, desc, texto_base, interacciones
    )
    salida["descripcion_corta_oracle"] = re.sub(r"\s+", " ", desc).strip()

  salida["firma_semantica_unspsc"] = _ensamblar_firma_semantica_unspsc(
      resultado_json
  )
  return resultado_json


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
      resultado_json = aplicar_invariantes_deterministas(
          resultado_json, texto_base, interacciones
      )
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

