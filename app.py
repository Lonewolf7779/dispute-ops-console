import json
import random
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_file

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "disputes.json"
DEFAULT_DATA_FILE = BASE_DIR / "data" / "default_disputes.json"
RELEASE_INTERVAL_SECONDS = 5 * 60

STATUS_PENDING = "PENDING"
STATUS_RESOLVED = "RESOLVED"
STATUS_ESCALATED = "ESCALATED"
STATUS_WAITING_FOR_CLIENT = "WAITING_FOR_CLIENT"
ALL_STATUSES = {
    STATUS_PENDING,
    STATUS_RESOLVED,
    STATUS_ESCALATED,
    STATUS_WAITING_FOR_CLIENT,
    "LOCKED",
}
RESOLUTION_STATUSES = {
    STATUS_RESOLVED,
    STATUS_ESCALATED,
    STATUS_WAITING_FOR_CLIENT,
}

app = Flask(__name__)
disputes_store = []


def read_disputes():
    with DATA_FILE.open("r", encoding="utf-8") as file:
        records = json.load(file)

    if not isinstance(records, list):
        raise ValueError("Invalid dispute database format")

    return records


def write_disputes(records):
    with DATA_FILE.open("w", encoding="utf-8") as file:
        json.dump(records, file, indent=2)


def read_default_disputes():
    with DEFAULT_DATA_FILE.open("r", encoding="utf-8") as file:
        records = json.load(file)

    if not isinstance(records, list):
        raise ValueError("Invalid default dispute database format")

    return records


def iso_now():
    return datetime.utcnow().isoformat(timespec="milliseconds") + "Z"


def ensure_available_at(records, reset_schedule=False):
    pending = [record for record in records if record.get("status") == STATUS_PENDING]
    pending.sort(key=lambda item: parse_iso_time(item.get("timestamp", "")))
    first_available_at = datetime.utcnow()

    for index, record in enumerate(pending):
        if reset_schedule or not record.get("availableAt"):
            record["availableAt"] = (
                first_available_at.timestamp() + index * RELEASE_INTERVAL_SECONDS
            )


def load_store():
    global disputes_store
    disputes_store = read_disputes()
    ensure_available_at(disputes_store)


def persist_store():
    write_disputes(disputes_store)


def validate_dispute_schema(dispute):
    required_string_fields = [
        "disputeId",
        "awbNumber",
        "sellerOrCourierName",
        "stakeholderType",
        "disputeCategory",
        "incidentDate",
        "disputeRemarks",
        "recipientEmail",
        "timestamp",
    ]

    for field in required_string_fields:
        value = dispute.get(field)
        if not isinstance(value, str) or not value.strip():
            return f"Invalid or missing field: {field}"

    required_number_fields = ["claimedAmount", "claimedWeightKg", "billedWeightKg"]
    for field in required_number_fields:
        if not isinstance(dispute.get(field), (int, float)):
            return f"Invalid or missing field: {field}"

    if dispute.get("status") not in ALL_STATUSES:
        return f"Invalid status: {dispute.get('status')}"

    return None


def parse_iso_time(iso_value):
    try:
        return datetime.fromisoformat(iso_value.replace("Z", "+00:00"))
    except Exception:
        return datetime.max


def oldest_pending(records):
    now = datetime.utcnow().timestamp()
    pending = [
        record
        for record in records
        if record.get("status") == STATUS_PENDING
        and float(record.get("availableAt", 0)) <= now
    ]
    # A manually dispatched random dispute is intentionally shown next. This
    # makes the admin "Send random dispute now" action immediately visible at
    # /disputes without permanently changing the ordinary FIFO order.
    pending.sort(key=lambda item: (
        0 if item.get("priorityDispatch") else 1,
        parse_iso_time(item.get("timestamp", "")),
    ))
    return pending[0] if pending else None


def next_release_seconds(records):
    now = datetime.utcnow().timestamp()
    pending = [
        record for record in records if record.get("status") == STATUS_PENDING
    ]
    if not pending:
        return 0
    next_available = min(float(record.get("availableAt", now)) for record in pending)
    return max(0, int(next_available - now))


def unlock_next_pending(records):
    pending = [
        record for record in records if record.get("status") == STATUS_PENDING
    ]
    pending.sort(key=lambda item: parse_iso_time(item.get("timestamp", "")))
    if not pending:
        return None

    pending[0]["availableAt"] = datetime.utcnow().timestamp()
    return pending[0]


def by_status(records, status):
    filtered = [record for record in records if record.get("status") == status]
    filtered.sort(key=lambda item: parse_iso_time(item.get("timestamp", "")))
    return filtered


def summary(records):
    values = {
        "total": len(records),
        "pending": 0,
        "resolved": 0,
        "escalated": 0,
        "waitingForClient": 0,
    }

    for row in records:
        status = row.get("status")
        if status == STATUS_PENDING:
            values["pending"] += 1
        elif status == STATUS_RESOLVED:
            values["resolved"] += 1
        elif status == STATUS_ESCALATED:
            values["escalated"] += 1
        elif status == STATUS_WAITING_FOR_CLIENT:
            values["waitingForClient"] += 1

    return values


@app.get("/api/disputes")
def get_active_dispute():
    current = oldest_pending(disputes_store)
    if not current:
        return jsonify({
            "status": "QUEUE_EMPTY",
            "message": "No pending disputes available at this time. Check back in next release window.",
            "data": None,
            "nextAvailableInSeconds": next_release_seconds(disputes_store),
        })
    return jsonify(current)


@app.get("/disputes")
def get_active_dispute_public_alias():
    return get_active_dispute()


@app.get("/api/disputes/active")
def get_active_dispute_alias():
    return get_active_dispute()


@app.post("/api/disputes/random")
def add_random_dispute():
    records = disputes_store
    existing_ids = {item.get("disputeId") for item in records}
    next_id = random.randint(88000, 98999)
    while f"DISP-{next_id}" in existing_ids:
        next_id = random.randint(88000, 98999)

    templates = [
        {
            "sellerOrCourierName": "Apex Retailers Ltd.",
            "stakeholderType": "Marketplace Seller",
            "disputeCategory": "Weight Discrepancy",
            "claimedAmount": 850,
            "claimedWeightKg": 0.6,
            "billedWeightKg": 2.1,
            "disputeRemarks": "Random test claim: billed weight exceeds the origin scan.",
        },
        {
            "sellerOrCourierName": "BlueDart Cargo",
            "stakeholderType": "3PL Courier Partner",
            "disputeCategory": "SLA Penalty Waiver",
            "claimedAmount": 2400,
            "claimedWeightKg": 1.8,
            "billedWeightKg": 1.8,
            "disputeRemarks": "Random test claim: manifest upload was delayed by a platform outage.",
        },
        {
            "sellerOrCourierName": "Northstar Homeware Pvt Ltd.",
            "stakeholderType": "Marketplace Seller",
            "disputeCategory": "Warehouse Inwarding Shortage",
            "claimedAmount": 5200,
            "claimedWeightKg": 12.0,
            "billedWeightKg": 12.0,
            "disputeRemarks": "Random test claim: received unit count differs from the handover manifest.",
        },
    ]
    template = random.choice(templates)
    now = datetime.utcnow()
    random_dispute = {
        "disputeId": f"DISP-{next_id}",
        "awbNumber": f"AWB-IN-{random.randint(7700000, 7799999)}",
        **template,
        "incidentDate": now.date().isoformat(),
        "recipientEmail": "sntoshprajapati163@gmail.com",
        "status": STATUS_PENDING,
        "resolutionSummary": None,
        "eligibleAmount": None,
        "timestamp": now.isoformat(timespec="milliseconds") + "Z",
    }

    # Only the latest manual dispatch receives the temporary priority flag.
    # Earlier random items rejoin the regular FIFO queue.
    for record in records:
        record.pop("priorityDispatch", None)

    random_dispute["availableAt"] = datetime.utcnow().timestamp()
    random_dispute["priorityDispatch"] = True
    records.append(random_dispute)
    persist_store()
    return jsonify({
        "message": "One random pending dispute was added.",
        "createdDispute": random_dispute,
        "activeDispute": oldest_pending(records),
    }), 201


@app.post("/disputes/random")
def add_random_dispute_public_alias():
    return add_random_dispute()


@app.post("/api/disputes/reset")
def reset_disputes():
    global disputes_store
    records = deepcopy(read_default_disputes())
    for record in records:
        record["status"] = STATUS_PENDING
        record["resolutionSummary"] = None
        record["eligibleAmount"] = None
        record.pop("resolvedAt", None)
    ensure_available_at(records, reset_schedule=True)
    disputes_store = records
    persist_store()
    return jsonify({
        "message": "All disputes were cleared and the default dataset was restored.",
        "summary": summary(records),
    })


@app.post("/disputes/reset")
def reset_disputes_public_alias():
    return reset_disputes()


@app.get("/api/disputes/resolved")
def get_resolved_disputes():
    return jsonify(by_status(disputes_store, STATUS_RESOLVED))


@app.get("/api/disputes/escalated")
def get_escalated_disputes():
    return jsonify(by_status(disputes_store, STATUS_ESCALATED))


@app.get("/api/disputes/waiting-for-client")
def get_waiting_disputes():
    return jsonify(by_status(disputes_store, STATUS_WAITING_FOR_CLIENT))


@app.get("/api/disputes/summary")
def get_summary():
    return jsonify(summary(disputes_store))


@app.put("/api/disputes/<dispute_id>/status")
def resolve_dispute(dispute_id):
    payload = request.get_json(silent=True) or {}
    status = payload.get("status")
    resolution_summary = payload.get("resolutionSummary")
    eligible_amount = payload.get("eligibleAmount")

    if status not in RESOLUTION_STATUSES:
        return jsonify({
            "message": "Invalid resolution status. Use RESOLVED, ESCALATED, or WAITING_FOR_CLIENT."
        }), 400

    if not isinstance(resolution_summary, str) or not resolution_summary.strip():
        return jsonify({"message": "resolutionSummary is required."}), 400

    if eligible_amount is not None and not isinstance(eligible_amount, (int, float)):
        return jsonify({"message": "eligibleAmount must be a number when provided."}), 400

    records = disputes_store
    index = next((i for i, item in enumerate(records) if item.get("disputeId") == dispute_id), -1)

    if index == -1:
        return jsonify({"message": "Dispute not found."}), 404

    if records[index].get("status") != STATUS_PENDING:
        return jsonify({
            "message": "Only PENDING disputes can be resolved from the active queue."
        }), 409

    updated = deepcopy(records[index])
    updated["status"] = status
    updated["resolutionSummary"] = resolution_summary.strip()
    updated["eligibleAmount"] = float(eligible_amount) if eligible_amount is not None else None
    updated["resolvedAt"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    schema_error = validate_dispute_schema(updated)
    if schema_error:
        return jsonify({"message": schema_error}), 400

    records[index] = updated
    next_pending = unlock_next_pending(records)
    persist_store()

    return jsonify({
        "message": "Dispute status updated successfully.",
        "updatedDispute": updated,
        "nextPendingDispute": next_pending,
        "nextAvailableInSeconds": next_release_seconds(records),
    })


@app.post("/api/resolve")
def resolve_dispute_for_bot():
    payload = request.get_json(silent=True) or {}
    dispute_id = payload.get("disputeId")
    resolution_status = payload.get("resolutionStatus")

    if not dispute_id:
        return jsonify({"success": False, "message": "disputeId is required."}), 400

    if resolution_status not in RESOLUTION_STATUSES:
        return jsonify({
            "success": False,
            "message": "resolutionStatus must be RESOLVED, ESCALATED, or WAITING_FOR_CLIENT.",
        }), 400

    resolution_summary = payload.get("resolutionSummary")
    if not isinstance(resolution_summary, str) or not resolution_summary.strip():
        return jsonify({"success": False, "message": "resolutionSummary is required."}), 400

    index = next((i for i, item in enumerate(disputes_store) if item.get("disputeId") == dispute_id), -1)
    if index == -1:
        return jsonify({"success": False, "message": "Dispute not found."}), 404
    if disputes_store[index].get("status") != STATUS_PENDING:
        return jsonify({"success": False, "message": "Only PENDING disputes can be resolved."}), 409

    eligible_amount = payload.get("eligibleAmount")
    if eligible_amount is not None and not isinstance(eligible_amount, (int, float)):
        return jsonify({"success": False, "message": "eligibleAmount must be numeric."}), 400

    disputes_store[index].update({
        "status": resolution_status,
        "eligibleAmount": eligible_amount,
        "resolutionSummary": resolution_summary.strip(),
        "resolvedAt": iso_now(),
    })
    unlock_next_pending(disputes_store)
    persist_store()
    return jsonify({
        "success": True,
        "message": f"Dispute {dispute_id} successfully resolved and updated.",
        "nextAvailableInSeconds": next_release_seconds(disputes_store),
    })


@app.get("/health")
def health():
    records = disputes_store
    issues = []
    for row_index, dispute in enumerate(records):
        error = validate_dispute_schema(dispute)
        if error:
            issues.append({"row": row_index, "error": error})

    return jsonify({
        "healthy": len(issues) == 0,
        "totalRecords": len(records),
        "invalidRows": issues,
    })


@app.get("/")
@app.get("/resolved")
@app.get("/escalated")
@app.get("/waiting-for-client")
@app.get("/dispute-resolution-form")
def frontend_routes():
    return send_file(BASE_DIR / "index.html")


load_store()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000, debug=False)
