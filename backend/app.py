from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import tempfile
from workspace import EncryptedWorkspace
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, Response, g, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from config import load_config
from db import (
    check_db,
    database_url,
    find_proof_events_by_video,
    init_db,
    insert_proof_event,
    insert_proof_history_event,
    list_proof_events,
    list_proof_history_events,
    make_idempotency_key,
    upsert_register_event,
    ConflictError,
)
from metrics import collector as metrics_collector
from noir import generate_silent_witness
from stego import canonical_metadata_hash, embed_metadata, extract_metadata, sha256_file
from logging_utils import log_structured, redact_sensitive
from readiness import ReadinessManager
from admission import AdmissionController, require_capacity
from webhook import WebhookWorker, queue_webhook_deliveries
from quarantine import QuarantineError, isolate_upload


ALLOWED_TIERS = {"silent", "source", "seal"}
REQUIRED_EMBED_METADATA = {"protocol", "version", "tier", "sourceHash", "proofId", "timestamp"}
LOGGER = logging.getLogger("harpocrates.requests")
if not LOGGER.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    LOGGER.addHandler(handler)
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False


def create_app() -> Flask:
    load_dotenv()
    config = load_config()
    app = Flask(__name__)
    CORS(
        app,
        origins=config.cors_origins,
        methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-Request-ID", "X-Metrics-Token"],
        expose_headers=[
            "Content-Disposition",
            "X-Request-ID",
            "X-Harpocrates-Source-Hash",
            "X-Harpocrates-Embedded-Hash",
            "X-Harpocrates-Metadata-Hash",
            "X-Harpocrates-Db-Event",
            "X-Harpocrates-Metadata",
        ],
    )
    app.config["MAX_CONTENT_LENGTH"] = config.max_content_length
    init_db()

    readiness_manager = ReadinessManager(timeout_seconds=1.0, cache_ttl_seconds=5.0)
    readiness_manager.add_dependency("database", check_db, critical=True)
    readiness_manager.add_dependency("video_tools", video_tooling_ready, critical=True)

    admission_controller = AdmissionController(
        max_concurrent=config.max_concurrent_requests,
        max_queue=config.max_queue_size,
        max_per_identity=config.max_concurrent_per_identity,
        timeout_seconds=config.admission_timeout_seconds,
    )

    @app.before_request
    def start_request_context():
        g.start_time = time.perf_counter()
        g.request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        g.request_started_at = time.perf_counter()

    @app.after_request
    def process_response(response: Response):
        response.headers["X-Request-ID"] = request_id()
        if config.security_headers_enabled:
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Referrer-Policy", "no-referrer")
            response.headers.setdefault("Cross-Origin-Resource-Policy", "same-site")
            response.headers.setdefault("Cache-Control", "no-store")

        if config.metrics_enabled and request.path != config.metrics_path:
            start_time = getattr(g, "start_time", None)
            duration = time.perf_counter() - start_time if start_time is not None else 0.0
            endpoint_rule = request.url_rule.rule if request.url_rule else "unmatched"
            metrics_collector.record_request(
                method=request.method,
                endpoint=endpoint_rule,
                status=response.status_code,
                duration_seconds=duration,
                upload_bytes=request.content_length,
            )

        if response.status_code >= 400:
            log_error_response(response.status_code)
        log_structured(
            LOGGER,
            logging.INFO,
            {
                "event": "request",
                "request_id": request_id(),
                "method": request.method,
                "route": request_route(),
                "path": request.path,
                "status": response.status_code,
                "duration_ms": request_duration_ms(),
            },
        )
        return response

    @app.errorhandler(RequestEntityTooLarge)
    def request_too_large(_error: RequestEntityTooLarge):
        return jsonify({"error": "request body is too large"}), 413

    @app.errorhandler(ValueError)
    def bad_request(error: ValueError):
        return jsonify({"error": str(error)}), 400

    @app.errorhandler(RuntimeError)
    def runtime_error(error: RuntimeError):
        return jsonify({"error": str(error)}), 500

    @app.get(config.metrics_path)
    def metrics():
        if not config.metrics_enabled:
            return jsonify({"error": "metrics service disabled"}), 404

        if config.metrics_token:
            auth_header = request.headers.get("Authorization", "")
            token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
            custom_token = request.headers.get("X-Metrics-Token", "").strip()
            if token != config.metrics_token and custom_token != config.metrics_token:
                return jsonify({"error": "unauthorized metrics access"}), 401

        output = metrics_collector.generate_prometheus_metrics()
        return Response(output, mimetype="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "service": "harpocrates-stego"})

    @app.get("/ready")
    def ready():
        status = readiness_manager.check()
        return jsonify(
            {
                "ok": status["ok"],
                "service": "harpocrates-stego",
                "database": status.get("database", "not_configured"),
                "video_tools": status.get("video_tools", "missing"),
                "noir_worker": "enabled" if config.noir_worker_enabled else "disabled",
            }
        ), 200 if status["ok"] else 503

    def _enforce_video_size(video) -> bool:
        video.seek(0, 2)
        size = video.tell()
        video.seek(0)
        return size <= config.max_video_bytes

    def _enable_streaming_for_large_uploads():
        """Replace large file uploads with streaming versions."""
        upload_max_bytes = getattr(config, "upload_max_bytes", config.max_video_bytes)
        if request.content_length and request.content_length > upload_max_bytes:
            # Store config for streaming file creation
            g.upload_config = config
            
            # Replace file uploads with streaming versions
            new_files = {}
            for field_name, field_storage in request.files.items():
                new_files[field_name] = create_streaming_file_storage(field_storage)
            
            # Replace the files in the request
            request.files = type(request.files)(new_files)

    def _enforce_json_size() -> int:
        raw = request.get_data()
        return len(raw) if raw else 0

    @app.post("/api/stego/embed")
    @require_capacity(admission_controller)
    def embed():
        # Enable streaming for large uploads
        _enable_streaming_for_large_uploads()
        
        video = request.files.get("video")
        metadata_raw = request.form.get("metadata")
        if video is None or metadata_raw is None:
            return jsonify({"error": "video and metadata are required"}), 400
        if not _enforce_video_size(video):
            return jsonify({"error": "video payload exceeds size limit"}), 413
        validate_video_upload(video)
        if len(metadata_raw.encode("utf-8")) > config.max_metadata_bytes:
            return jsonify({"error": "metadata is too large"}), 413

        try:
            metadata = json.loads(metadata_raw)
        except json.JSONDecodeError:
            return jsonify({"error": "metadata must be valid JSON"}), 400
        try:
            validate_embed_metadata(metadata)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        try:
            quarantine_context = isolate_upload(video)
            with quarantine_context as quarantined_path, EncryptedWorkspace() as workspace:
                source_url = workspace.get_url("source.video")
                output_url = workspace.get_url("embedded.mp4")
                with quarantined_path.open("rb") as quarantined:
                    workspace.write_encrypted(
                        "source.video",
                        quarantined,
                        size=quarantined_path.stat().st_size,
                    )

                embed_metadata(source_url, output_url, metadata)
                output_bytes = workspace.read_decrypted("embedded.mp4")
                source_hash = workspace.sha256("source.video")
                embedded_hash = workspace.sha256("embedded.mp4")
                metadata_hash = canonical_metadata_hash(metadata)
        except QuarantineError as exc:
            return jsonify({"error": str(exc)}), 400

        db_event = insert_proof_event(
            event_type="embed",
            file_name=safe_filename(video.filename),
            video_hash=embedded_hash,
            metadata_hash=metadata_hash,
            proof_id=metadata.get("proofId"),
            tier=metadata.get("tier"),
            embedded_hash=embedded_hash,
            metadata=redact_metadata(metadata),
        )

        if db_event and db_event.get("id"):
            queue_webhook_deliveries(db_event["id"])

        response = Response(output_bytes, mimetype="video/mp4")
        response.headers["Content-Disposition"] = 'attachment; filename="harpocrates-evidence.mp4"'
        response.headers["X-Harpocrates-Source-Hash"] = source_hash
        response.headers["X-Harpocrates-Embedded-Hash"] = embedded_hash
        response.headers["X-Harpocrates-Metadata-Hash"] = metadata_hash
        response.headers["X-Harpocrates-Db-Event"] = str(db_event)
        if config.expose_metadata_header:
            response.headers["X-Harpocrates-Metadata"] = base64.b64encode(
                json.dumps(metadata, separators=(",", ":"), sort_keys=True).encode("utf-8")
            ).decode("ascii")
        return response

    @app.post("/api/stego/extract")
    @require_capacity(admission_controller)
    def extract():
        # Enable streaming for large uploads
        _enable_streaming_for_large_uploads()
        
        video = request.files.get("video")
        if video is None:
            return jsonify({"error": "video is required"}), 400
        if not _enforce_video_size(video):
            return jsonify({"error": "video payload exceeds size limit"}), 413
        validate_video_upload(video)

        try:
            quarantine_context = isolate_upload(video)
            with quarantine_context as quarantined_path, EncryptedWorkspace() as workspace:
                source_url = workspace.get_url("source.video")
                with quarantined_path.open("rb") as quarantined:
                    workspace.write_encrypted(
                        "source.video",
                        quarantined,
                        size=quarantined_path.stat().st_size,
                    )
                metadata = extract_metadata(source_url)
                video_hash = workspace.sha256("source.video")
                metadata_hash = canonical_metadata_hash(metadata) if metadata else None
        except QuarantineError as exc:
            return jsonify({"error": str(exc)}), 400

        db_event = insert_proof_event(
            event_type="extract",
            file_name=safe_filename(video.filename),
            video_hash=video_hash,
            metadata_hash=metadata_hash,
            proof_id=metadata.get("proofId") if metadata else None,
            tier=metadata.get("tier") if metadata else None,
            metadata=redact_metadata(metadata),
        )

        if db_event and db_event.get("id"):
            queue_webhook_deliveries(db_event["id"])

        return jsonify(
            {
                "ok": True,
                "video_hash": video_hash,
                "metadata_hash": metadata_hash,
                "metadata": metadata,
                "db_event": db_event,
            }
        )

    @app.get("/api/proofs")
    def proofs():
        limit = request.args.get("limit", "25")
        try:
            parsed_limit = int(limit)
        except ValueError:
            return jsonify({"error": "limit must be an integer"}), 400

        return jsonify({"ok": True, "events": list_proof_events(parsed_limit)})

    @app.get("/api/proofs/by-video/<video_hash>")
    def proof_by_video(video_hash: str):
        if not is_hex_32(video_hash):
            return jsonify({"error": "video_hash must be a 32-byte hex string"}), 400

        return jsonify({"ok": True, "events": find_proof_events_by_video(video_hash)})

    @app.post("/api/proofs/register")
    def register_proof_event():
        if _enforce_json_size() > config.max_json_bytes:
            return jsonify({"error": "JSON payload exceeds size limit"}), 413
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "JSON body is required"}), 400
        if len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) > config.max_metadata_bytes:
            return jsonify({"error": "registration payload is too large"}), 413

        video_hash = payload.get("videoHash")
        metadata_hash = payload.get("metadataHash")
        proof_id = payload.get("proofId")
        tx_hash = payload.get("txHash")
        tx_status = payload.get("txStatus")

        for name, value in (
            ("videoHash", video_hash),
            ("metadataHash", metadata_hash),
            ("proofId", proof_id),
        ):
            if not is_hex_32(value):
                return jsonify({"error": f"{name} must be a 32-byte hex string"}), 400

        try:
            normalized_tx_hash = normalize_tx_hash(tx_hash)
            normalized_tx_status = normalize_tx_status(tx_status)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        idempotency_key = make_idempotency_key(video_hash, proof_id, normalized_tx_hash)

        try:
            db_event, created = upsert_register_event(
                idempotency_key=idempotency_key,
                file_name=safe_filename(payload.get("fileName")),
                video_hash=video_hash,
                metadata_hash=metadata_hash,
                proof_id=proof_id,
                tier=payload.get("tier"),
                tx_hash=normalized_tx_hash,
                tx_status=normalized_tx_status,
                source_address=payload.get("sourceAddress"),
                contract_id=payload.get("contractId"),
                metadata=redact_metadata(payload),
            )
        except ConflictError as exc:
            return jsonify({
                "error": "idempotency key reused with conflicting payload",
                "conflict_field": exc.field,
            }), 409

        if db_event and db_event.get("id") and created:
            queue_webhook_deliveries(db_event["id"])

        status = 201 if created else 200
        return jsonify({"ok": True, "db_event": db_event, "created": created}), status

    @app.get("/api/proofs/history/<proof_id>")
    def proof_history(proof_id: str):
        if not is_hex_32(proof_id):
            return jsonify({"error": "proof_id must be a 32-byte hex string"}), 400

        limit = request.args.get("limit", "50")
        offset = request.args.get("offset", "0")
        try:
            parsed_limit = int(limit)
            parsed_offset = int(offset)
        except ValueError:
            return jsonify({"error": "limit and offset must be integers"}), 400

        return jsonify(
            {"ok": True, "events": list_proof_history_events(proof_id, parsed_limit, parsed_offset)}
        )

    @app.post("/api/proofs/history")
    def register_proof_history_event():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "JSON body is required"}), 400

        proof_id = payload.get("proofId")
        action = payload.get("action")
        actor = payload.get("actor")
        reason_code = payload.get("reasonCode")
        contract_id = payload.get("contractId")
        tx_hash = payload.get("txHash")

        if not is_hex_32(proof_id):
            return jsonify({"error": "proofId must be a 32-byte hex string"}), 400
        if not isinstance(action, str) or not action:
            return jsonify({"error": "action is required"}), 400
        if reason_code is None or not isinstance(reason_code, int):
            return jsonify({"error": "reasonCode is required"}), 400

        db_event = insert_proof_history_event(
            proof_id=proof_id,
            action=action,
            actor=actor,
            reason_code=reason_code,
            contract_id=contract_id,
            tx_hash=tx_hash,
        )

        return jsonify({"ok": True, "db_event": db_event})

    @app.post("/api/noir/silent-witness")
    @require_capacity(admission_controller)
    def silent_witness_proof():
        if _enforce_json_size() > config.max_json_bytes:
            return jsonify({"error": "JSON payload exceeds size limit"}), 413
        if not config.noir_worker_enabled:
            return jsonify({"error": "local Noir worker is disabled"}), 404

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "JSON body is required"}), 400

        video_hash = payload.get("videoHash")
        if not is_hex_32(video_hash):
            return jsonify({"error": "videoHash must be a 32-byte hex string"}), 400
        credential_secret = payload.get("credentialSecret")
        nullifier_secret = payload.get("nullifierSecret")
        if not is_field_decimal(credential_secret):
            return jsonify({"error": "credentialSecret must be a decimal field string"}), 400
        if not is_field_decimal(nullifier_secret):
            return jsonify({"error": "nullifierSecret must be a decimal field string"}), 400

        try:
            proof = generate_silent_witness(
                video_hash,
                credential_secret,
                nullifier_secret,
            )
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 500

        return jsonify({"ok": True, "proof": proof})

    # Start webhook worker if DB is enabled
    if database_url():
        webhook_worker = WebhookWorker()
        webhook_worker.start()
        app.webhook_worker = webhook_worker

    return app


def request_id() -> str:
    return getattr(g, "request_id", "unknown")


def request_route() -> str:
    return request.url_rule.rule if request.url_rule else request.path


def request_duration_ms() -> float:
    started_at = getattr(g, "request_started_at", None)
    if started_at is None:
        return 0.0
    return round((time.perf_counter() - started_at) * 1000, 2)


def log_error_response(status: int) -> None:
    log_structured(
        LOGGER,
        logging.ERROR,
        {
            "event": "error",
            "request_id": request_id(),
            "method": request.method,
            "route": request_route(),
            "path": request.path,
            "status": status,
            "duration_ms": request_duration_ms(),
        },
    )


def is_hex_32(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def normalize_tx_hash(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("txHash must be a 32-byte hex string")

    normalized = value.strip().lower()
    if not is_hex_32(normalized):
        raise ValueError("txHash must be a 32-byte hex string")
    return normalized


def normalize_tx_status(value: object) -> str | None:
    if value is None:
        return None

    if not isinstance(value, str):
        raise ValueError("txStatus must be one of: pending, confirmed, failed, missing")

    normalized = value.strip().lower()
    if normalized in {"pending", "confirmed", "failed", "missing"}:
        return normalized
    if normalized in {"success", "successful", "succeeded"}:
        return "confirmed"
    if normalized in {"failure", "failed", "error", "errored"}:
        return "failed"
    if normalized in {"not_found", "notfound"}:
        return "missing"
    raise ValueError("txStatus must be one of: pending, confirmed, failed, missing")


def is_field_decimal(value: object) -> bool:
    if not isinstance(value, str) or not value or not value.isdecimal():
        return False
    field_modulus = (
        21888242871839275222246405745257275088548364400416034343698204186575808495617
    )
    return 0 < int(value) < field_modulus


def validate_video_upload(video) -> None:
    if not video.filename:
        raise ValueError("video filename is required")
    content_type = (video.content_type or "").lower()
    if content_type and not content_type.startswith("video/") and content_type != "application/octet-stream":
        raise ValueError("video upload must use a video content type")


def validate_embed_metadata(metadata: object) -> None:
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be a JSON object")
    missing = REQUIRED_EMBED_METADATA - set(metadata.keys())
    if missing:
        raise ValueError(f"metadata missing required field: {sorted(missing)[0]}")
    if metadata.get("protocol") != "harpocrates":
        raise ValueError("metadata protocol must be harpocrates")
    if metadata.get("tier") not in ALLOWED_TIERS:
        raise ValueError("metadata tier is invalid")
    if not is_hex_32(metadata.get("sourceHash")):
        raise ValueError("metadata sourceHash must be a 32-byte hex string")
    if not is_hex_32(metadata.get("proofId")):
        raise ValueError("metadata proofId must be a 32-byte hex string")
    _validate_timestamp(metadata.get("timestamp"))


def _validate_timestamp(value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("metadata timestamp must be a string")
    ts = value.strip().replace("Z", "+00:00").replace("z", "+00:00")
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        raise ValueError("metadata timestamp must be a timezone-aware ISO-8601 string")
    if dt.tzinfo is None:
        raise ValueError("metadata timestamp must be timezone-aware")
    if dt > datetime.now(timezone.utc) + timedelta(seconds=300):
        raise ValueError("metadata timestamp is unreasonably far in the future")


def safe_filename(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    sanitized = secure_filename(value)
    return sanitized[:160] if sanitized else None


def redact_metadata(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    return redact_sensitive(value)


def video_tooling_ready() -> bool:
    try:
        from stego import _require

        _require("ffmpeg")
        _require("ffprobe")
    except RuntimeError:
        return False
    return True


app = create_app()


if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "5050")), debug=debug)
