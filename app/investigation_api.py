import hmac
import os
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, PositiveInt
from sqlalchemy.orm import Session

from app import database
from app.approvals import RetryAction, approval_details, approve_retry
from app.investigation import investigate
from app.investigation_summary import model_summary
from app.investigation_tools import ServiceTools
from app.models import RetryApproval

bearer = HTTPBearer(auto_error=False)


def require_operator(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    token = os.getenv("OPSPILOT_OPERATOR_TOKEN")
    if not token:
        raise HTTPException(503, "Operator API is not enabled")
    if not credentials or not hmac.compare_digest(credentials.credentials.encode(), token.encode()):
        raise HTTPException(401, "Operator authentication required", headers={"WWW-Authenticate": "Bearer"})


router = APIRouter(prefix="/operator", dependencies=[Depends(require_operator)])


class InvestigationRequest(BaseModel):
    endpoint_id: PositiveInt
    delivery_id: PositiveInt | None = None
    question: str = Field(min_length=1, max_length=500)


@router.post("/investigations")
def investigate_endpoint(request: InvestigationRequest):
    with Session(database.engine) as session, session.begin():
        report = investigate(
            ServiceTools(session), request.endpoint_id, request.question, request.delivery_id
        )
    report["model_summary"] = model_summary(report)
    return report


@router.post("/retry-approvals/{approval_id}/approve", status_code=202)
def approve_manual_retry(approval_id: UUID, action: RetryAction):
    with Session(database.engine) as session, session.begin():
        approval = approve_retry(session, approval_id, action)
        return approval_details(session, approval)


@router.get("/retry-approvals/{approval_id}")
def get_retry_approval(approval_id: UUID):
    with Session(database.engine) as session:
        approval = session.get(RetryApproval, approval_id)
        if approval is None:
            raise HTTPException(404, "Approval not found")
        return approval_details(session, approval)
