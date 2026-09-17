import json
import random
import re
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

STATUS_SYNONYMS = {
    "RESOLVED": STATUS_RESOLVED,
    "RESOLVE": STATUS_RESOLVED,
    "RESOLVING": STATUS_RESOLVED,
    "COMPLETED": STATUS_RESOLVED,
    "COMPLETE": STATUS_RESOLVED,
    "APPROVED": STATUS_RESOLVED,
    "APPROVE": STATUS_RESOLVED,
    "SETTLED": STATUS_RESOLVED,
    "SUCCESS": STATUS_RESOLVED,

    "ESCALATED": STATUS_ESCALATED,
    "ESCALATE": STATUS_ESCALATED,
    "ESCALATION": STATUS_ESCALATED,
    "HITL": STATUS_ESCALATED,
    "HUMAN_REVIEW": STATUS_ESCALATED,
    "REJECTED": STATUS_ESCALATED,
    "REJECT": STATUS_ESCALATED,

    "WAITING_FOR_CLIENT": STATUS_WAITING_FOR_CLIENT,
    "WAITINGFORCLIENT": STATUS_WAITING_FOR_CLIENT,
    "WAITING FOR CLIENT": STATUS_WAITING_FOR_CLIENT,
    "WAITING_FOR_SELLER": STATUS_WAITING_FOR_CLIENT,
    "WAITING_FOR_COURIER": STATUS_WAITING_FOR_CLIENT,
    "WAITING": STATUS_WAITING_FOR_CLIENT,
    "PENDING_CLIENT": STATUS_WAITING_FOR_CLIENT,
    "AWAITING_CLIENT": STATUS_WAITING_FOR_CLIENT,
    "ON_HOLD": STATUS_WAITING_FOR_CLIENT,
}

DEFAULT_FALLBACK_RECIPIENT = "evaluator@demo-evaluation.com"
active_evaluator_email = None

app = Flask(__name__)
disputes_store = []


def is_valid_email(email):
    if not isinstance(email, str):
        return False
    email = email.strip()
    return bool(re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", email))


def get_effective_recipient():
    # 1. Header if provided by caller
    header_email = request.headers.get("X-Evaluator-Email")
    if header_email and is_valid_email(header_email):
        return header_email.strip()

    # 2. Query param if provided
    query_email = request.args.get("evaluatorEmail") or request.args.get("evaluator_email")
    if query_email and is_valid_email(query_email):
        return query_email.strip()

    # 3. Active session email stored in memory
    if active_evaluator_email and is_valid_email(active_evaluator_email):
        return active_evaluator_email

    # 4. Default fallback
    return DEFAULT_FALLBACK_RECIPIENT


def format_dispute_for_response(item):
    if item is None:
        return None
    if isinstance(item, list):
        return [format_dispute_for_response(x) for x in item]
    if isinstance(item, dict):
        copied = deepcopy(item)
        copied["recipientEmail"] = get_effective_recipient()
        return copied
    return item


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


def normalize_resolution_status(status_raw):
    if not status_raw:
        return None
    clean = str(status_raw).strip().upper().replace("-", "_")
    if clean in STATUS_SYNONYMS:
        return STATUS_SYNONYMS[clean]
    clean_no_underscore = clean.replace("_", " ")
    if clean_no_underscore in STATUS_SYNONYMS:
        return STATUS_SYNONYMS[clean_no_underscore]
    return None


def sanitize_amount(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    val_str = str(value).strip()
    if not val_str or val_str.lower() in ("null", "none", "n/a", "na", "undefined", "nil", "-"):
        return None
    # Strip currency signs, commas, and formatting while keeping digits, negative sign, and decimal
    cleaned = re.sub(r"[^\d.-]", "", val_str)
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_flexible_json_payload():
    # 1. Standard Flask JSON parsing
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data

    # 2. Form or query parameters
    if request.form:
        return dict(request.form)

    # 3. Handle broken/malformed JSON strings from RPA variable interpolations
    # e.g. "eligibleAmount": , or unquoted empty values
    try:
        raw_bytes = request.get_data()
        if raw_bytes:
            raw_text = raw_bytes.decode("utf-8", errors="ignore").strip()
            if raw_text:
                # Replace empty values before commas or braces with null
                repaired = re.sub(r':\s*,', ': null,', raw_text)
                repaired = re.sub(r':\s*}', ': null}', repaired)
                repaired = re.sub(r':\s*]', ': null]', repaired)
                parsed = json.loads(repaired)
                if isinstance(parsed, dict):
                    return parsed
    except Exception:
        pass

    return {}


def extract_dispute_fields(payload):
    dispute_id = (
        payload.get("disputeId")
        or payload.get("dispute_id")
        or payload.get("disputeID")
        or payload.get("id")
        or payload.get("awbNumber")
        or payload.get("awb_number")
    )
    if dispute_id is not None:
        dispute_id = str(dispute_id).strip()

    status_raw = (
        payload.get("resolutionStatus")
        or payload.get("resolution_status")
        or payload.get("status")
        or payload.get("resolution")
    )
    resolution_status = normalize_resolution_status(status_raw)

    amount_raw = (
        payload.get("eligibleAmount")
        or payload.get("eligible_amount")
        or payload.get("amount")
        or payload.get("approvedAmount")
        or payload.get("approved_amount")
        or payload.get("settlementAmount")
    )
    eligible_amount = sanitize_amount(amount_raw)

    summary_raw = (
        payload.get("resolutionSummary")
        or payload.get("resolution_summary")
        or payload.get("summary")
        or payload.get("remarks")
        or payload.get("notes")
        or payload.get("auditRemarks")
        or payload.get("comment")
    )
    if summary_raw is not None:
        resolution_summary = str(summary_raw).strip()
    else:
        resolution_summary = ""

    return dispute_id, resolution_status, eligible_amount, resolution_summary


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


@app.post("/api/evaluator-email")
def set_evaluator_email():
    global active_evaluator_email
    payload = parse_flexible_json_payload()
    email = (
        payload.get("evaluatorEmail")
        or payload.get("email")
        or payload.get("evaluator_email")
        or payload.get("recipientEmail")
    )
    if not email or not is_valid_email(email):
        return jsonify({
            "success": False,
            "message": "Please enter a valid email address."
        }), 400

    active_evaluator_email = email.strip()
    return jsonify({
        "success": True,
        "message": "Active evaluator email registered successfully.",
        "evaluatorEmail": active_evaluator_email
    })


@app.get("/api/evaluator-email")
def get_evaluator_email_info():
    return jsonify({
        "evaluatorEmail": active_evaluator_email,
        "hasEvaluator": bool(active_evaluator_email and is_valid_email(active_evaluator_email)),
        "effectiveRecipient": get_effective_recipient(),
    })


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
    return jsonify(format_dispute_for_response(current))


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
            "disputeRemarks": "Apparel consignment (dead weight 0.60 kg on merchant scale) was billed as 2.10 kg volumetric weight. Requesting ₹850 excess freight reversal. Weighing slip attached.",
        },
        {
            "sellerOrCourierName": "BlueDart Cargo",
            "stakeholderType": "3PL Courier Partner",
            "disputeCategory": "SLA Penalty Waiver",
            "claimedAmount": 2400,
            "claimedWeightKg": 1.8,
            "billedWeightKg": 1.8,
            "disputeRemarks": "SLA penalty waiver requested. Consignment manifest delayed due to documented cloud network gateway outage during peak manifest window. IT bulletin attached.",
        },
        {
            "sellerOrCourierName": "Northstar Homeware Pvt Ltd.",
            "stakeholderType": "Marketplace Seller",
            "disputeCategory": "Warehouse Inwarding Shortage",
            "claimedAmount": 5200,
            "claimedWeightKg": 12.0,
            "billedWeightKg": 12.0,
            "disputeRemarks": "Inwarding dock receipt confirms 40 units received, but GRN inward count recorded 34 units (shortage of 6 units valued at ₹5,200). Receiving supervisor sign-off attached.",
        },
    ]
    template = random.choice(templates)
    now = datetime.utcnow()
    random_dispute = {
        "disputeId": f"DISP-{next_id}",
        "awbNumber": f"AWB-IN-{random.randint(7700000, 7799999)}",
        **template,
        "incidentDate": now.date().isoformat(),
        "recipientEmail": DEFAULT_FALLBACK_RECIPIENT,
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
        "createdDispute": format_dispute_for_response(random_dispute),
        "activeDispute": format_dispute_for_response(oldest_pending(records)),
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
        "evaluatorEmail": get_effective_recipient()
    })


@app.post("/disputes/reset")
def reset_disputes_public_alias():
    return reset_disputes()


@app.get("/api/disputes/resolved")
def get_resolved_disputes():
    return jsonify(format_dispute_for_response(by_status(disputes_store, STATUS_RESOLVED)))


@app.get("/api/disputes/escalated")
def get_escalated_disputes():
    return jsonify(format_dispute_for_response(by_status(disputes_store, STATUS_ESCALATED)))


@app.get("/api/disputes/waiting-for-client")
def get_waiting_disputes():
    return jsonify(format_dispute_for_response(by_status(disputes_store, STATUS_WAITING_FOR_CLIENT)))


@app.get("/api/disputes/summary")
def get_summary():
    return jsonify(summary(disputes_store))


@app.put("/api/disputes/<dispute_id>/status")
def resolve_dispute(dispute_id):
    payload = parse_flexible_json_payload()
    _, status, eligible_amount, resolution_summary = extract_dispute_fields(payload)

    if not status:
        status_raw = payload.get("status") or payload.get("resolutionStatus")
        return jsonify({
            "message": f"Invalid resolution status '{status_raw}'. Use RESOLVED, ESCALATED, or WAITING_FOR_CLIENT."
        }), 400

    if not resolution_summary:
        resolution_summary = f"Dispute resolved via console."

    records = disputes_store
    index = next((i for i, item in enumerate(records) if item.get("disputeId") == dispute_id or item.get("awbNumber") == dispute_id), -1)

    if index == -1:
        return jsonify({"message": "Dispute not found."}), 404

    if records[index].get("status") != STATUS_PENDING:
        return jsonify({
            "message": "Only PENDING disputes can be resolved from the active queue."
        }), 409

    updated = deepcopy(records[index])
    updated["status"] = status
    updated["resolutionSummary"] = resolution_summary
    updated["eligibleAmount"] = eligible_amount
    updated["resolvedAt"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    schema_error = validate_dispute_schema(updated)
    if schema_error:
        return jsonify({"message": schema_error}), 400

    records[index] = updated
    next_pending = unlock_next_pending(records)
    persist_store()

    return jsonify({
        "message": "Dispute status updated successfully.",
        "updatedDispute": format_dispute_for_response(updated),
        "nextPendingDispute": format_dispute_for_response(next_pending),
        "nextAvailableInSeconds": next_release_seconds(records),
    })


@app.post("/api/resolve")
def resolve_dispute_for_bot():
    payload = parse_flexible_json_payload()
    dispute_id, resolution_status, eligible_amount, resolution_summary = extract_dispute_fields(payload)

    if not dispute_id:
        # If dispute_id is missing, auto-target the current oldest pending dispute in queue
        active = oldest_pending(disputes_store)
        if active:
            dispute_id = active.get("disputeId")
        else:
            return jsonify({
                "success": False,
                "message": "disputeId is required and no active pending dispute was found in queue."
            }), 400

    if not resolution_status:
        # Safe default to RESOLVED
        resolution_status = STATUS_RESOLVED

    if not resolution_summary:
        resolution_summary = f"Dispute processed with status {resolution_status} via automated task."

    index = next((i for i, item in enumerate(disputes_store) if item.get("disputeId") == dispute_id), -1)
    if index == -1:
        # Also check if passed dispute_id was an AWB number
        index = next((i for i, item in enumerate(disputes_store) if item.get("awbNumber") == dispute_id), -1)

    if index == -1:
        return jsonify({
            "success": False,
            "message": f"Dispute '{dispute_id}' not found."
        }), 404

    if disputes_store[index].get("status") != STATUS_PENDING:
        return jsonify({
            "success": False,
            "message": f"Dispute '{dispute_id}' is already {disputes_store[index].get('status')} and cannot be resolved."
        }), 409

    disputes_store[index].update({
        "status": resolution_status,
        "eligibleAmount": eligible_amount,
        "resolutionSummary": resolution_summary,
        "resolvedAt": iso_now(),
    })
    next_pending = unlock_next_pending(disputes_store)
    persist_store()

    return jsonify({
        "success": True,
        "message": f"Dispute {dispute_id} successfully resolved and updated.",
        "updatedDispute": format_dispute_for_response(disputes_store[index]),
        "nextPendingDispute": format_dispute_for_response(next_pending),
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
        "activeEvaluatorEmail": active_evaluator_email,
        "effectiveRecipient": get_effective_recipient(),
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
