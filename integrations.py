"""
integrations.py - Router para manejar integraciones de terceros.

Maneja el flujo de OAuth 2.0 con Google para Gmail, incluyendo:
1. Intercambio de código de autorización por access_token y refresh_token.
2. Encriptación del refresh_token usando Fernet.
3. Almacenamiento seguro en la tabla user_integrations de Supabase.
"""

import os
import time
import requests
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, status
from cryptography.fernet import Fernet
from auth import verify_supabase_jwt
from supabase import create_client, Client

router = APIRouter(
    prefix="/api/v1/auth/google",
    tags=["Integrations"],
)

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "")
FERNET_KEY = os.getenv("FERNET_KEY", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

# Inicializar cliente de Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Instancia de Fernet para cifrar y descifrar
if FERNET_KEY:
    try:
        fernet = Fernet(FERNET_KEY.encode('utf-8'))
    except Exception as e:
        print(f"Error inicializando Fernet: {e}")
        fernet = None
else:
    fernet = None


class GoogleAuthCode(BaseModel):
    code: str

class EmailPayload(BaseModel):
    lead_id: str
    subject: str
    body: str

class GmailService:
    @staticmethod
    def refresh_token(encrypted_refresh_token: str) -> dict:
        """Refresca el access_token usando el refresh_token encriptado."""
        if not fernet:
            raise ValueError("Fernet no configurado.")
        
        refresh_token = fernet.decrypt(encrypted_refresh_token.encode()).decode()
        
        token_url = "https://oauth2.googleapis.com/token"
        data = {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        
        response = requests.post(token_url, data=data)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def send_email(access_token: str, to: str, subject: str, body: str):
        """Envía un correo usando la API de Gmail."""
        import base64
        from email.mime.text import MIMEText
        
        message = MIMEText(body)
        message['to'] = to
        message['subject'] = subject
        
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        
        url = "https://www.googleapis.com/gmail/v1/users/me/messages/send"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        
        response = requests.post(url, headers=headers, json={"raw": raw_message})
        response.raise_for_status()
        return response.json()

@router.get("/status", summary="Verificar estado de conexión con Google")
async def get_google_status(
    user_id: str = Depends(verify_supabase_jwt)
):
    try:
        res = supabase.table("user_integrations").select("id").eq("user_id", user_id).eq("provider", "google").execute()
        is_connected = len(res.data) > 0
        return {"is_connected": is_connected}
    except Exception as e:
        print(f"Error verificando estado de Google: {e}")
        return {"is_connected": False}

@router.post("/callback", summary="Intercambio de código OAuth de Google")
async def google_oauth_callback(
    payload: GoogleAuthCode,
    user_id: str = Depends(verify_supabase_jwt)
):
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET or not GOOGLE_REDIRECT_URI:
        raise HTTPException(status_code=500, detail="Credenciales de Google no configuradas en el backend.")
    if not fernet:
        raise HTTPException(status_code=500, detail="FERNET_KEY no está configurado o es inválido.")

    # Intercambio de código
    token_url = "https://oauth2.googleapis.com/token"
    data = {
        "code": payload.code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code",
    }

    try:
        response = requests.post(token_url, data=data, timeout=10)
        response.raise_for_status()
        tokens = response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error en Google token exchange: {e}")
        raise HTTPException(status_code=400, detail=f"Error al intercambiar el código con Google.")

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    expires_in = tokens.get("expires_in", 3599)
    
    if not access_token:
        raise HTTPException(status_code=400, detail="No se recibió un access_token de Google.")

    # expires_at como Unix timestamp (int) — el campo en Supabase es BIGINT
    expires_at: int = int(time.time()) + int(expires_in)
    
    encrypted_refresh_token = None
    if refresh_token:
        encrypted_refresh_token = fernet.encrypt(refresh_token.encode('utf-8')).decode('utf-8')

    integration_data = {
        "user_id": user_id,
        "provider": "google",
        "access_token": access_token,
        "expires_at": expires_at,          # int Unix timestamp
    }
    
    if encrypted_refresh_token:
        integration_data["refresh_token"] = encrypted_refresh_token

    try:
        existing = supabase.table("user_integrations").select("*").eq("user_id", user_id).eq("provider", "google").execute()
        
        if existing.data and len(existing.data) > 0:
            if not refresh_token and existing.data[0].get("refresh_token"):
                integration_data["refresh_token"] = existing.data[0]["refresh_token"]
            supabase.table("user_integrations").update(integration_data).eq("id", existing.data[0]["id"]).execute()
        else:
            supabase.table("user_integrations").insert(integration_data).execute()

        return {"status": "success", "message": "Gmail conectado correctamente."}
    except Exception as e:
        print(f"Error persistiendo integración: {e}")
        raise HTTPException(status_code=500, detail="Error al guardar la integración.")

@router.post("/send-email", summary="Enviar correo vía Gmail")
async def send_gmail_email(
    payload: EmailPayload,
    user_id: str = Depends(verify_supabase_jwt)
):
    # 1. Obtener tokens de la DB
    res = supabase.table("user_integrations").select("*").eq("user_id", user_id).eq("provider", "google").execute()
    if not res.data:
        raise HTTPException(status_code=400, detail="Gmail no está conectado.")
    
    integration = res.data[0]
    access_token = integration["access_token"]
    refresh_token_enc = integration["refresh_token"]
    expires_at_ts: int = integration["expires_at"]   # Unix timestamp (BIGINT)

    # 2. Refrescar token si expiró (o falta menos de 5 min para que expire)
    if expires_at_ts <= int(time.time()) + 300:
        try:
            new_tokens = GmailService.refresh_token(refresh_token_enc)
            access_token = new_tokens["access_token"]
            expires_in = new_tokens.get("expires_in", 3599)
            new_expires_at: int = int(time.time()) + int(expires_in)

            # Actualizar en DB
            supabase.table("user_integrations").update({
                "access_token": access_token,
                "expires_at": new_expires_at,          # int Unix timestamp
            }).eq("id", integration["id"]).execute()
        except Exception as e:
            print(f"Error refrescando token: {e}")
            raise HTTPException(status_code=401, detail="Error al refrescar la sesión de Google. Por favor, reconecta Gmail.")

    # 3. Obtener el email del lead (esto asume que el lead_id existe en la tabla leads)
    # NOTA: Como el usuario dijo que ya agregó la columna email a la tabla leads
    lead_res = supabase.table("leads").select("email").eq("id", payload.lead_id).execute()
    if not lead_res.data or not lead_res.data[0].get("email"):
        raise HTTPException(status_code=404, detail="Email del lead no encontrado.")
    
    to_email = lead_res.data[0]["email"]

    # 4. Enviar email
    try:
        GmailService.send_email(access_token, to_email, payload.subject, payload.body)
        return {"status": "success", "message": f"Email enviado a {to_email}"}
    except Exception as e:
        print(f"Error enviando email: {e}")
        raise HTTPException(status_code=500, detail=f"Error al enviar el correo: {str(e)}")

