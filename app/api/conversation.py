from datetime import datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from app.agent.contracts import ConversationId, StrictModel
from app.agent.security import CredentialGuard
from app.api.dependencies import get_credential_guard, get_management_identity
from app.contracts.auth import CurrentUser
from app.conversation.store import ConversationStore

router = APIRouter(prefix="/api/intelligence/conversation", tags=["Conversation"])


class CreateConversation(StrictModel):
    pass


class CreatedConversation(StrictModel):
    conversation_id: ConversationId = Field(alias="conversationId")
    created_at: datetime = Field(alias="createdAt")
    last_activity_at: datetime = Field(alias="lastActivityAt")


class CloseConversation(StrictModel):
    conversation_id: ConversationId = Field(alias="conversationId")


class ClosedConversation(StrictModel):
    closed: Literal[True] = True


def get_conversations(request: Request) -> ConversationStore:
    return cast(ConversationStore, request.app.state.conversations)


@router.post("", response_model=CreatedConversation, status_code=201)
async def create_conversation(
    body: CreateConversation,
    identity: Annotated[CurrentUser, Depends(get_management_identity)],
    store: Annotated[ConversationStore, Depends(get_conversations)],
    guard: Annotated[CredentialGuard, Depends(get_credential_guard)],
) -> CreatedConversation:
    guard.check(identity.subject)
    entry = await store.create(identity.subject)
    return CreatedConversation(
        conversationId=entry.conversation_id,
        createdAt=entry.created_at,
        lastActivityAt=entry.last_activity_at,
    )


@router.post("/resume", response_model=CreatedConversation)
async def resume_conversation(
    body: CreateConversation,
    identity: Annotated[CurrentUser, Depends(get_management_identity)],
    store: Annotated[ConversationStore, Depends(get_conversations)],
    guard: Annotated[CredentialGuard, Depends(get_credential_guard)],
) -> CreatedConversation:
    guard.check(identity.subject)
    entry = await store.resume(identity.subject)
    return CreatedConversation(
        conversationId=entry.conversation_id,
        createdAt=entry.created_at,
        lastActivityAt=entry.last_activity_at,
    )


@router.post("/close", response_model=ClosedConversation)
async def close_conversation(
    body: CloseConversation,
    identity: Annotated[CurrentUser, Depends(get_management_identity)],
    store: Annotated[ConversationStore, Depends(get_conversations)],
) -> ClosedConversation:
    await store.close(body.conversation_id, identity.subject)
    return ClosedConversation()
