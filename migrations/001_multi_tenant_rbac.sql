-- ============================================================
-- MIGRACIÓN 001: Arquitectura Multi-Tenant con RBAC
-- Motor de Prospección de Leads B2B
-- ============================================================
-- Ejecutar en: Supabase SQL Editor (Dashboard → SQL Editor)
-- Autor:       Arquitectura Spec-Driven
-- Fecha:       2026-05-13
-- ============================================================

-- ──────────────────────────────────────────────
-- 1. ENUM de roles de usuario
-- ──────────────────────────────────────────────
-- Usamos DO $$ para hacerlo idempotente (no falla si ya existe).

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'user_role') THEN
        CREATE TYPE public.user_role AS ENUM ('admin', 'client');
    END IF;
END
$$;


-- ──────────────────────────────────────────────
-- 2. Tabla de perfiles de usuario (user_profiles)
-- ──────────────────────────────────────────────
-- Extiende auth.users con rol y email denormalizado.
-- Se vincula 1:1 con el sistema de autenticación nativo de Supabase.

CREATE TABLE IF NOT EXISTS public.user_profiles (
    id          UUID        PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    email       TEXT        NOT NULL,
    role        user_role   NOT NULL DEFAULT 'client',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Índice para búsquedas por rol (usado en las políticas RLS)
CREATE INDEX IF NOT EXISTS idx_user_profiles_role
    ON public.user_profiles(role);

COMMENT ON TABLE public.user_profiles IS
    'Perfil extendido del usuario. Referencia 1:1 a auth.users. Contiene el rol RBAC.';


-- ──────────────────────────────────────────────
-- 3. Trigger: crear perfil automáticamente al registrarse
-- ──────────────────────────────────────────────
-- Cuando un usuario se registra vía Supabase Auth, se inserta
-- automáticamente un registro en user_profiles con rol 'client'.

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    INSERT INTO public.user_profiles (id, email, role)
    VALUES (
        NEW.id,
        COALESCE(NEW.email, ''),
        'client'
    );
    RETURN NEW;
END;
$$;

-- Eliminamos el trigger si ya existe para recrearlo limpio
DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;

CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW
    EXECUTE FUNCTION public.handle_new_user();


-- ──────────────────────────────────────────────
-- 4. Alterar tabla leads — agregar columna user_id
-- ──────────────────────────────────────────────
-- Vincula cada lead al usuario que ejecutó la prospección.
-- NOT NULL garantiza aislamiento de datos desde el momento de inserción.

-- Paso 4a: Agregar la columna (si no existe)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'leads'
          AND column_name  = 'user_id'
    ) THEN
        -- Agregar columna nullable primero para no romper filas existentes
        ALTER TABLE public.leads
            ADD COLUMN user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE;
    END IF;
END
$$;

-- Paso 4b: Agregar la columna razonamiento_filtro (nuevo campo del modelo)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'leads'
          AND column_name  = 'razonamiento_filtro'
    ) THEN
        ALTER TABLE public.leads
            ADD COLUMN razonamiento_filtro TEXT DEFAULT '';
    END IF;
END
$$;

-- Paso 4c: Backfill — Si hay leads existentes sin user_id,
-- se deben asignar a un usuario admin antes de aplicar NOT NULL.
-- ⚠️  IMPORTANTE: Descomenta y ejecuta MANUALMENTE si tienes datos previos.
--     Reemplaza '<UUID_DEL_ADMIN>' con el UUID real del usuario admin.
--
-- UPDATE public.leads
--     SET user_id = '<UUID_DEL_ADMIN>'
--     WHERE user_id IS NULL;

-- Paso 4d: Aplicar NOT NULL después del backfill
-- ⚠️  Ejecutar SOLO después de que todas las filas tengan user_id asignado.
--     Si tienes filas existentes sin user_id, primero ejecuta el UPDATE anterior.
--
-- ALTER TABLE public.leads
--     ALTER COLUMN user_id SET NOT NULL;

-- Índice para filtrar leads por usuario (optimiza las políticas RLS)
CREATE INDEX IF NOT EXISTS idx_leads_user_id
    ON public.leads(user_id);


-- ══════════════════════════════════════════════
-- 5. ROW LEVEL SECURITY (RLS) — Políticas de acceso
-- ══════════════════════════════════════════════

-- Habilitar RLS en la tabla leads
ALTER TABLE public.leads ENABLE ROW LEVEL SECURITY;

-- Forzar RLS incluso para el dueño de la tabla (seguridad estricta)
ALTER TABLE public.leads FORCE ROW LEVEL SECURITY;

-- Limpiar políticas previas si existen (idempotencia)
DROP POLICY IF EXISTS "client_isolation_policy" ON public.leads;
DROP POLICY IF EXISTS "admin_read_all_policy"   ON public.leads;


-- ──────────────────────────────────────────────
-- Política 1: AISLAMIENTO DE CLIENTE
-- ──────────────────────────────────────────────
-- Un usuario con rol 'client' solo puede:
--   • SELECT sus propios leads (user_id = auth.uid())
--   • INSERT leads que le pertenezcan (user_id = auth.uid())
-- No puede ver, editar, ni eliminar leads de otros usuarios.

CREATE POLICY "client_isolation_policy"
    ON public.leads
    FOR ALL
    USING (
        auth.uid() = user_id
    )
    WITH CHECK (
        auth.uid() = user_id
    );


-- ──────────────────────────────────────────────
-- Política 2: LECTURA TOTAL PARA ADMIN
-- ──────────────────────────────────────────────
-- Un usuario con rol 'admin' en user_profiles puede
-- leer (SELECT) TODOS los leads de TODOS los usuarios.
-- Usa un subquery para verificar el rol del usuario autenticado.

CREATE POLICY "admin_read_all_policy"
    ON public.leads
    FOR SELECT
    USING (
        EXISTS (
            SELECT 1
            FROM public.user_profiles
            WHERE user_profiles.id = auth.uid()
              AND user_profiles.role = 'admin'
        )
    );


-- ──────────────────────────────────────────────
-- 6. RLS en user_profiles (protección adicional)
-- ──────────────────────────────────────────────
-- Cada usuario solo puede leer su propio perfil.
-- Los admins pueden leer todos los perfiles.

ALTER TABLE public.user_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_profiles FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "users_read_own_profile"   ON public.user_profiles;
DROP POLICY IF EXISTS "admins_read_all_profiles" ON public.user_profiles;

CREATE POLICY "users_read_own_profile"
    ON public.user_profiles
    FOR SELECT
    USING (
        auth.uid() = id
    );

CREATE POLICY "admins_read_all_profiles"
    ON public.user_profiles
    FOR SELECT
    USING (
        EXISTS (
            SELECT 1
            FROM public.user_profiles up
            WHERE up.id = auth.uid()
              AND up.role = 'admin'
        )
    );


-- ══════════════════════════════════════════════
-- VERIFICACIÓN — Ejecutar después de la migración
-- ══════════════════════════════════════════════

-- Verificar que el ENUM existe
SELECT typname, enumlabel
FROM pg_type t
JOIN pg_enum e ON t.oid = e.enumtypid
WHERE typname = 'user_role';

-- Verificar que RLS está habilitado
SELECT tablename, rowsecurity
FROM pg_tables
WHERE schemaname = 'public'
  AND tablename IN ('leads', 'user_profiles');

-- Listar políticas activas
SELECT policyname, tablename, cmd, qual
FROM pg_policies
WHERE schemaname = 'public'
  AND tablename IN ('leads', 'user_profiles');
