"""
main.py — Motor de prospección de leads impulsado por IA.

Punto de entrada de la API. Orquesta el flujo completo:
  1. Recibe criterios de búsqueda del frontend.
  2. Obtiene perfiles de LinkedIn vía Apify.
  3. Evalúa cada perfil con Groq (LLM) usando salida estructurada.
  4. Persiste los resultados calificados en Supabase.

Ejecutar con:
    uvicorn main:app --reload
"""

import asyncio
import json
import re
import os
import urllib.parse
import uuid
import requests
from typing import Any, List

from apify_client import ApifyClient
from tavily import TavilyClient
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from fastapi import Depends, FastAPI, HTTPException, status, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client

from models import SearchParams, LeadEvaluado, SavedQueryCreate, SavedQueryUpdate, SavedQueryResponse
from auth import verify_supabase_jwt
import integrations

# ──────────────────────────────────────────────
# 1. Carga de variables de entorno
# ──────────────────────────────────────────────
load_dotenv()

SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
APIFY_API_TOKEN: str = os.getenv("APIFY_API_TOKEN", "")
TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")
APOLLO_API_KEY: str = os.getenv("APOLLO_API_KEY", "")

# Límite de perfiles para la fase MVP (evita consumir créditos)
MVP_MAX_PROFILES: int = 3

# ──────────────────────────────────────────────
# 2. Configuración del cliente de Apify
# ──────────────────────────────────────────────
apify_client = ApifyClient(APIFY_API_TOKEN)

# ──────────────────────────────────────────────
# 2.5 Configuración del cliente de Tavily (búsqueda de noticias)
# ──────────────────────────────────────────────
tavily_client = TavilyClient(api_key=TAVILY_API_KEY)

# ──────────────────────────────────────────────
# 3. Configuración del LLM (Groq + LangChain)
# ──────────────────────────────────────────────
llm = ChatGroq(
    api_key=GROQ_API_KEY,
    model="meta-llama/llama-4-scout-17b-16e-instruct",  # 30K TPM / 500K TPD en Groq
    temperature=0,          # Determinístico para evaluaciones consistentes
)

# ──────────────────────────────────────────────
# 4. Prompt del evaluador de leads B2B
# ──────────────────────────────────────────────
# NOTA: NO usamos with_structured_output() porque llama-4-scout-17b
# emite "true"/"false" (strings) en las tool calls de Groq, lo que dispara
# un 400 de validación estricta. En su lugar, pedimos JSON en un code block
# y lo parseamos manualmente con un paso de limpieza de booleanos.
EVALUATION_PROMPT = ChatPromptTemplate.from_messages([
    (
        "system",
        """Eres un experto Calificador de Leads B2B con profunda experiencia en inteligencia de ventas.
Tu trabajo es analizar un perfil de LinkedIn y determinar si la persona es un lead calificado.

Estás escribiendo en nombre de la empresa: {mi_empresa}. Usa este nombre de forma natural en el cuerpo del correo para presentarte.

REGLA ESTRICTA DE IDIOMA: DEBES generar TODOS los outputs, incluyendo 'mensaje_generado', 'trigger_noticia' y 'razonamiento_filtro', completamente en español colombiano profesional de negocios. Nunca uses palabras en inglés a menos que sea un término técnico específico o un nombre de marca.

DEBES evaluar con base en estos criterios estrictos:
1. **Coincidencia de Sector**: ¿La industria/sector del lead coincide con el sector objetivo?
2. **Coincidencia de Tamaño de Empresa**: ¿El tamaño de la empresa se alinea con el rango objetivo (tamano_empresa)?
3. **Rol de Tomador de Decisiones**: ¿El rol actual del lead coincide o se relaciona estrechamente con el título de decisor objetivo (cargo_decision)?

Adicionalmente:
- Recibirás noticias/contexto reciente sobre la empresa del lead en el campo {{noticias}}.
- Usa este contexto noticioso para escribir un trigger_noticia altamente contextualizado que referencie eventos reales y verificables.
- Si no se proporcionan noticias concretas, busca cambios de rol, anuncios corporativos, rondas de inversión, expansiones o señales contextuales en el perfil que coincidan con el trigger_noticia o dolor_cliente.
- Si no se encuentra ningún trigger concreto, infiere uno del contexto del perfil (ej: cambio de trabajo reciente, señales de crecimiento de la empresa).

PROCESO DE RAZONAMIENTO (obligatorio):
- ANTES de generar el mensaje, DEBES articular claramente tu decisión en el campo 'razonamiento_filtro'.
- Explica en UNA sola oración concisa POR QUÉ este lead fue marcado como calificado o descalificado, basándote en el poder de toma de decisiones de su cargo y la coincidencia con el pain point de la empresa.

Para el mensaje de conexión:
- Escribe un mensaje altamente personalizado y profesional EN ESPAÑOL COLOMBIANO DE NEGOCIOS.
- Referencia detalles específicos del perfil del lead (nombre, empresa, rol, logros).
- Integra fluidamente el contexto de noticias recientes en la PRIMERA LÍNEA del mensaje_generado para hacerlo ultra-personalizado y oportuno.
- Incorpora de forma natural la propuesta_valor (propuesta de valor) para mostrar relevancia.
- Entrelaza de forma orgánica los casos de éxito/diferenciadores medibles proporcionados y utiliza jerga o keywords de la industria si aplica.
- Al evaluar el contexto de noticias o el perfil, mantente atento a los triggers de compra para determinar la relevancia.
- Mantenlo conciso (3-4 oraciones máximo), cálido y sin parecer una venta.
- El tono debe sentirse como un acercamiento genuino entre pares, no como un pitch frío.

Sé honesto en tu evaluación. Si el lead NO cumple con los criterios, establece es_calificado en false.

DEBES responder con ÚNICAMENTE un bloque de código JSON (```json ... ```) usando EXACTAMENTE este esquema:
{{
  "es_calificado": true o false,
  "razonamiento_filtro": "string",
  "trigger_noticia": "string",
  "mensaje_generado": "string"
}}

REGLAS CRÍTICAS:
- es_calificado DEBE ser un booleano JSON crudo: true o false. NO un string. NO "true" ni "false".
- razonamiento_filtro DEBE completarse ANTES de generar el mensaje. Es obligatorio.
- TODOS los campos de texto (razonamiento_filtro, trigger_noticia, mensaje_generado) DEBEN estar en español colombiano profesional.
- NO agregues ningún texto, explicación o comentario fuera del bloque de código JSON.
- NO agregues campos adicionales más allá de los cuatro listados arriba.""",
    ),
    (
        "human",
        """Analiza el siguiente perfil de LinkedIn contra los criterios de búsqueda.

--- DATOS DEL PERFIL ---
{profile_data}

--- CRITERIOS DE BÚSQUEDA ---
- Sector Objetivo: {sector}
- País Objetivo: {pais}
- Tamaño de Empresa Objetivo: {tamano_empresa}
- Título del Decisor: {cargo_decision}
- Trigger / Noticia Reciente a buscar: {trigger_noticia}
- Triggers de Compra Avanzados: {triggers_compra}
- Casos de Éxito / Diferenciadores: {casos_exito}
- Keywords de Industria: {keywords_industria}
- Pain Point del Cliente: {dolor_cliente}
- Propuesta de Valor: {propuesta_valor}

--- NOTICIAS RECIENTES DE LA EMPRESA (de Tavily) ---
{noticias}

Usa las noticias anteriores para escribir un trigger_noticia altamente contextualizado e integra fluidamente este contexto en la primera línea del mensaje_generado.

Responde con ÚNICAMENTE un bloque de código JSON. Ningún otro texto. Todo en español colombiano profesional.""",
    ),
])

# Cadena completa: Prompt → LLM (sin salida estructurada, parseamos manualmente)
evaluation_chain = EVALUATION_PROMPT | llm

# ──────────────────────────────────────────────
# 5. Configuración del cliente de Supabase
# ──────────────────────────────────────────────
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ──────────────────────────────────────────────
# 6. Instancia de FastAPI y Almacén de Trabajos
# ──────────────────────────────────────────────
app = FastAPI(
    title="Lead Prospecting Engine",
    description="Motor de prospección de leads impulsado por IA — MVP",
    version="0.1.0",
)

# Almacén de trabajos en memoria para la arquitectura de Polling Asíncrono.
# NOTA MVP: En un entorno de producción con múltiples workers (ej: Gunicorn),
# esto debe migrarse a Redis o una tabla en Supabase.
jobs: dict[str, Any] = {}

# ──────────────────────────────────────────────
# 7. Middleware CORS — Orígenes explícitos (producción + desarrollo)
# ──────────────────────────────────────────────
# NOTA: allow_credentials=True es INCOMPATIBLE con allow_origins=["*"].
# Listamos únicamente los orígenes reales del frontend para que el navegador
# envíe cookies/headers de autenticación (Supabase JWT) sin problemas.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://glovar-prospector-front-ten.vercel.app",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(integrations.router)

# ══════════════════════════════════════════════
# FUNCIONES DEL ORQUESTADOR (esqueleto / placeholder)
# ══════════════════════════════════════════════


def _extraer_texto(valor: Any) -> str:
    """
    Desempaqueta de forma segura un valor para obtener una cadena de texto limpia.
    Maneja casos donde el valor sea un diccionario (ej: {'name': 'Danone'}) o nulo.
    """
    if not valor:
        return ""
    if isinstance(valor, str):
        return valor.strip()
    if isinstance(valor, dict):
        # Intentamos obtener las claves más comunes para nombres/títulos en diccionarios
        res = valor.get("name") or valor.get("text") or valor.get("title") or ""
        return str(res).strip()
    return str(valor).strip()


def _extraer_campos_perfil(item: dict[str, Any]) -> dict[str, Any]:
    """
    Extrae solo los campos ligeros y relevantes de un perfil crudo de Apify.

    Esto minimiza el tamaño del contexto enviado al LLM y evita
    enviar datos innecesarios (imágenes, IDs internos, etc.).
    """
    # Experiencia laboral — solo las primeras 3 posiciones
    experiencias_raw = item.get("experiences") or item.get("positions") or []
    experiencias = []
    if isinstance(experiencias_raw, list):
        for exp in experiencias_raw[:3]:
            if isinstance(exp, dict):
                experiencias.append({
                    "titulo": _extraer_texto(exp.get("title")),
                    "empresa": _extraer_texto(exp.get("companyName")) or _extraer_texto(exp.get("company")),
                    "duracion": _extraer_texto(exp.get("duration")) or _extraer_texto(exp.get("timePeriod")),
                    "ubicacion": _extraer_texto(exp.get("location")),
                })

    # Educación — solo las primeras 2
    educacion_raw = item.get("educations") or item.get("education") or []
    educacion = []
    if isinstance(educacion_raw, list):
        for edu in educacion_raw[:2]:
            if isinstance(edu, dict):
                educacion.append({
                    "institucion": _extraer_texto(edu.get("schoolName")) or _extraer_texto(edu.get("school")),
                    "titulo": _extraer_texto(edu.get("degreeName")) or _extraer_texto(edu.get("degree")),
                    "campo": _extraer_texto(edu.get("fieldOfStudy")),
                })

    # Resolución robusta del Nombre del lead
    nombre = _extraer_texto(item.get("fullName"))
    if not nombre:
        first = _extraer_texto(item.get("firstName"))
        last = _extraer_texto(item.get("lastName"))
        concat = f"{first} {last}".strip()
        nombre = concat if concat else _extraer_texto(item.get("name"))
    nombre = nombre or "Desconocido"

    # Extracción robusta de la URL de LinkedIn
    linkedin_url = _extraer_texto(item.get("url")) or _extraer_texto(item.get("linkedInUrl"))
    if not linkedin_url:
        identifier = _extraer_texto(item.get("publicIdentifier"))
        if identifier:
            linkedin_url = f"https://www.linkedin.com/in/{identifier}"
    linkedin_url = linkedin_url or ""

    return {
        "nombre": nombre,
        "titular": _extraer_texto(item.get("headline")),
        "resumen": _extraer_texto(item.get("summary")) or _extraer_texto(item.get("about")),
        "ubicacion": _extraer_texto(item.get("location")) or _extraer_texto(item.get("geoLocation")),
        "sector": _extraer_texto(item.get("industry")),
        "linkedin_url": linkedin_url,
        "experiencia": experiencias,
        "educacion": educacion,
    }


def buscar_noticias_empresa(
    nombre_empresa: str, triggers_compra: str | None = None, pais: str = ""
) -> tuple[str, str | None]:
    """
    Busca noticias recientes o señales de compra sobre una empresa usando Tavily.

    Si el nombre de la empresa no es válido, retorna un mensaje por defecto.
    Si Tavily falla, captura la excepción y retorna un mensaje de error seguro
    para no interrumpir el flujo del orquestador.

    Args:
        nombre_empresa: Nombre de la empresa a buscar.
        triggers_compra: Triggers específicos a buscar (opcional).
        pais: País objetivo para contextualizar la búsqueda genérica.

    Returns:
        Tuple (noticias_texto, primera_url_o_None).
    """
    # Validación: si no hay nombre de empresa útil, cortamos de inmediato
    if not nombre_empresa or nombre_empresa.strip().lower() in ("", "n/a", "desconocido"):
        print("[Tavily] Nombre de empresa no disponible. Saltando búsqueda de noticias.")
        return "Sin noticias recientes relevantes encontradas.", None

    # Orquestación de Query Dinámica
    if triggers_compra and triggers_compra.strip():
        query = f"{nombre_empresa} AND ({triggers_compra.strip()})"
    else:
        ubicacion = pais.strip() if pais and pais.strip() else "Colombia"
        query = f"{nombre_empresa} (news OR expansion OR updates OR {ubicacion})"

    # ── Guardrail: truncar query a 400 chars (límite seguro de Tavily) ──
    query = query[:400]
    print(f"[Tavily] Buscando contexto sobre '{nombre_empresa}' con query ({len(query)} chars): '{query}'...")

    try:
        response = tavily_client.search(
            query=query,
            search_depth="advanced",
            max_results=2,
        )

        # Extraer y concatenar el contenido de los resultados
        resultados = response.get("results", [])
        if not resultados:
            print(f"[Tavily] No se encontraron noticias para '{nombre_empresa}'.")
            return "Sin noticias recientes relevantes encontradas.", None

        noticias = " | ".join(
            r.get("content", "").strip()
            for r in resultados
            if r.get("content", "").strip()
        )

        if not noticias:
            print(f"[Tavily] Resultados vacíos para '{nombre_empresa}'.")
            return "Sin noticias recientes relevantes encontradas.", None

        # Capturamos la URL del primer resultado para trazabilidad
        primera_url: str | None = resultados[0].get("url") or None

        print(f"[Tavily] ✅ Se encontraron {len(resultados)} resultados para '{nombre_empresa}'. URL: {primera_url}")
        return noticias, primera_url

    except Exception as e:
        print(f"[Tavily] ❌ Error al buscar noticias para '{nombre_empresa}': {e}")
        return "Sin noticias recientes relevantes encontradas.", None


def fetch_linkedin_profiles(params: SearchParams) -> list[str]:
    """
    Func 1 — Obtiene perfiles de LinkedIn vía el actor de Apify
    (supreme_coder/linkedin-profile-scraper).

    Construye una URL de búsqueda de LinkedIn a partir de los parámetros,
    ejecuta el actor, y devuelve los perfiles como cadenas JSON minificadas.

    Args:
        params: Criterios de búsqueda definidos por el usuario.

    Returns:
        Lista de cadenas JSON minificadas con los datos relevantes de cada perfil.
        Retorna lista vacía [] si ocurre un error.
    """
    print(f"[Apify] Buscando perfiles en el sector '{params.sector}' en '{params.pais}'...")

    try:
        # Función auxiliar para construir consultas booleanas a partir de valores separados por coma
        def _to_boolean(val: str) -> str:
            parts = [p.strip() for p in val.split(',') if p.strip()]
            if not parts:
                return ""
            # ── Guardrail: máx 3 términos por campo para evitar que LinkedIn
            #    rompa su motor de búsqueda con booleanos demasiado largos ──
            parts = parts[:3]
            if len(parts) == 1:
                # Si hay múltiples palabras pero sin comas, las envolvemos en comillas
                # para hacer una búsqueda exacta si queremos, pero por ahora lo dejamos simple.
                return parts[0]
            return "(" + " OR ".join(parts) + ")"

        # Construimos la URL de búsqueda de LinkedIn People Search usando lógica booleana
        cargo_bool = _to_boolean(params.cargo_decision)
        sector_bool = _to_boolean(params.sector)
        pais_bool = _to_boolean(params.pais)

        query_parts = []
        if cargo_bool: query_parts.append(cargo_bool)
        if sector_bool: query_parts.append(sector_bool)
        if pais_bool: query_parts.append(pais_bool)

        keywords = " AND ".join(query_parts)
        keywords_encoded = urllib.parse.quote(keywords)
        
        search_url = (
            f"https://www.linkedin.com/search/results/people/"
            f"?keywords={keywords_encoded}&origin=GLOBAL_SEARCH_HEADER"
        )
        print(f"[Apify] URL de búsqueda generada: {search_url} (Query Original: {keywords})")

        # Preparamos el input para el actor de Apify
        run_input: dict[str, Any] = {
            "urls": [{"url": search_url}],
            "scrapeCompany": False,    # No necesitamos la página de la empresa
            "maxProfiles": params.limite_perfiles,
        }

        # Ejecutamos el actor y esperamos a que termine
        print(f"[Apify] Ejecutando actor supreme_coder/linkedin-profile-scraper...")
        run = apify_client.actor("supreme_coder/linkedin-profile-scraper").call(
            run_input=run_input,
            timeout_secs=300,  # Timeout de 5 minutos para mayor robustez
        )

        # Obtenemos los items del dataset resultante
        dataset_id = run["defaultDatasetId"]
        print(f"[Apify] Dataset ID: {dataset_id}")

        items = list(
            apify_client.dataset(dataset_id).iterate_items()
        )
        print(f"[Apify] Se obtuvieron {len(items)} perfiles crudos del dataset.")

        if not items:
            print("[Apify] Advertencia: el actor no devolvió ningún perfil.")
            return []

        print(f"[Apify] Procesando la lista completa de {len(items)} perfiles obtenidos.")

        # Extraemos campos relevantes y convertimos a JSON minificado
        perfiles_json: list[str] = []
        for item in items:
            perfil_limpio = _extraer_campos_perfil(item)
            perfil_str = json.dumps(perfil_limpio, ensure_ascii=False, separators=(",", ":"))
            perfiles_json.append(perfil_str)
            print(f"[Apify] Perfil procesado: {perfil_limpio.get('nombre', 'N/A')}")

        print(f"[Apify] Se procesaron {len(perfiles_json)} perfiles exitosamente.")
        return perfiles_json

    except Exception as e:
        # Capturamos errores de timeout, autenticación, red, etc.
        print(f"[Apify] Error al obtener perfiles de LinkedIn: {e}")
        print(f"[Apify] Verifique que APIFY_API_TOKEN sea válido y tenga créditos disponibles.")
        return []


def _limpiar_y_parsear_json(raw_text: str) -> dict[str, Any]:
    """
    Extrae, limpia y parsea el JSON devuelto por el LLM.

    Pasos:
        1. Busca un bloque ```json ... ``` en la respuesta.
        2. Si no lo encuentra, intenta parsear el texto crudo completo.
        3. LIMPIEZA CRÍTICA: reemplaza booleanos envueltos en comillas
           ("true" → true, "false" → false) para corregir la alucinación
           persistente de llama-4-scout-17b.
        4. Parsea con json.loads y retorna el diccionario.

    Args:
        raw_text: Texto crudo de la respuesta del LLM.

    Returns:
        Diccionario con los campos parseados.

    Raises:
        ValueError: Si no se puede extraer ni parsear un JSON válido.
    """
    # Paso 1: Intentar extraer JSON de un code block ```json ... ```
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw_text, re.DOTALL)
    json_string = match.group(1).strip() if match else raw_text.strip()

    # Paso 2: LIMPIEZA CRÍTICA — corregir booleanos como strings
    # El modelo llama-4-scout-17b a veces emite: "es_calificado": "true"
    # Necesitamos convertirlo a: "es_calificado": true
    json_string = json_string.replace('"true"', 'true').replace('"false"', 'false')

    print(f"[Parser] JSON limpio (primeros 300 chars): {json_string[:300]}")

    # Paso 3: Parsear
    try:
        data = json.loads(json_string)
    except json.JSONDecodeError as e:
        print(f"[Parser] Error al parsear JSON: {e}")
        print(f"[Parser] Texto raw completo: {raw_text}")
        raise ValueError(f"No se pudo parsear el JSON del LLM: {e}") from e

    return data


def evaluate_with_langchain(
    profile_data: str,
    params: SearchParams,
    noticias: str = "Sin noticias recientes.",
) -> LeadEvaluado:
    """
    Func 2 — Evalúa un perfil usando Groq + LangChain con parseo manual de JSON.

    Flujo:
        1. Invoca la cadena EVALUATION_PROMPT → ChatGroq (sin salida estructurada).
        2. Extrae el JSON del code block con _limpiar_y_parsear_json().
        3. Valida el diccionario con el modelo Pydantic LeadEvaluado.

    Este enfoque evita el error 400 de Groq causado por la validación estricta
    de tool calls, donde llama-4-scout-17b emite "true" (string) en vez de true (bool).

    Args:
        profile_data: Cadena JSON minificada con los datos del perfil.
        params:       Criterios de búsqueda originales para contexto.
        noticias:     Noticias recientes de la empresa obtenidas vía Tavily.

    Returns:
        Objeto LeadEvaluado con la calificación, trigger y mensaje.
    """
    # Intentamos extraer el nombre del JSON para los logs
    try:
        nombre_perfil = json.loads(profile_data).get("nombre", "Desconocido")
    except (json.JSONDecodeError, AttributeError):
        nombre_perfil = "Desconocido"

    print(f"[LangChain/Groq] Evaluando perfil de '{nombre_perfil}'...")

    try:
        # Invocamos la cadena — devuelve un AIMessage, no un objeto Pydantic
        ai_message = evaluation_chain.invoke({
            "mi_empresa": params.mi_empresa,
            "profile_data": profile_data,
            "sector": params.sector,
            "pais": params.pais,
            "tamano_empresa": params.tamano_empresa,
            "cargo_decision": params.cargo_decision,
            "trigger_noticia": params.trigger_noticia or "No especificado",
            "triggers_compra": params.triggers_compra or "No especificado",
            "casos_exito": params.casos_exito or "No especificado",
            "keywords_industria": params.keywords_industria or "No especificado",
            "dolor_cliente": params.dolor_cliente,
            "propuesta_valor": params.propuesta_valor,
            "noticias": noticias,
        })

        # Extraemos el texto crudo de la respuesta del LLM
        raw_content: str = ai_message.content
        print(f"[LangChain/Groq] Respuesta raw recibida ({len(raw_content)} chars).")

        # Limpiamos y parseamos el JSON manualmente
        datos = _limpiar_y_parsear_json(raw_content)

        # Validamos contra el modelo Pydantic
        resultado = LeadEvaluado(**datos)

        print(f"[LangChain/Groq] ✅ Resultado: calificado={resultado.es_calificado}")
        return resultado

    except Exception as e:
        # Manejo de errores: si el LLM falla o la salida no es parseable
        print(f"[LangChain/Groq] ❌ Error al evaluar perfil de '{nombre_perfil}': {e}")
        print(f"[LangChain/Groq] Retornando lead no calificado como fallback.")

        # Fallback seguro: marcamos como no calificado para no perder el perfil
        return LeadEvaluado(
            es_calificado=False,
            razonamiento_filtro="Error: no se pudo analizar el perfil con el LLM.",
            trigger_noticia="Error: no se pudo analizar el perfil con el LLM.",
            mensaje_generado="",
        )


def _extraer_empresa_actual(profile_raw: dict[str, Any]) -> tuple[str, str]:
    """
    Extrae la empresa y cargo actuales del perfil scrapeado.

    Busca en el campo 'experiencia' (ya normalizado por _extraer_campos_perfil)
    o en los campos de primer nivel como fallback.

    Returns:
        Tupla (empresa, cargo). Cadenas vacías si no se encuentran.
    """
    # Intentar desde la lista de experiencia normalizada
    experiencias = profile_raw.get("experiencia", [])
    if experiencias and isinstance(experiencias, list):
        primera = experiencias[0]  # La más reciente
        if isinstance(primera, dict):
            empresa = primera.get("empresa", "")
            cargo = primera.get("titulo", "")
            if empresa or cargo:
                return _extraer_texto(empresa), _extraer_texto(cargo)

    # Fallback: campos de primer nivel (por si el formato crudo varía)
    empresa = (
        profile_raw.get("empresa")
        or profile_raw.get("companyName")
        or profile_raw.get("company")
    )
    cargo = (
        profile_raw.get("cargo")
        or profile_raw.get("titular")
        or profile_raw.get("headline")
    )
    return _extraer_texto(empresa), _extraer_texto(cargo)


def _parse_name_for_apollo(full_name: str) -> tuple[str, str]:
    """
    Limpia y divide un nombre completo para el payload de Apollo /v1/people/match.

    Pasos:
        1. Toma solo la primera parte antes de la primera coma — esto elimina
           credenciales y sufijos como ", MSc, MBA, CPIM", ", CPSM®", etc.
        2. Limpia caracteres no-ASCII extraños (®, ™, ©…) del fragmento resultante.
        3. Divide en first_name (primera palabra) y last_name (el resto).

    Ejemplos:
        "André Maiochi, MSc, MBA, CPIM"  → ("André",   "Maiochi")
        "Fernando PENTEADO, CPSM®"       → ("Fernando", "PENTEADO")
        "María José Rodríguez"           → ("María",    "José Rodríguez")
        "Zuckerberg"                     → ("Zuckerberg", "")

    Returns:
        Tupla (first_name, last_name). Cadenas vacías si el input es inválido.
    """
    if not full_name or not full_name.strip():
        return "", ""

    # Paso 1: descartar todo lo que venga después de la primera coma
    base = full_name.split(",")[0].strip()

    # Paso 2: eliminar símbolos no-ASCII que no aportan al nombre (®, ™, ©, etc.)
    base = re.sub(r"[^\w\s\-\'ÁáÉéÍíÓóÚúÜüÑñÀàÈèÌìÒòÙùÂâÊêÎîÔôÛûÄäËëÏïÖöÃãÕõÇç]", "", base).strip()

    # Paso 3: dividir en palabras
    parts = base.split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""

    # first_name = primera palabra; last_name = todo el resto unido
    return parts[0], " ".join(parts[1:])


def enrich_lead_with_apollo(nombre: str, empresa: str) -> dict[str, Any]:
    """
    Busca datos de contacto (email, teléfono) en Apollo.io usando /v1/people/match.

    Limpia el nombre completo con _parse_name_for_apollo antes de enviarlo,
    descartando credenciales/sufijos que provocan errores 422 en Apollo.
    Registra explícitamente response.text cuando Apollo responde con 4xx/5xx
    para facilitar el diagnóstico desde el terminal.
    """
    if not APOLLO_API_KEY:
        print("[Apollo] API Key no configurada. Saltando enriquecimiento.")
        return {}

    # ── Limpieza robusta del nombre ──────────────────────────────────────────
    first_name, last_name = _parse_name_for_apollo(nombre)

    if not first_name:
        print(f"[Apollo] ⚠️  Nombre inválido tras limpieza (input='{nombre}'). Saltando.")
        return {}

    print(
        f"[Apollo] Buscando contacto para '{first_name} {last_name}' "
        f"en '{empresa}' (nombre original: '{nombre}')..."
    )

    url = "https://api.apollo.io/v1/people/match"
    # Apollo requiere la API key en el header X-Api-Key, NO en el cuerpo JSON.
    # Enviarla en el body genera: 422 INVALID_API_KEY_LOCATION.
    headers = {
        "Cache-Control": "no-cache",
        "Content-Type": "application/json",
        "X-Api-Key": APOLLO_API_KEY,
    }
    # Payload estricto — solo first_name, last_name y organization_name
    payload: dict[str, Any] = {
        "first_name": first_name,
        "organization_name": empresa,
    }
    # Omitimos last_name si está vacío para evitar falsos positivos en el match
    if last_name:
        payload["last_name"] = last_name

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)

        # ── Manejo explícito de errores HTTP ────────────────────────────────
        if not response.ok:
            print(
                f"[Apollo] ❌ HTTP {response.status_code} para '{first_name} {last_name}'.\n"
                f"[Apollo] Payload enviado : {payload}\n"
                f"[Apollo] Respuesta exacta: {response.text}"
            )
            response.raise_for_status()   # Relanzamos para que el caller lo capture

        result = response.json()
        person = result.get("person") or {}

        email    = person.get("email") or None
        telefono = person.get("sanitized_phone") or person.get("phone") or None

        print(
            f"[Apollo] ✅ Enriquecimiento OK para '{first_name} {last_name}'. "
            f"Email={'<encontrado>' if email else 'no encontrado'}, "
            f"Teléfono={'<encontrado>' if telefono else 'no encontrado'}."
        )
        return {"email": email, "telefono": telefono}

    except requests.exceptions.HTTPError as http_err:
        # Ya logueamos el body arriba; aquí solo registramos la excepción
        print(f"[Apollo] ❌ HTTPError al enriquecer '{nombre}': {http_err}")
        return {}
    except requests.exceptions.Timeout:
        print(f"[Apollo] ⏱️  Timeout al contactar la API para '{nombre}'.")
        return {}
    except Exception as e:
        print(f"[Apollo] ❌ Error inesperado al enriquecer '{nombre}': {e}")
        return {}


def save_to_supabase(
    lead: LeadEvaluado,
    profile_raw: dict[str, Any],
    user_id: str,
) -> dict[str, Any]:
    """
    Func 3 — Persiste el lead evaluado en la tabla 'leads' de Supabase.

    Mapea los datos del objeto LeadEvaluado (evaluación de la IA) y el
    diccionario profile_raw (datos del perfil de LinkedIn) a las columnas
    exactas de la tabla 'leads' en Supabase.

    Columnas mapeadas:
        user_id, nombre_lead, empresa, cargo, linkedin_url,
        trigger_noticia, mensaje_generado, es_calificado.

    Args:
        lead:        Resultado de la evaluación del LLM (Pydantic).
        profile_raw: Diccionario con los datos del perfil de LinkedIn.
        user_id:     UUID del usuario autenticado (extraído del JWT).

    Returns:
        Diccionario con la fila insertada devuelta por Supabase.
    """
    nombre = profile_raw.get("nombre", "Desconocido")
    empresa, cargo = _extraer_empresa_actual(profile_raw)

    print(f"[Supabase] Guardando lead: '{nombre}' ({cargo} en {empresa or 'N/A'}) para user={user_id[:8]}...")

    # Construimos el registro con las columnas exactas de la tabla
    registro: dict[str, Any] = {
        "user_id": user_id,
        "nombre_lead": nombre,
        "empresa": empresa,
        "cargo": cargo,
        "linkedin_url": getattr(lead, "linkedin_url", "") or profile_raw.get("linkedin_url", ""),
        "razonamiento_filtro": lead.razonamiento_filtro,
        "trigger_noticia": lead.trigger_noticia,
        "mensaje_generado": lead.mensaje_generado,
        "es_calificado": lead.es_calificado,
        "email": getattr(lead, "email", None),
        "telefono": getattr(lead, "telefono", None),
        "url_noticia": getattr(lead, "url_noticia", None),
    }

    try:
        response = (
            supabase
            .table("leads")
            .insert(registro)
            .execute()
        )
        print(f"[Supabase] ✅ Lead '{nombre}' guardado exitosamente.")
        return response.data[0] if response.data else registro

    except Exception as e:
        print(f"[Supabase] ❌ Error al guardar lead '{nombre}': {e}")
        print(f"[Supabase] Verifique la conexión y el esquema de la tabla 'leads'.")
        # No relanzamos: el orquestador maneja el fallback
        raise


# ══════════════════════════════════════════════
# HELPER ASÍNCRONO — Procesamiento individual con rate limiting
# ══════════════════════════════════════════════


async def _process_single_lead(
    perfil_json: str,
    params: SearchParams,
    semaphore: asyncio.Semaphore,
    user_id: str,
) -> dict[str, Any] | None:
    """
    Procesa un lead individual de forma asíncrona con protección de rate limit.

    Flujo dentro del semáforo:
        1. Parsear el JSON del perfil.
        2. Buscar noticias de la empresa vía Tavily (asyncio.to_thread).
        3. Evaluar el perfil con Groq/LangChain (asyncio.to_thread).
        4. Persistir en Supabase (asyncio.to_thread).

    Args:
        perfil_json: Cadena JSON minificada del perfil de LinkedIn.
        params:      Criterios de búsqueda originales.
        semaphore:   Semáforo para limitar concurrencia a las APIs externas.
        user_id:     UUID del usuario autenticado (para inyección en Supabase).

    Returns:
        Diccionario con los datos del lead procesado, o None si falla.
    """
    async with semaphore:
        try:
            # Parseamos el JSON para extraer datos del perfil
            perfil_dict: dict[str, Any] = json.loads(perfil_json)
            nombre_perfil = perfil_dict.get("nombre", "Desconocido")

            print(f"[Orquestador] ▶ Iniciando procesamiento de '{nombre_perfil}' (semáforo adquirido)...")

            # ── Paso 1: Buscar noticias recientes de la empresa vía Tavily ──
            empresa_nombre, _ = _extraer_empresa_actual(perfil_dict)
            noticias_extraidas, url_noticia = await asyncio.to_thread(
                buscar_noticias_empresa, empresa_nombre, params.triggers_compra, params.pais
            )

            # ── Paso 2: Evaluar con Groq/LangChain ──
            evaluacion: LeadEvaluado = await asyncio.to_thread(
                evaluate_with_langchain, perfil_json, params, noticias_extraidas
            )

            # Aseguramos que la URL de LinkedIn esté presente en el objeto evaluado
            if hasattr(evaluacion, "linkedin_url"):
                evaluacion.linkedin_url = perfil_dict.get("linkedin_url", "")

            # Inyectamos la URL de la fuente noticiosa en el objeto evaluado
            evaluacion.url_noticia = url_noticia

            # ── Paso 2.5: Enriquecer con Apollo si está calificado ──
            if evaluacion.es_calificado:
                datos_contacto = await asyncio.to_thread(
                    enrich_lead_with_apollo, nombre_perfil, empresa_nombre
                )
                evaluacion.email = datos_contacto.get("email")
                evaluacion.telefono = datos_contacto.get("telefono")

            # Combinar datos del perfil + evaluación de la IA para el frontend
            empresa, cargo = _extraer_empresa_actual(perfil_dict)
            lead_completo: dict[str, Any] = {
                "nombre_lead": nombre_perfil,
                "empresa": empresa,
                "cargo": cargo,
                "linkedin_url": perfil_dict.get("linkedin_url", ""),
                "es_calificado": evaluacion.es_calificado,
                "razonamiento_filtro": evaluacion.razonamiento_filtro,
                "trigger_noticia": evaluacion.trigger_noticia,
                "mensaje_generado": evaluacion.mensaje_generado,
                "email": getattr(evaluacion, "email", None),
                "telefono": getattr(evaluacion, "telefono", None),
                "url_noticia": evaluacion.url_noticia,
            }

            # ── Paso 3: Persistir en Supabase ──
            guardado_ok = False
            lead_id = None
            try:
                supabase_res = await asyncio.to_thread(save_to_supabase, evaluacion, perfil_dict, user_id)
                if supabase_res and "id" in supabase_res:
                    lead_id = str(supabase_res["id"])
                    guardado_ok = True
            except Exception as e:
                print(f"[Orquestador] Advertencia: no se pudo guardar el lead '{nombre_perfil}': {e}")

            lead_completo["id"] = lead_id
            lead_completo["guardado_en_db"] = guardado_ok

            print(f"[Orquestador] ✅ Lead '{nombre_perfil}' procesado (calificado={evaluacion.es_calificado}).")
            return lead_completo

        except Exception as e:
            print(f"[Orquestador] ❌ Error al procesar lead: {e}")
            return None


# ══════════════════════════════════════════════
# ENDPOINT PRINCIPAL — Orquestador de prospección
# ══════════════════════════════════════════════


@app.post(
    "/api/v1/prospect",
    summary="Encolar prospección de leads",
    description="Inicia un trabajo en segundo plano para procesar leads y devuelve un job_id.",
    response_model=dict,
    status_code=status.HTTP_202_ACCEPTED,
)
async def prospect_leads(
    params: SearchParams,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(verify_supabase_jwt),
) -> dict[str, Any]:
    """
    Encola el trabajo de prospección y devuelve el job_id inmediatamente.
    """
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "processing",
        "current_phase": "Iniciando prospección",
        "processed_leads": 0,
        "total_leads": 0,
    }
    background_tasks.add_task(process_prospecting_job, job_id, params, user_id)
    return {"job_id": job_id, "status": "processing"}


async def process_prospecting_job(job_id: str, params: SearchParams, user_id: str):
    """
    Función en segundo plano que procesa los leads.
    """
    print("=" * 60)
    print(f"[Orquestador Job {job_id}] Iniciando prospección para sector='{params.sector}', país='{params.pais}'")
    print("=" * 60)

    try:
        # ── Paso 1: Obtener perfiles de LinkedIn vía Apify (bloqueante → hilo) ──
        jobs[job_id]["current_phase"] = "Extrayendo de LinkedIn"
        perfiles_obtenidos: list[str] = await asyncio.to_thread(
            fetch_linkedin_profiles, params
        )

        if not perfiles_obtenidos:
            # ── Failure path: Apify returned 0 profiles (timeout, bad cookie, or no matches) ──
            # We explicitly set status="error" (NOT "completed") so the frontend polling
            # can detect this gracefully and show a clear user-facing warning.
            print(
                f"[Orquestador Job {job_id}] Apify returned 0 profiles. "
                "Marking job as error (bad cookie or no LinkedIn matches)."
            )
            jobs[job_id] = {
                "status": "error",
                "current_phase": "No se encontraron perfiles o la cookie de LinkedIn expiró.",
                "processed_leads": 0,
                "total_leads": 0,
                "error": "No se encontraron perfiles. Verifica que la cookie de LinkedIn sea válida o amplía los criterios de búsqueda.",
            }
            return

        # ── Handbrake: cortar la lista al límite del usuario para proteger rate limits ──
        perfiles_a_procesar = perfiles_obtenidos[:params.limite_perfiles]
        print(
            f"[Orquestador Job {job_id}] Perfiles obtenidos: {len(perfiles_obtenidos)} → "
            f"Procesando: {len(perfiles_a_procesar)} (límite: {params.limite_perfiles})"
        )

        # ── Actualizar progreso: fase 2 conoce el total de leads a procesar ──
        jobs[job_id]["current_phase"] = "Analizando con IA"
        jobs[job_id]["total_leads"] = len(perfiles_a_procesar)
        jobs[job_id]["processed_leads"] = 0

        # ── Paso 2: Evaluar perfiles concurrentemente con semáforo de rate limit ──
        semaphore = asyncio.Semaphore(3)
        print(f"[Orquestador Job {job_id}] Semáforo inicializado (máx. 3 concurrentes).")

        async def _tracked_lead(perfil_json: str) -> Any:
            """Wrapper que incrementa el contador de progreso al completar cada lead."""
            result = await _process_single_lead(perfil_json, params, semaphore, user_id)
            jobs[job_id]["processed_leads"] = jobs[job_id].get("processed_leads", 0) + 1
            halfway = jobs[job_id]["total_leads"] // 2
            if jobs[job_id]["processed_leads"] >= halfway:
                jobs[job_id]["current_phase"] = "Buscando emails y guardando"
            return result

        tareas = [
            _tracked_lead(perfil_json)
            for perfil_json in perfiles_a_procesar
        ]

        resultados = await asyncio.gather(*tareas, return_exceptions=True)

        # Filtrar resultados válidos (descartar None y excepciones)
        leads_procesados: list[dict[str, Any]] = []
        for resultado in resultados:
            if isinstance(resultado, Exception):
                print(f"[Orquestador Job {job_id}] ❌ Tarea falló con excepción: {resultado}")
            elif resultado is not None:
                leads_procesados.append(resultado)

        # ── Paso 3: Construir respuesta ──
        leads_calificados = [lead for lead in leads_procesados if lead.get("es_calificado")]

        resumen: dict[str, Any] = {
            "total_perfiles_encontrados": len(perfiles_obtenidos),
            "total_evaluados": len(leads_procesados),
            "total_calificados": len(leads_calificados),
            "leads": leads_procesados,
        }

        print("=" * 60)
        print(
            f"[Orquestador Job {job_id}] Prospección finalizada — "
            f"{resumen['total_calificados']}/{resumen['total_evaluados']} leads calificados."
        )
        print("=" * 60)

        jobs[job_id] = {
            "status": "completed",
            "current_phase": "Completado",
            "processed_leads": len(leads_procesados),
            "total_leads": len(perfiles_a_procesar),
            "result": resumen,
        }

    except Exception as e:
        print(f"[Orquestador Job {job_id}] ❌ Error crítico: {e}")
        jobs[job_id] = {
            "status": "failed",
            "current_phase": "Error",
            "processed_leads": jobs[job_id].get("processed_leads", 0),
            "total_leads": jobs[job_id].get("total_leads", 0),
            "error": str(e),
        }


@app.get(
    "/api/v1/prospect/job/{job_id}",
    summary="Obtener estado del trabajo de prospección",
    description="Devuelve el estado del trabajo y los resultados si ya terminó.",
    response_model=dict,
    status_code=status.HTTP_200_OK,
)
async def get_prospect_job_status(
    job_id: str,
    user_id: str = Depends(verify_supabase_jwt),
) -> dict[str, Any]:
    """
    Endpoint de polling para el frontend.
    """
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


# ══════════════════════════════════════════════
# ENDPOINTS CRUD — Consultas Guardadas
# ══════════════════════════════════════════════

@app.get("/api/v1/queries", response_model=List[SavedQueryResponse], summary="Obtener consultas guardadas")
async def get_saved_queries(user_id: str = Depends(verify_supabase_jwt)):
    try:
        response = supabase.table("saved_queries").select("*").eq("user_id", user_id).order("updated_at", desc=True).execute()
        return response.data
    except Exception as e:
        print(f"[Supabase] Error obteniendo consultas guardadas: {e}")
        raise HTTPException(status_code=500, detail="Error obteniendo consultas guardadas")

@app.post("/api/v1/queries", response_model=SavedQueryResponse, summary="Crear nueva consulta guardada")
async def create_saved_query(query: SavedQueryCreate, user_id: str = Depends(verify_supabase_jwt)):
    data = {
        "user_id": user_id,
        "query_name": query.query_name,
        "search_params": query.search_params
    }
    try:
        response = supabase.table("saved_queries").insert(data).execute()
        if not response.data:
            raise HTTPException(status_code=500, detail="Error guardando la consulta")
        return response.data[0]
    except Exception as e:
        print(f"[Supabase] Error creando consulta guardada: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/v1/queries/{query_id}", response_model=SavedQueryResponse, summary="Sobrescribir consulta existente")
async def update_saved_query(query_id: str, query: SavedQueryUpdate, user_id: str = Depends(verify_supabase_jwt)):
    data = {
        "query_name": query.query_name,
        "search_params": query.search_params,
        "updated_at": "now()"
    }
    try:
        response = supabase.table("saved_queries").update(data).eq("id", query_id).eq("user_id", user_id).execute()
        if not response.data:
            raise HTTPException(status_code=404, detail="Consulta no encontrada o sin autorización")
        return response.data[0]
    except Exception as e:
        print(f"[Supabase] Error actualizando consulta guardada: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/v1/queries/{query_id}", summary="Eliminar consulta guardada")
async def delete_saved_query(query_id: str, user_id: str = Depends(verify_supabase_jwt)):
    try:
        response = supabase.table("saved_queries").delete().eq("id", query_id).eq("user_id", user_id).execute()
        if not response.data:
            raise HTTPException(status_code=404, detail="Consulta no encontrada o sin autorización")
        return {"message": "Consulta eliminada exitosamente"}
    except Exception as e:
        print(f"[Supabase] Error eliminando consulta guardada: {e}")
        raise HTTPException(status_code=500, detail=str(e))
