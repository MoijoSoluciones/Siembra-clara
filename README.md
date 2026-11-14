# Siembra Clara en Vercel

Contenido:
- `main.py`: API de clima con NASA POWER (Vercel la detecta sola).
- `requirements.txt`: dependencias.
- `public/index.html`: la app.
- `static/index.html`: copia de la app para la ruta `/`.

## Publicar sin terminal
1. GitHub → **New repository** → nombre `siembra-clara` → **Create repository**.
2. **uploading an existing file** → arrastre `main.py`, `requirements.txt`, `README.md` y las carpetas `public` y `static` → **Commit changes**.
3. https://vercel.com/new → **Continue with GitHub** → **Import** en `siembra-clara` → **Deploy** (sin cambiar nada).
4. Abra el enlace `https://siembra-clara-xxxx.vercel.app`.

## Revisar
- `/` → la app
- `/api/clima/historico?lat=9.919&lon=-83.86&alt=1435` → datos de NASA POWER

## Actualizar la app
Reemplace `public/index.html` y `static/index.html` en GitHub (**Add file → Upload files**). Vercel vuelve a publicar solo.
