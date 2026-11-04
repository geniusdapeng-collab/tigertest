"""Evidence availability for the scored risk dimension of strict research."""

SOURCE_PATH = "trading_system/tech_chain/risk_evidence.py"
CONTRACT = "tiger.tech-risk-evidence.v1"


def risk_coverage(document_count: int) -> dict:
    return {"contract": CONTRACT, "scorable_documents": document_count,
            "status": "available" if document_count else "insufficient_evidence"}
