# Reto de Ingeniería PBG

Tiempo del reto principal: 60 minutos.

El uso de herramientas de inteligencia artificial está altamente recomendado. Puedes utilizar Claude Code, ChatGPT, Codex, Cursor, Gemini u otras herramientas.

No buscamos un producto terminado. Prioriza, construye una parte funcional y prepárate para explicar tus decisiones.

Entrega requerida:
1. URL del repositorio.
2. Demo desplegado si aplica.
3. Video de máximo 2 minutos.
4. Respuestas escritas solicitadas.
5. Respuesta al reto universal de screen sharing.

No utilices datos reales de clientes. Todos los datos incluidos en este paquete son sintéticos.

## Reto: Ingeniería de Telefonía y Marcador

Construye la parte que consideres más crítica de un sistema de telefonía donde una línea del agente puede mantenerse activa mientras diferentes clientes entran y salen.

Los webhooks pueden llegar duplicados o fuera de orden. El proceso puede reiniciarse. Puede entrar una llamada inbound durante una outbound.

Demuestra cómo modelarías estado, idempotencia, recuperación y separación entre la identidad de la línea del agente y la del cliente.

No necesitas construir un dialer completo.

## Ejecutar el MVP de telefonía

Desde esta carpeta, instala las dependencias y arranca la API:

```powershell
python -m pip install -r requirements.txt
python -m uvicorn main:app --reload
```

Abre `http://127.0.0.1:8000/ui` para usar la interfaz de operador, o
`http://127.0.0.1:8000/docs` para la interfaz interactiva de FastAPI.
La base SQLite (`telephony.db`) se crea automáticamente. SQLite no implementa
bloqueos `SELECT FOR UPDATE`; el MVP usa `BEGIN IMMEDIATE` para serializar
escrituras en SQLite y conserva `with_for_update()` para bases con locks por fila.
