"""Shared OpenAI-compatible connection settings, independent of the Settings UI."""

from urllib.parse import urlsplit, urlunsplit


def normalize_api_url(value):
    value = str(value or "").strip()
    if not value:
        raise ValueError("Enter an API server address.")
    if "://" not in value:
        value = "http://" + value
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or any(c.isspace() for c in value)):
        raise ValueError("Use an HTTP or HTTPS server address without credentials, query or fragment.")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("Invalid server port.") from exc
    path = parsed.path.rstrip("/")
    if not path.lower().endswith("/v1"):
        path += "/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def api_key_value(data, previous=None):
    """Omitted keys survive edits only for the same normalized endpoint."""
    if "api_key" in data:
        value = data["api_key"]
        if not isinstance(value, str) or len(value) > 4096:
            raise ValueError("Invalid API key.")
        value = value.strip()
        if any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise ValueError("API keys cannot contain spaces or control characters.")
        return value
    previous = previous or {}
    def endpoint(value):
        value = str(value or "").strip().rstrip("/")
        if value and "://" not in value:
            value = "http://" + value
        if value and not value.lower().endswith("/v1"):
            value += "/v1"
        return value
    if endpoint(data.get("api_url")) == endpoint(previous.get("api_url")):
        return api_key_value({"api_key": previous.get("api_key", "")})
    return ""


def auth_headers(connection):
    key = api_key_value({"api_key": connection.get("api_key", "")})
    return {"Authorization": "Bearer " + key} if key else {}


def public_connection(value):
    """Keep credentials out of config responses, including custom/shared backups."""
    if isinstance(value, list):
        return [public_connection(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: public_connection(item) for key, item in value.items() if key != "api_key"}
    if "api_key" in value:
        result["api_key_set"] = bool(value["api_key"])
    return result


def validate_connection(data, *, allow_auto=False):
    if not isinstance(data, dict):
        raise ValueError("Expected a connection object.")
    backend = data.get("backend", "openai")
    if backend != "openai":
        raise ValueError("Choose an OpenAI-compatible backend, such as LM Studio.")
    transport = data.get("transport", "hub")
    if transport not in ({"hub", "browser", "auto"} if allow_auto else {"hub", "browser"}):
        raise ValueError("Choose the CyberHub computer or browser computer.")
    model = data.get("model", "")
    if not isinstance(model, str) or len(model) > 512:
        raise ValueError("Invalid model name.")
    return {"backend": backend, "api_url": normalize_api_url(data.get("api_url")),
            "model": model.strip(), "transport": transport, "api_key": api_key_value(data)}


class AIConnection:
    """Modules opt in through hub.ai_connection; older Hubs need no new imports."""

    def __init__(self, settings):
        self.settings = settings

    def shared(self):
        saved = self.settings.get("ai_connection", {})
        if not saved:
            return {"configured": False, "backend": "openai", "api_url": "",
                    "model": "", "transport": "hub", "api_key": ""}
        return {"configured": True, **validate_connection(saved)}

    def draft(self, data):
        connection = validate_connection(data)
        connection["api_key"] = api_key_value(data, self.settings.get("ai_connection", {}))
        return connection

    def save(self, data):
        connection = self.draft(data)
        self.settings.set("ai_connection", connection)
        return self.shared()

    def resolve(self, module_key, *, legacy_transport="browser"):
        local = self.settings.get_module(module_key)
        try:
            shared = self.shared()
        except ValueError as exc:
            shared = {"configured": False, "api_url": "", "model": "", "transport": "hub", "error": str(exc)}
        mode = local.get("connection_mode")
        if mode not in {"shared", "custom"}:
            # Existing module connections never silently opt into a global default.
            mode = "custom" if local.get("api_url") or not shared["configured"] else "shared"
        custom = {
            "api_url": local.get("api_url") or "http://localhost:1234/v1",
            "model": local.get("model") or "",
            "transport": local.get("transport") or legacy_transport,
            "api_key": local.get("api_key") or "",
        }
        connection = shared if mode == "shared" else custom
        error = "Save a central AI connection in Settings first." if mode == "shared" and not shared["configured"] else ""
        return {
            "central_available": True, "shared": shared, "custom": custom,
            "connection_mode": mode, "connection_error": error,
            "api_url": connection["api_url"], "transport": connection["transport"],
            "api_key": connection.get("api_key", ""),
            "model": (local.get("shared_model") or shared["model"]) if mode == "shared" else custom["model"],
            "shared_model": local.get("shared_model") or "",
            "connection_saved": bool(local.get("connection_mode") or local.get("api_url")),
        }

    def module_values(self, data, *, legacy_transport="browser"):
        """Validate before saving; shared mode must not overwrite the custom backup."""
        if not isinstance(data, dict):
            raise ValueError("Expected connection settings.")
        mode = data.get("connection_mode", "custom")
        if mode == "shared":
            if not self.shared()["configured"]:
                raise ValueError("Save a central AI connection in Settings first.")
            model = data.get("shared_model", "")
            if not isinstance(model, str) or len(model) > 512:
                raise ValueError("Invalid model name.")
            return {"connection_mode": mode, "shared_model": model.strip()}
        if mode != "custom":
            raise ValueError("Choose the central connection or an own connection.")
        connection = validate_connection({**data, "transport": data.get("transport", legacy_transport)}, allow_auto=True)
        return {"connection_mode": mode, **{key: connection[key] for key in ("api_url", "model", "transport", "api_key")}}

    @staticmethod
    def models(data):
        import requests
        connection = validate_connection(data, allow_auto=True)
        response = requests.get(connection["api_url"] + "/models", timeout=8,
                                headers=auth_headers(connection), allow_redirects=False)
        response.raise_for_status()
        models = response.json().get("data", [])
        if not isinstance(models, list):
            raise ValueError("Unexpected model response.")
        models = [item for item in models if isinstance(item, dict) and isinstance(item.get("id"), str)]
        return {"models": [item["id"] for item in models], "data": models}
