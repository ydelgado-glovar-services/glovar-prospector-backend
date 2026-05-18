"""
auth.py — Dependencia de autenticación JWT para Supabase.

Valida el token JWT enviado por el frontend delegando la verificación
al servidor de Supabase Auth mediante el SDK oficial. Extrae el `sub` (user_id)
y lo inyecta en el request context para garantizar aislamiento de datos por tenant.

Uso en endpoints:
    @app.post("/api/v1/prospect")
    async def prospect_leads(
        params: SearchParams,
        user_id: str = Depends(verify_supabase_jwt),
    ):
        ...
"""

import os
from dotenv import load_dotenv
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from supabase import create_client, Client

load_dotenv()

# ──────────────────────────────────────────────
# Configuración del Cliente Supabase para Auth
# ──────────────────────────────────────────────
SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError(
        "SUPABASE_URL o SUPABASE_KEY no están configurados en .env. "
        "Son necesarios para validar las sesiones contra Supabase Auth."
    )

# Inicializamos una instancia independiente para evitar importaciones circulares con main.py
supabase_auth_client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Esquema HTTPBearer — extrae automáticamente "Bearer <token>" del header
_bearer_scheme = HTTPBearer(
    auto_error=True,  # Lanza 403 si no se envía el header; lo convertimos a 401 abajo
    description="Token JWT de Supabase Auth (access_token del frontend)",
)


# ──────────────────────────────────────────────
# Dependencia principal — verify_supabase_jwt
# ──────────────────────────────────────────────
async def verify_supabase_jwt(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> str:
    """
    Dependencia de FastAPI que valida el JWT de Supabase usando el SDK oficial.

    Flujo:
        1. Extrae el token del header `Authorization: Bearer <token>`.
        2. Llama a `supabase.auth.get_user(token)` para que el servidor verifique
           criptográficamente la validez y vigencia del token (soporta rotación de claves/JWKS).
        3. Retorna el UUID del usuario autenticado.

    Returns:
        str: El user_id (UUID) del usuario autenticado.

    Raises:
        HTTPException(401): Si el token es inválido o ha expirado.
    """
    token = credentials.credentials

    try:
        # get_user se comunica de forma segura con el servidor de Supabase o valida localmente
        # con las claves asimétricas más recientes.
        user_response = supabase_auth_client.auth.get_user(token)
        if not user_response or not user_response.user:
            raise ValueError("Respuesta de usuario vacía")

        return user_response.user.id
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token inválido o expirado. Inicia sesión nuevamente. ({str(e)})",
            headers={"WWW-Authenticate": "Bearer"},
        )

