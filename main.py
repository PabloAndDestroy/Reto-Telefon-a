"""
Telephony State Machine MVP

Run from this folder with:
    uvicorn main:app --reload

SQLite is used for a zero-configuration demo. SQLite ignores SELECT FOR UPDATE,
so write paths also use BEGIN IMMEDIATE to serialize competing state changes.
For production row-level locking, use a database such as PostgreSQL.
"""

from enum import Enum
from typing import Generator, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, String, create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)


DATABASE_URL = "sqlite:///./telephony.db"
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
)
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


class AgentStatus(str, Enum):
    IDLE = "IDLE"
    DIALING = "DIALING"
    IN_CALL = "IN_CALL"


class CallLegType(str, Enum):
    AGENT_BRIDGE = "AGENT_BRIDGE"
    INBOUND_CUSTOMER = "INBOUND_CUSTOMER"
    OUTBOUND_CUSTOMER = "OUTBOUND_CUSTOMER"


class CallLegStatus(str, Enum):
    INITIATED = "INITIATED"
    ANSWERED = "ANSWERED"
    COMPLETED = "COMPLETED"


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    bridge_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    status: Mapped[AgentStatus] = mapped_column(SAEnum(AgentStatus), nullable=False)
    call_legs: Mapped[list["CallLeg"]] = relationship(back_populates="agent")


class CallLeg(Base):
    __tablename__ = "call_legs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    type: Mapped[CallLegType] = mapped_column(SAEnum(CallLegType), nullable=False)
    status: Mapped[CallLegStatus] = mapped_column(SAEnum(CallLegStatus), nullable=False)
    sequence_number: Mapped[int] = mapped_column(nullable=False, default=0)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id"), nullable=False, index=True
    )
    agent: Mapped[Agent] = relationship(back_populates="call_legs")


class ProcessedWebhook(Base):
    __tablename__ = "processed_webhooks"

    event_id: Mapped[str] = mapped_column(String, primary_key=True)


Base.metadata.create_all(engine)

app = FastAPI(
    title="Telephony State Machine MVP",
    description=(
        "Simulated CRM dialer. Use the interactive /docs page to start an agent, "
        "place inbound/outbound calls, and replay webhooks."
    ),
    version="1.0.0",
)

# Fake-provider state is deliberately in memory; the database remains the source
# of truth for our application state.
SIMULATED_PROVIDER_COMPLETED_IDS: set[str] = set()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def begin_write_transaction(db: Session) -> None:
    """Serialize SQLite writers; FOR UPDATE below is used on locking databases."""
    if db.get_bind().dialect.name == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))


def refresh_agent_status(db: Session, agent: Agent) -> None:
    """Derive availability from all customer legs, not webhook arrival order."""
    db.flush()
    customer_legs = db.scalars(
        select(CallLeg).where(
            CallLeg.agent_id == agent.id,
            CallLeg.type.in_(
                [CallLegType.INBOUND_CUSTOMER, CallLegType.OUTBOUND_CUSTOMER]
            ),
            CallLeg.status != CallLegStatus.COMPLETED,
        )
    ).all()

    if any(leg.status == CallLegStatus.ANSWERED for leg in customer_legs):
        agent.status = AgentStatus.IN_CALL
    elif customer_legs:
        agent.status = AgentStatus.DIALING
    else:
        agent.status = AgentStatus.IDLE


class StartAgentRequest(BaseModel):
    agent_id: str = Field(..., min_length=1)


class DialRequest(BaseModel):
    agent_id: str = Field(..., min_length=1)
    customer_phone: str = Field(..., min_length=1)


class WebhookEvent(BaseModel):
    event_id: str = Field(..., min_length=1)
    call_id: str = Field(..., min_length=1)
    event_type: Literal["call.answered", "call.completed"]
    sequence_number: int = Field(..., ge=1)


@app.get("/", tags=["operator"])
def operator_help():
    """Point operators to the built-in interface and API documentation."""
    return {
        "name": "Telephony State Machine MVP",
        "message": "Open /ui to manage the simulated dialer.",
        "operator_interface": "/ui",
        "workflow": [
            "POST /agent/start",
            "POST /dial/outbound or /dial/inbound",
            "POST /webhook/event",
        ],
        "admin_tools": [
            "POST /admin/simulated-provider/{call_id}/complete",
            "POST /admin/recovery/sync",
        ],
        "interactive_docs": "/docs",
    }


@app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
def operator_ui():
    """Serve a tiny no-build operator interface directly from the API."""
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Telephony Dialer MVP</title>
  <style>
    :root { color-scheme: light; font-family: system-ui, sans-serif; color: #192231; background: #f2f5f9; }
    body { max-width: 850px; margin: 32px auto; padding: 0 16px; }
    h1 { margin-bottom: 4px; } .hint { color: #536174; margin-top: 0; }
    section { background: white; border: 1px solid #dbe2ea; border-radius: 10px; padding: 18px; margin: 16px 0; }
    h2 { font-size: 1.1rem; margin-top: 0; }
    .row { display: flex; flex-wrap: wrap; gap: 10px; align-items: end; }
    label { display: grid; gap: 5px; flex: 1 1 180px; font-size: .9rem; }
    input, select, button { box-sizing: border-box; font: inherit; padding: 9px 10px; border: 1px solid #bbc6d4; border-radius: 6px; }
    button { background: #1457c5; border-color: #1457c5; color: white; cursor: pointer; }
    button:hover { background: #0d449c; } button.secondary { background: #43536a; border-color: #43536a; }
    #result { white-space: pre-wrap; overflow-wrap: anywhere; background: #111827; color: #e5edf8; padding: 14px; border-radius: 8px; min-height: 48px; }
    .actions { display:flex; flex-wrap:wrap; gap:8px; margin-top: 10px; }
  </style>
</head>
<body>
  <h1>Telephony Dialer</h1>
  <p class="hint">Demo controls for agents and simulated customer calls. No real calls are placed.</p>

  <section>
    <h2>1. Start an agent</h2>
    <div class="row">
      <label>Agent name / ID<input id="agent" value="agent-1" autocomplete="off"></label>
      <button onclick="startAgent()">Start agent</button>
    </div>
  </section>

  <section>
    <h2>2. Simulate a customer call</h2>
    <div class="row">
      <label>Agent ID<input id="dialAgent" value="agent-1" autocomplete="off"></label>
      <label>Customer phone<input id="phone" value="+15555550100" autocomplete="off"></label>
    </div>
    <div class="actions">
      <button onclick="dial('outbound')">Start outbound call</button>
      <button class="secondary" onclick="dial('inbound')">Simulate inbound call</button>
    </div>
  </section>

  <section>
    <h2>3. Simulate provider events</h2>
    <div class="row">
      <label>Call ID<input id="callId" placeholder="Filled after starting a call" autocomplete="off"></label>
      <label>Event
        <select id="eventType">
          <option value="call.answered">Customer answered</option>
          <option value="call.completed">Call completed</option>
        </select>
      </label>
      <label>Sequence number<input id="sequence" type="number" min="1" value="1"></label>
    </div>
    <div class="actions"><button onclick="sendEvent()">Send simulated event</button></div>
  </section>

  <section>
    <h2>4. Admin recovery</h2>
    <p class="hint">Mark the selected call completed at the fake provider, then reconcile state.</p>
    <div class="actions">
      <button class="secondary" onclick="providerComplete()">Mark provider call completed</button>
      <button class="secondary" onclick="recover()">Run recovery</button>
    </div>
  </section>

  <section>
    <h2>Result</h2>
    <pre id="result" role="status" aria-live="polite">Ready. Start an agent to begin.</pre>
  </section>

  <script>
    async function request(path, method, body) {
      const options = { method };
      if (body) {
        options.headers = { "Content-Type": "application/json" };
        options.body = JSON.stringify(body);
      }
      const response = await fetch(path, options);
      const data = await response.json();
      document.getElementById("result").textContent =
        JSON.stringify({ status: response.status, ...data }, null, 2);
      if (!response.ok) throw new Error(data.detail || "Request failed");
      return data;
    }
    async function startAgent() {
      try {
        const agent = document.getElementById("agent").value.trim();
        document.getElementById("dialAgent").value = agent;
        await request("/agent/start", "POST", { agent_id: agent });
      } catch (error) { showError(error); }
    }
    async function dial(direction) {
      try {
        const data = await request("/dial/" + direction, "POST", {
          agent_id: document.getElementById("dialAgent").value.trim(),
          customer_phone: document.getElementById("phone").value.trim()
        });
        document.getElementById("callId").value = data.call_id;
      } catch (error) { showError(error); }
    }
    async function sendEvent() {
      try {
        await request("/webhook/event", "POST", {
          event_id: crypto.randomUUID(),
          call_id: document.getElementById("callId").value.trim(),
          event_type: document.getElementById("eventType").value,
          sequence_number: Number(document.getElementById("sequence").value)
        });
        document.getElementById("sequence").stepUp();
      } catch (error) { showError(error); }
    }
    async function providerComplete() {
      try {
        const callId = encodeURIComponent(document.getElementById("callId").value.trim());
        await request("/admin/simulated-provider/" + callId + "/complete", "POST");
      } catch (error) { showError(error); }
    }
    async function recover() {
      try { await request("/admin/recovery/sync", "POST"); }
      catch (error) { showError(error); }
    }
    function showError(error) {
      const output = document.getElementById("result");
      output.textContent += "\\nError: " + error.message;
    }
  </script>
</body>
</html>"""


@app.post("/agent/start", status_code=status.HTTP_201_CREATED, tags=["agent"])
def start_agent(payload: StartAgentRequest, db: Session = Depends(get_db)):
    bridge_id = str(uuid4())
    agent = Agent(
        id=payload.agent_id,
        bridge_id=bridge_id,
        status=AgentStatus.IDLE,
    )
    bridge_leg = CallLeg(
        id=bridge_id,
        type=CallLegType.AGENT_BRIDGE,
        status=CallLegStatus.ANSWERED,
        sequence_number=0,
        agent_id=payload.agent_id,
    )
    db.add_all([agent, bridge_leg])
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Agent already exists",
        ) from exc

    return {
        "agent_id": agent.id,
        "bridge_id": agent.bridge_id,
        "status": agent.status,
    }


def create_customer_leg(
    payload: DialRequest,
    leg_type: CallLegType,
    db: Session,
):
    # with_for_update() is honored by PostgreSQL. SQLite has no row locks, so
    # begin_write_transaction() uses BEGIN IMMEDIATE to serialize this check/write.
    begin_write_transaction(db)
    agent = db.execute(
        select(Agent)
        .where(Agent.id == payload.agent_id)
        .with_for_update()
    ).scalar_one_or_none()

    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.status != AgentStatus.IDLE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Agent is {agent.status.value}; calls require IDLE status",
        )

    agent.status = AgentStatus.DIALING
    call_leg = CallLeg(
        id=str(uuid4()),
        type=leg_type,
        status=CallLegStatus.INITIATED,
        sequence_number=0,
        agent_id=agent.id,
    )
    db.add(call_leg)
    db.commit()
    return {
        "call_id": call_leg.id,
        "agent_id": agent.id,
        "customer_phone": payload.customer_phone,
        "type": call_leg.type,
        "status": call_leg.status,
    }


@app.post("/dial/outbound", status_code=status.HTTP_201_CREATED, tags=["dialer"])
def dial_outbound(payload: DialRequest, db: Session = Depends(get_db)):
    return create_customer_leg(payload, CallLegType.OUTBOUND_CUSTOMER, db)


@app.post("/dial/inbound", status_code=status.HTTP_201_CREATED, tags=["dialer"])
def simulate_inbound(payload: DialRequest, db: Session = Depends(get_db)):
    """Create a fake incoming customer leg, using the same collision guard."""
    return create_customer_leg(payload, CallLegType.INBOUND_CUSTOMER, db)


@app.post("/webhook/event", status_code=status.HTTP_200_OK, tags=["provider webhook"])
def provider_webhook(event: WebhookEvent, db: Session = Depends(get_db)):
    # Insert first in the same transaction. The primary key makes delivery
    # idempotent; BEGIN IMMEDIATE also serializes concurrent SQLite deliveries.
    begin_write_transaction(db)
    db.add(ProcessedWebhook(event_id=event.event_id))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return {"ok": True, "ignored": "duplicate event_id"}

    call_leg = db.get(CallLeg, event.call_id)
    if call_leg is None:
        db.commit()
        return {"ok": True, "ignored": "unknown call_id"}

    # Sequence numbers are monotonic per call; never let stale events roll state
    # back. The event id remains recorded so duplicate deliveries stay harmless.
    if event.sequence_number <= call_leg.sequence_number:
        db.commit()
        return {"ok": True, "ignored": "out-of-order event"}

    agent = db.execute(
        select(Agent)
        .where(Agent.id == call_leg.agent_id)
        .with_for_update()
    ).scalar_one()
    call_leg.sequence_number = event.sequence_number

    if event.event_type == "call.answered":
        call_leg.status = CallLegStatus.ANSWERED
    else:
        call_leg.status = CallLegStatus.COMPLETED

    # The bridge leg remains separate and persistent. Re-derive agent status
    # from customer legs so completion of an unrelated leg cannot free the agent.
    refresh_agent_status(db, agent)
    db.commit()
    return {
        "ok": True,
        "call_id": call_leg.id,
        "status": call_leg.status,
        "agent_status": agent.status,
    }


@app.post(
    "/admin/simulated-provider/{call_id}/complete",
    tags=["admin / recovery"],
)
def simulate_provider_completion(call_id: str, db: Session = Depends(get_db)):
    """Tell the fake provider that this call completed without delivering a webhook."""
    if db.get(CallLeg, call_id) is None:
        raise HTTPException(status_code=404, detail="Call leg not found")
    SIMULATED_PROVIDER_COMPLETED_IDS.add(call_id)
    return {"ok": True, "provider_status": "COMPLETED", "call_id": call_id}


def sync_state_with_provider() -> int:
    """Reconcile initiated DB legs that the simulated provider says completed."""
    recovered = 0
    with SessionLocal() as db:
        begin_write_transaction(db)
        legs = db.scalars(
            select(CallLeg)
            .where(CallLeg.status == CallLegStatus.INITIATED)
            .with_for_update()
        ).all()
        agents_to_refresh: dict[str, Agent] = {}

        for leg in legs:
            if leg.id not in SIMULATED_PROVIDER_COMPLETED_IDS:
                continue
            leg.status = CallLegStatus.COMPLETED
            agents_to_refresh[leg.agent_id] = db.execute(
                select(Agent)
                .where(Agent.id == leg.agent_id)
                .with_for_update()
            ).scalar_one()
            recovered += 1

        for agent in agents_to_refresh.values():
            refresh_agent_status(db, agent)

        db.commit()
    return recovered


@app.post("/admin/recovery/sync", tags=["admin / recovery"])
def run_recovery():
    """Run the fake-provider reconciliation job on demand."""
    return {"ok": True, "recovered_legs": sync_state_with_provider()}
