#!/usr/bin/env python3
import html
import http.client
import json
import logging
import os
import socket
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

SOCKET_PATH = os.getenv("DOCKER_SOCKET", "/var/run/docker.sock")
STATE_FILE = Path(os.getenv("STATE_FILE", "/data/state.json"))
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_THREAD_ID = os.getenv("TELEGRAM_THREAD_ID", "").strip()
TELEGRAM_TOKEN_FILE = os.getenv("TELEGRAM_BOT_TOKEN_FILE", "/run/secrets/telegram_bot_token")
NOTIFY_LABEL = os.getenv("NOTIFY_LABEL", "").strip()
NOTIFY_IMAGE_ONLY = os.getenv("NOTIFY_IMAGE_ONLY", "true").lower() in {"1", "true", "yes", "on"}
UPDATE_TIMEOUT = int(os.getenv("UPDATE_TIMEOUT", "900"))
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "2"))
TELEGRAM_DRY_RUN = os.getenv("TELEGRAM_DRY_RUN", "false").lower() in {"1", "true", "yes", "on"}
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("swarm-update-notifier")


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout=None):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if self.timeout is not None:
            self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def docker_get(path: str, params=None, timeout=10):
    if params:
        path = f"{path}?{urllib.parse.urlencode(params)}"
    conn = UnixHTTPConnection(SOCKET_PATH, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        data = resp.read()
        if resp.status >= 400:
            raise RuntimeError(f"Docker API {path}: HTTP {resp.status}: {data.decode(errors='replace')}")
        if not data:
            return None
        return json.loads(data)
    finally:
        conn.close()


def docker_events(since=None):
    filters = json.dumps({"type": ["service"], "event": ["update"]}, separators=(",", ":"))
    params = {"filters": filters}
    if since is not None:
        params["since"] = str(since)
    path = "/events?" + urllib.parse.urlencode(params)
    conn = UnixHTTPConnection(SOCKET_PATH, timeout=None)
    conn.request("GET", path)
    resp = conn.getresponse()
    if resp.status >= 400:
        body = resp.read().decode(errors="replace")
        conn.close()
        raise RuntimeError(f"Docker events API: HTTP {resp.status}: {body}")
    return conn, resp


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        return {"services": {}}
    except Exception as exc:
        log.warning("Cannot read state file %s: %s", STATE_FILE, exc)
        return {"services": {}}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, STATE_FILE)


def service_image(service):
    return (
        service.get("Spec", {})
        .get("TaskTemplate", {})
        .get("ContainerSpec", {})
        .get("Image", "")
    )


def service_labels(service):
    labels = {}
    labels.update(service.get("Spec", {}).get("Labels", {}) or {})
    labels.update(
        service.get("Spec", {})
        .get("TaskTemplate", {})
        .get("ContainerSpec", {})
        .get("Labels", {})
        or {}
    )
    return labels


def label_matches(service):
    if not NOTIFY_LABEL:
        return True
    if "=" in NOTIFY_LABEL:
        key, expected = NOTIFY_LABEL.split("=", 1)
    else:
        key, expected = NOTIFY_LABEL, "true"
    value = str(service_labels(service).get(key, ""))
    return value.lower() == expected.lower()


def inspect_service(service_id):
    return docker_get(f"/services/{urllib.parse.quote(service_id, safe='')}")


def list_services():
    return docker_get("/services") or []


def list_tasks(service_id):
    filters = json.dumps({"service": [service_id]}, separators=(",", ":"))
    return docker_get("/tasks", {"filters": filters}) or []


def node_hostname(node_id, cache):
    if not node_id:
        return None
    if node_id in cache:
        return cache[node_id]
    try:
        node = docker_get(f"/nodes/{urllib.parse.quote(node_id, safe='')}")
        hostname = node.get("Description", {}).get("Hostname") or node_id[:12]
    except Exception:
        hostname = node_id[:12]
    cache[node_id] = hostname
    return hostname


def task_summary(service_id):
    tasks = list_tasks(service_id)
    active = [t for t in tasks if t.get("DesiredState") == "running"]
    running = [t for t in active if t.get("Status", {}).get("State") == "running"]
    cache = {}
    nodes = sorted({node_hostname(t.get("NodeID"), cache) for t in running if t.get("NodeID")})
    failed = [
        t for t in tasks
        if t.get("Status", {}).get("State") in {"failed", "rejected", "orphaned"}
    ]
    last_error = ""
    if failed:
        failed.sort(key=lambda t: t.get("Status", {}).get("Timestamp", ""), reverse=True)
        last_error = failed[0].get("Status", {}).get("Err", "") or failed[0].get("Status", {}).get("Message", "")
    return {
        "running": len(running),
        "desired": len(active),
        "nodes": nodes,
        "last_error": last_error,
    }


def image_display(image):
    if "@" in image:
        base, digest = image.rsplit("@", 1)
        short = digest.split(":", 1)[-1][:12]
        return base, short
    return image, ""


def service_identity(service):
    spec = service.get("Spec", {})
    name = spec.get("Name", service.get("ID", "unknown")[:12])
    labels = spec.get("Labels", {}) or {}
    stack = labels.get("com.docker.stack.namespace", "")
    short_name = name
    if stack and name.startswith(stack + "_"):
        short_name = name[len(stack) + 1 :]
    return stack, short_name, name


def telegram_token():
    try:
        return Path(TELEGRAM_TOKEN_FILE).read_text().strip()
    except Exception as exc:
        raise RuntimeError(f"Cannot read Telegram bot token from {TELEGRAM_TOKEN_FILE}: {exc}")


def send_telegram(text):
    if TELEGRAM_DRY_RUN:
        log.info("Telegram dry-run:\n%s", text)
        return
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID is required")
    token = telegram_token()
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if TELEGRAM_THREAD_ID:
        payload["message_thread_id"] = TELEGRAM_THREAD_ID
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        method="POST",
    )
    last_exc = None
    for attempt in range(1, 6):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read())
                if not body.get("ok"):
                    raise RuntimeError(f"Telegram API returned: {body}")
            return
        except Exception as exc:
            last_exc = exc
            log.warning("Telegram send failed (%d/5): %s", attempt, exc)
            time.sleep(min(2 ** attempt, 20))
    raise RuntimeError(f"Telegram send failed after retries: {last_exc}")


TERMINAL_STATES = {"completed", "paused", "rollback_completed", "rollback_paused"}


def wait_for_update(service_id):
    deadline = time.monotonic() + UPDATE_TIMEOUT
    last_state = ""
    service = inspect_service(service_id)
    while time.monotonic() < deadline:
        try:
            service = inspect_service(service_id)
        except Exception as exc:
            return "removed", str(exc), None, {"running": 0, "desired": 0, "nodes": [], "last_error": ""}
        update = service.get("UpdateStatus") or {}
        state = update.get("State", "")
        message = update.get("Message", "")
        if state and state != last_state:
            log.info("Service %s update state: %s", service.get("Spec", {}).get("Name"), state)
            last_state = state
        if state in TERMINAL_STATES:
            return state, message, service, task_summary(service_id)
        time.sleep(POLL_INTERVAL)
    service = inspect_service(service_id)
    update = service.get("UpdateStatus") or {}
    return "timeout", update.get("Message", ""), service, task_summary(service_id)


def format_message(status, message, before_image, attempted_image, final_service, summary, recovered=False):
    stack, short_name, full_name = service_identity(final_service)
    final_image = service_image(final_service)
    old_base, old_digest = image_display(before_image)
    new_base, new_digest = image_display(attempted_image)
    final_base, final_digest = image_display(final_image)

    if status == "completed":
        icon, title = "✅", "Swarm service updated"
    elif status == "rollback_completed":
        icon, title = "⚠️", "Swarm service rolled back"
    elif status in {"paused", "rollback_paused", "timeout", "removed"}:
        icon, title = "❌", "Swarm service update failed"
    else:
        icon, title = "ℹ️", "Swarm service changed"

    lines = [f"{icon} <b>{title}</b>"]
    if recovered:
        lines.append("<i>Detected after notifier restart</i>")
    lines.append("")
    if stack:
        lines.append(f"Stack: <code>{html.escape(stack)}</code>")
    lines.append(f"Service: <code>{html.escape(short_name)}</code>")
    if full_name != short_name:
        lines.append(f"Swarm name: <code>{html.escape(full_name)}</code>")
    lines.append("")

    if before_image != attempted_image:
        if old_base == new_base and old_digest and new_digest:
            lines.append(f"Image: <code>{html.escape(new_base)}</code>")
            lines.append(f"Digest: <code>{old_digest}</code> → <code>{new_digest}</code>")
        else:
            lines.append(f"From: <code>{html.escape(before_image)}</code>")
            lines.append(f"To: <code>{html.escape(attempted_image)}</code>")
    else:
        lines.append(f"Image: <code>{html.escape(attempted_image)}</code>")

    if final_image != attempted_image:
        lines.append(f"Final: <code>{html.escape(final_base)}@{final_digest if final_digest else ''}</code>")

    lines.append(f"Status: <code>{html.escape(status)}</code>")
    lines.append(f"Replicas: <code>{summary['running']}/{summary['desired']}</code>")
    if summary["nodes"]:
        lines.append(f"Node: <code>{html.escape(', '.join(summary['nodes']))}</code>")

    detail = summary.get("last_error") or message
    if detail and status != "completed":
        if len(detail) > 700:
            detail = detail[:697] + "..."
        lines.extend(["", f"Error: <code>{html.escape(detail)}</code>"])
    return "\n".join(lines)


def process_service_update(service_id, state, recovered=False):
    try:
        service = inspect_service(service_id)
    except Exception as exc:
        log.warning("Cannot inspect service %s: %s", service_id, exc)
        return

    current_version = int(service.get("Version", {}).get("Index", 0))
    attempted_image = service_image(service)
    previous = state["services"].get(service_id)

    if previous and int(previous.get("version", 0)) >= current_version:
        return

    if not previous:
        state["services"][service_id] = {
            "name": service.get("Spec", {}).get("Name", ""),
            "image": attempted_image,
            "version": current_version,
        }
        save_state(state)
        return

    before_image = previous.get("image", "")
    image_changed = bool(before_image and attempted_image and before_image != attempted_image)

    if not label_matches(service):
        state["services"][service_id] = {
            "name": service.get("Spec", {}).get("Name", ""),
            "image": attempted_image,
            "version": current_version,
        }
        save_state(state)
        return

    if NOTIFY_IMAGE_ONLY and not image_changed:
        log.info("Ignoring non-image update for %s", service.get("Spec", {}).get("Name"))
        state["services"][service_id] = {
            "name": service.get("Spec", {}).get("Name", ""),
            "image": attempted_image,
            "version": current_version,
        }
        save_state(state)
        return

    status, message, final_service, summary = wait_for_update(service_id)
    if final_service is None:
        return

    text = format_message(
        status,
        message,
        before_image,
        attempted_image,
        final_service,
        summary,
        recovered=recovered,
    )
    try:
        send_telegram(text)
    except Exception as exc:
        log.error("Notification failed: %s", exc)

    final_version = int(final_service.get("Version", {}).get("Index", current_version))
    final_image = service_image(final_service)
    state["services"][service_id] = {
        "name": final_service.get("Spec", {}).get("Name", ""),
        "image": final_image,
        "version": final_version,
    }
    save_state(state)


def initialize_or_reconcile(state):
    services = list_services()
    first_run = not bool(state.get("services"))
    current_ids = set()

    for service in services:
        service_id = service.get("ID")
        if not service_id:
            continue
        current_ids.add(service_id)
        current_version = int(service.get("Version", {}).get("Index", 0))
        current_image = service_image(service)
        previous = state["services"].get(service_id)

        if first_run or not previous:
            state["services"][service_id] = {
                "name": service.get("Spec", {}).get("Name", ""),
                "image": current_image,
                "version": current_version,
            }
            continue

        if current_version > int(previous.get("version", 0)):
            process_service_update(service_id, state, recovered=True)

    # Remove services that no longer exist so the state file does not grow forever.
    for stale in set(state["services"]) - current_ids:
        state["services"].pop(stale, None)

    save_state(state)
    if first_run:
        log.info("Initial baseline stored for %d services; no notifications sent", len(current_ids))


def main():
    if not Path(SOCKET_PATH).exists():
        raise RuntimeError(f"Docker socket not found: {SOCKET_PATH}")
    if not TELEGRAM_DRY_RUN:
        if not TELEGRAM_CHAT_ID:
            raise RuntimeError("TELEGRAM_CHAT_ID is required")
        telegram_token()

    state = load_state()
    state.setdefault("services", {})
    initialize_or_reconcile(state)

    since = int(time.time())
    log.info("Listening for Swarm service update events")
    while True:
        conn = None
        try:
            conn, resp = docker_events(since=since)
            while True:
                line = resp.readline()
                if not line:
                    raise RuntimeError("Docker event stream closed")
                event = json.loads(line)
                event_time = int(event.get("time", time.time()))
                since = max(since, event_time)
                service_id = event.get("Actor", {}).get("ID") or event.get("id")
                if not service_id:
                    continue
                process_service_update(service_id, state)
        except KeyboardInterrupt:
            return
        except Exception as exc:
            log.error("Event listener error: %s; reconnecting", exc)
            time.sleep(3)
        finally:
            if conn:
                conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log.critical("Fatal: %s", exc)
        sys.exit(1)
