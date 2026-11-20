"""Image execution through its public service: real SQLite/files, fake HTTP only."""

from __future__ import annotations

import base64
import json
import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.db.base import Base
from app.db.models import User, NarrativeSession, Message
from app.narrative import imagegen as rules
from app.services import imagegen_service as service

pytestmark = pytest.mark.integration
PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"


@pytest.fixture
def image_context(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "images.sqlite3"))
    Base.metadata.create_all(engine)
    monkeypatch.setattr(service, "media_dir", lambda: tmp_path / "media")
    with Session(engine) as db:
        user = User(
            username="image-test",
            email="image-test@example.com",
            password_hash="fixture-only",
        )
        db.add(user)
        db.flush()
        session = NarrativeSession(user_id=user.id, title="Image service test")
        db.add(session)
        db.flush()
        message = Message(session_id=session.id, role="assistant", content="正文应保留")
        db.add(message)
        db.commit()
        yield db, session, message, tmp_path / "media"
    engine.dispose()


def transport(monkeypatch, handler):
    client = httpx.Client
    monkeypatch.setattr(
        service.httpx,
        "Client",
        lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
    )


@pytest.mark.parametrize(
    "form",
    ["base64", "url", "data-uri", "chat-markdown", "chat-content", "chat-images"],
)
def test_images_are_requested_saved_and_reloaded(image_context, monkeypatch, form):
    db, session, message, directory = image_context
    encoded = base64.b64encode(PNG).decode("ascii")
    uri = "data:image/png;base64," + encoded
    payloads = {
        "base64": {"data": [{"b64_json": encoded}]},
        "url": {"data": [{"url": "https://cdn.test/image.png"}]},
        "data-uri": {"data": [{"url": uri}]},
        "chat-markdown": {
            "choices": [{"message": {"content": "![x](https://cdn.test/image.png)"}}]
        },
        "chat-content": {
            "choices": [
                {
                    "message": {
                        "content": [{"type": "image_url", "image_url": {"url": uri}}]
                    }
                }
            ]
        },
        "chat-images": {
            "choices": [
                {"message": {"content": "", "images": [{"image_url": {"url": uri}}]}}
            ]
        },
    }
    calls = []

    def handle(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, content=PNG)
        return httpx.Response(200, json=payloads[form])

    transport(monkeypatch, handle)
    protocol = "openai-chat" if form.startswith("chat-") else "openai-images"
    plan = service.ImageGenPlan(
        plugin_name="test",
        spec=rules.parse_config(
            {
                "base_url": "https://gateway.test/v1",
                "model": "image-model",
                "protocol": protocol,
            }
        ),
        prompts=["a misty harbor"],
        api_key="test-key",
    )
    outcome = service.execute_plan(db, session, message, plan)
    assert outcome.error is None
    assert len(outcome.images) == 1
    post = calls[0]
    assert post.method == "POST" and post.headers["Authorization"] == "Bearer test-key"
    assert post.url.path == (
        "/v1/chat/completions"
        if protocol == "openai-chat"
        else "/v1/images/generations"
    )
    body = json.loads(post.content)
    assert body["model"] == "image-model"
    assert (
        body["messages"][0]["content"] if protocol == "openai-chat" else body["prompt"]
    ) == "a misty harbor"
    db.expire_all()
    stored = service.load_images(db.get(Message, message.id))
    assert stored == outcome.images
    assert message.content == "正文应保留"
    assert (directory / stored[0]["url"].rsplit("/", 1)[-1]).read_bytes() == PNG
    assert not list(directory.glob("*.part"))


@pytest.mark.parametrize("failure", ["no-image", "unsafe-svg", "upstream", "timeout"])
def test_image_failure_preserves_text_without_retry(
    image_context, monkeypatch, failure
):
    db, session, message, directory = image_context
    calls = []

    def handle(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("test timeout", request=request)
        if failure == "upstream":
            return httpx.Response(503, json={"error": {"message": "test outage"}})
        data = (
            []
            if failure == "no-image"
            else [{"b64_json": base64.b64encode(b"<svg>unsafe</svg>").decode("ascii")}]
        )
        return httpx.Response(200, json={"data": data})

    transport(monkeypatch, handle)
    plan = service.ImageGenPlan(
        plugin_name="test",
        spec=rules.parse_config(
            {"base_url": "https://gateway.test/v1", "model": "image-model"}
        ),
        prompts=["harbor"],
    )
    outcome = service.execute_plan(db, session, message, plan)
    assert outcome.error and not outcome.images
    assert len(calls) == 1
    assert message.content == "正文应保留" and service.load_images(message) == []
    assert not directory.exists() or not list(directory.iterdir())
