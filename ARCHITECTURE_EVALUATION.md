# MVP de máquina de estados de telefonía

## 📌 Resumen ejecutivo
Este MVP implementa una máquina de estados robusta para un marcador de CRM en el que los agentes permanecen en un «puente» persistente mientras atienden llamadas consecutivas de clientes. El objetivo principal de esta aplicación es resolver desafíos críticos de los sistemas distribuidos propios de la integración de telefonía, en particular: condiciones de carrera (colisiones de llamadas simultáneas), idempotencia (webhooks duplicados) y orden de eventos (eventos recibidos fuera de secuencia).

## 🌳 Árbol de decisiones y decisiones clave de ingeniería

### 1. Concurrencia y condiciones de carrera
*   **Decisión:** Usar bloqueos a nivel de fila (`with_for_update`) en el registro `Agent` al crear llamadas nuevas.
*   **Motivo:** Si un agente intenta realizar una llamada saliente exactamente cuando se le asigna una llamada entrante, existe el riesgo de reservarlo dos veces. El bloqueo de la fila del agente garantiza que solo una transacción pueda comprobar que está `IDLE` y cambiar su estado a `DIALING`.
*   **Implementación:** En este MVP se usa `BEGIN IMMEDIATE` con SQLite, pero el diseño permite pasar directamente a los bloqueos nativos por fila de PostgreSQL.

### 2. Idempotencia (webhooks duplicados)
*   **Decisión:** Usar una inserción atómica de clave primaria en una tabla `ProcessedWebhook`.
*   **Motivo:** Los proveedores de telefonía (como Twilio o Plivo) suelen volver a intentar entregar webhooks si hay latencia en la red. Procesar dos veces el mismo evento `call.completed` o `call.answered` puede corromper el estado o liberar incorrectamente a un agente.
*   **Implementación:** El código intenta insertar el `event_id` en la base de datos. Si se produce una violación de restricción única (`IntegrityError`), el sistema identifica el evento como duplicado y lo ignora de forma segura.

### 3. Orden de eventos (eventos recibidos fuera de secuencia)
*   **Decisión:** Usar números de secuencia monotónicos asociados a `CallLeg`.
*   **Motivo:** Debido a las colas asíncronas, un evento `call.completed` podría llegar antes que un evento `call.answered`.
*   **Implementación:** La carga útil del webhook entrante debe incluir un `sequence_number`. Si el número es menor o igual que el número de secuencia almacenado en `CallLeg`, se descarta el evento. Así se garantiza que la máquina de estados solo avance y nunca vuelva a un estado obsoleto.

### 4. Cálculo del estado del agente
*   **Decisión:** Calcular dinámicamente `AgentStatus` a partir de los `CallLeg` activos en lugar de codificar de forma rígida las transiciones de estado.
*   **Motivo:** Las transiciones codificadas de forma rígida (por ejemplo, «cuando termina una llamada, marcar al agente como `IDLE`») son frágiles. ¿Qué ocurre si, por algún motivo, el agente tiene dos líneas simultáneas?
*   **Implementación:** La función `refresh_agent_status` consulta todas las líneas de cliente que no estén completadas. Si alguna está contestada, el agente pasa a `IN_CALL`; si hay alguna línea activa, pasa a `DIALING`; de lo contrario, pasa a `IDLE`.

---

## 🚧 Limitaciones

1.  **Cuello de botella de SQLite:** SQLite no admite bloqueos reales a nivel de fila. La transacción `BEGIN IMMEDIATE` bloquea toda la base de datos durante las escrituras. Esto funciona para un MVP, pero no permite soportar una carga de producción concurrente.
2.  **Sin proceso de limpieza de llamadas abandonadas (TTL):** Si se inicia una llamada, pero el proveedor de telefonía no envía ningún webhook, el `CallLeg` permanece en `INITIATED` indefinidamente y el agente queda atascado en `DIALING`.
3.  **Estado síncrono:** La interfaz del agente no tiene forma de saber cuándo se conecta una llamada entrante sin consultar periódicamente el backend. No hay una capa de WebSocket ni de Server-Sent Events (SSE) que envíe actualizaciones de estado a la interfaz.

---

## 🎮 Cómo funciona el simulador

El simulador incluye una interfaz integrada que FastAPI sirve directamente en `/ui`.

1.  **Iniciar agente (inicio de sesión):** Crea el `CallLeg` persistente `AGENT_BRIDGE` y marca al agente como `IDLE`.
2.  **Marcación:** Al hacer clic en «Outbound» o «Inbound», se comprueba la disponibilidad del agente. Si está `IDLE`, se crea un `CallLeg` (`OUTBOUND_CUSTOMER` o `INBOUND_CUSTOMER`) y el agente pasa a `DIALING`.
3.  **Webhooks:** La interfaz genera automáticamente UUID para los eventos y lleva el control de sus números de secuencia. Se puede seleccionar un tipo de evento (`call.answered`, `call.completed`) y hacer clic en «Send Webhook». Al repetir esta acción con el mismo ID de evento, se demuestra la protección contra la idempotencia.
4.  **Sincronización de recuperación:** Ejecuta la función de sincronización en segundo plano para conciliar las llamadas que el proveedor simulado considera «completadas», pero que la base de datos aún registra como «iniciadas».

---

## 🔍 Evaluación y próximos pasos

### ¿Qué funciona bien?
*   **Integridad del estado:** La lógica de idempotencia, orden de eventos y bloqueo de filas está bien. Los modelos de datos principales separan correctamente el puente persistente del agente de las llamadas transitorias de los clientes.
*   **Arquitectura limpia:** La lógica de la aplicación dentro de las rutas de FastAPI es ligera y delega la gestión del estado en las transacciones de la base de datos.

### Oportunidades de mejora
*   **Motor de base de datos:** Cambiar SQLite por PostgreSQL. Esto reemplazaría el bloqueo global de la base de datos por bloqueos por fila de alto rendimiento.
*   **Limpieza de llamadas huérfanas:** Implementar un proceso en segundo plano (por ejemplo, Celery, ARQ o un bucle sencillo de Cron) que limpie automáticamente las llamadas «atascadas» cuyo estado haya permanecido en `INITIATED` más allá de un límite de tiempo específico (por ejemplo, 5 minutos).

### Evolución futura de la aplicación
Para convertir este MVP en un sistema listo para empresas, los próximos pasos serían:
1.  **Migración de base de datos:** Usar PostgreSQL con SQLAlchemy y Alembic para las migraciones.
2.  **Interfaz basada en eventos:** Integrar WebSockets de `asyncio` para transmitir cambios de estado en tiempo real al panel del agente.
3.  **Integración real de telefonía:** Conectar directamente los endpoints de webhook a un proveedor de CPaaS como Twilio, SignalWire o Plivo, y adaptar sus formatos de eventos específicos al modelo genérico `WebhookEvent` utilizado aquí.
