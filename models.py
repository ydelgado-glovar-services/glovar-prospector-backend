"""
models.py — Contratos de datos (Pydantic) para el motor de prospección de leads.

Define los esquemas de entrada y salida que validan toda la información
que entra y sale del sistema, garantizando integridad contractual.
"""

from typing import Dict, Any, Optional
from datetime import datetime
import uuid
from pydantic import BaseModel, Field


# ──────────────────────────────────────────────
# Esquema de ENTRADA — Parámetros de búsqueda
# ──────────────────────────────────────────────
class SearchParams(BaseModel):
    """Payload que envía el frontend con los criterios de prospección."""

    mi_empresa: str = Field(
        ...,
        min_length=1,
        description="Nombre de la empresa del remitente (tu empresa)",
    )
    sector: str = Field(
        ...,
        min_length=1,
        description="Industria o sector objetivo (ej: 'Fintech', 'SaaS B2B')",
    )
    pais: str = Field(
        ...,
        min_length=1,
        description="País objetivo para la prospección (ej: 'Colombia', 'México')",
    )
    tamano_empresa: str = Field(
        ...,
        min_length=1,
        description="Rango de tamaño de la empresa (ej: '50-200 empleados')",
    )
    cargo_decision: str = Field(
        ...,
        min_length=1,
        description="Título del decisor que buscamos (ej: 'CTO', 'VP de Ingeniería')",
    )
    trigger_noticia: str = Field(
        default="",
        description="Noticia o evento reciente a buscar (ej: 'Ronda de inversión', 'Expansión regional')",
    )
    dolor_cliente: str = Field(
        ...,
        min_length=1,
        description="Pain point principal del prospecto (ej: 'Migración a la nube')",
    )
    propuesta_valor: str = Field(
        ...,
        min_length=1,
        description="Propuesta de valor que ofrecemos al prospecto",
    )
    triggers_compra: str | None = Field(
        default=None,
        description="Eventos que detonan la necesidad (ej: 'apertura de operaciones, quejas de clientes')",
    )
    casos_exito: str | None = Field(
        default=None,
        description="Diferenciadores medibles (ej: 'reducción de tiempo 60%, entregables en 2 semanas')",
    )
    keywords_industria: str | None = Field(
        default=None,
        description="Jerga específica usada por el cliente (ej: 'cold chain excursion, SLAs')",
    )
    limite_perfiles: int = Field(
        default=30,
        description="Límite máximo de perfiles a obtener en la prospección",
    )
    exclusion_list: list[str] = Field(
        default=[],
        description="[Sec-Driven] Lista O(n) de empresas a excluir (Early Exit guard clause).",
    )


# ──────────────────────────────────────────────
# Esquema de SALIDA — Lead evaluado por la IA
# ──────────────────────────────────────────────
class LeadEvaluado(BaseModel):
    """Resultado del análisis de un perfil por el agente de IA."""

    es_calificado: bool = Field(
        description=(
            "MUST be a strict JSON boolean (true or false). "
            "DO NOT wrap in quotes. DO NOT output the string \"true\" or \"false\". "
            "Output the raw boolean literal only. "
            "true si el perfil encaja con el sector y cargo, false en caso contrario."
        ),
    )
    razonamiento_filtro: str = Field(
        description=(
            "A single, concise sentence explaining exactly WHY this lead was "
            "marked as qualified or disqualified based on their job title "
            "decision-making power and the company's pain point match."
        ),
    )
    trigger_noticia: str = Field(
        description="Resumen de la noticia o evento reciente de la empresa",
    )
    mensaje_generado: str = Field(
        description="El mensaje final personalizado en español colombiano de negocios",
    )
    linkedin_url: str = Field(
        default="",
        description="URL del perfil de LinkedIn del prospecto",
    )
    email: Optional[str] = Field(
        default=None,
        description="Correo electrónico del prospecto obtenido vía Apollo",
    )
    telefono: Optional[str] = Field(
        default=None,
        description="Número de teléfono del prospecto obtenido vía Apollo",
    )
    url_noticia: Optional[str] = Field(
        default=None,
        description="URL de la primera noticia encontrada en Tavily para este lead",
    )

# ──────────────────────────────────────────────
# Esquemas para Consultas Guardadas (Saved Queries)
# ──────────────────────────────────────────────
class SavedQueryCreate(BaseModel):
    query_name: str
    search_params: Dict[str, Any]

class SavedQueryUpdate(BaseModel):
    query_name: str
    search_params: Dict[str, Any]

class SavedQueryResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    query_name: str
    search_params: Dict[str, Any]
    created_at: datetime
    updated_at: datetime
