from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import revoke as svc

router = APIRouter(tags=["冲正撤销"])


class RevokeBody(BaseModel):
    reversal_key: str
    run_id: str | None = None


@router.get("/revocations")
def get_revocations():
    return svc.list_revocations()


@router.get("/revocations/{revocation_id}/proof")
def get_revocation_proof(revocation_id: int):
    """按撤销事件 id 只读查询恢复证明（冲正键、敞口前后、余额前后）。"""
    return svc.get_revocation_proof(revocation_id)


@router.post("/revocations")
def post_revocation(body: RevokeBody):
    if not body.reversal_key:
        raise HTTPException(400, "缺少冲正流水键")
    return svc.revoke_reversal(body.reversal_key, body.run_id)
