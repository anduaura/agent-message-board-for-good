from __future__ import annotations

import uuid

import pytest
from sqlmodel import Session, SQLModel, create_engine

import amb.db as db_module
from amb import crypto
from amb.config import get_settings
from amb.models import AgentStatus
from amb.services import agents as agent_svc


@pytest.fixture
def session(tmp_path, monkeypatch):
    """A fresh database per test."""
    url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("AMB_DATABASE_URL", url)
    get_settings.cache_clear()

    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_module, "_engine", engine)
    import amb.models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    get_settings.cache_clear()


@pytest.fixture
def keypair():
    return crypto.generate_keypair()


@pytest.fixture
def make_agent(session):
    def _make(handle=None, scopes=("read:web", "browse:app", "write:board"), **kw):
        priv, pub = crypto.generate_keypair()
        agent = agent_svc.register(
            session,
            handle=handle or f"agent-{uuid.uuid4().hex[:6]}",
            operator_email="op@example.com",
            public_key=pub,
            allowed_scopes=list(scopes),
            **kw,
        )
        agent.status = AgentStatus.ACTIVE
        session.add(agent)
        session.commit()
        session.refresh(agent)
        return agent, priv

    return _make
